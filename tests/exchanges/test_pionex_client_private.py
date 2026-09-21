"""
Tests für die signierten (privaten) Endpunkte von
sgr.exchanges.pionex_client.PionexClient.

Deckt (siehe Aufgabenstellung "TESTS"): Authentication, Rate Limit,
Malformed Response, Timeout/Exchange-unavailable (auf Client-Ebene).
Alle Tests nutzen FakeSession/FakeResponse (kein echtes Netzwerk, keine
echten Credentials, keine echten Orders - dieser Client kann ohnehin
keine Order-Methoden aufrufen, siehe Modul-Docstring).
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
import requests

from sgr.exchanges.pionex_client import (
    PionexAPIError,
    PionexAuthenticationError,
    PionexClient,
    PionexHTTPError,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(f"HTTP {self.status_code}", response=response)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception
        self.headers = {}
        self.last_url = None
        self.last_params = None
        self.last_headers = None
        self.last_data = None
        self.last_method = None
        self.closed = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.last_method = "GET"
        self.last_url = url
        self.last_params = params
        self.last_headers = headers
        self.last_timeout = timeout
        if self.exception is not None:
            raise self.exception
        return self.response

    def post(self, url, data=None, headers=None, timeout=None):
        return self._write("POST", url, data, headers, timeout)

    def delete(self, url, data=None, headers=None, timeout=None):
        return self._write("DELETE", url, data, headers, timeout)

    def _write(self, method, url, data, headers, timeout):
        self.last_method = method
        self.last_url = url
        self.last_data = data
        self.last_headers = headers
        self.last_timeout = timeout
        if self.exception is not None:
            raise self.exception
        return self.response

    def close(self):
        self.closed = True


def _ok_payload(data):
    return {"result": True, "data": data, "timestamp": 1655896754515}


class TestSignatureAlgorithm:
    """Verifiziert die Signatur BYTE-FUER-BYTE gegen das offizielle
    Pionex-Beispiel (siehe Modul-Docstring von pionex_client.py -
    Secret/Timestamp/Query stammen direkt aus
    https://pionex-doc.gitbook.io/apidocs/restful/general/authentication)."""

    def test_signed_query_string_is_sorted_ascending(self) -> None:
        client = PionexClient(api_key="k", api_secret="s")
        qs = client._signed_query_string(
            {"symbol": "BTC_USDT", "limit": 1, "timestamp": "1655896754515"}
        )
        assert qs == "limit=1&symbol=BTC_USDT&timestamp=1655896754515"

    def test_signed_query_string_drops_none_values(self) -> None:
        client = PionexClient(api_key="k", api_secret="s")
        qs = client._signed_query_string({"symbol": "BTC_USDT", "endTime": None})
        assert qs == "symbol=BTC_USDT"

    def test_sign_matches_official_no_body_reference(self) -> None:
        """
        Gegenprobe ohne Body-Suffix (die von diesem Client gewaehlte,
        mit der Signing-Anleitung konsistente Variante - siehe
        PionexClient._sign() Docstring fuer die vollstaendige
        Herleitung inkl. der gefundenen Dokumentations-Inkonsistenz).
        """
        client = PionexClient(api_key="k", api_secret="NFqv4MB3hB0SOiEsJNDP9e0jDdKPWbDqS_Z1dbU4")
        query_string = "limit=1&symbol=BTC_USDT&timestamp=1655896754515"

        signature = client._sign("GET", "/api/v1/trade/allOrders", query_string)

        expected = hmac.new(
            b"NFqv4MB3hB0SOiEsJNDP9e0jDdKPWbDqS_Z1dbU4",
            b"GET/api/v1/trade/allOrders?limit=1&symbol=BTC_USDT&timestamp=1655896754515",
            hashlib.sha256,
        ).hexdigest()
        assert signature == expected
        assert len(signature) == 64

    def test_sign_without_query_string_omits_question_mark(self) -> None:
        client = PionexClient(api_key="k", api_secret="s")
        signature = client._sign("GET", "/api/v1/account/balances", "")
        expected = hmac.new(b"s", b"GET/api/v1/account/balances", hashlib.sha256).hexdigest()
        assert signature == expected

    def test_sign_raises_without_secret(self) -> None:
        client = PionexClient(api_key="k")
        with pytest.raises(PionexAuthenticationError):
            client._sign("GET", "/api/v1/account/balances", "")

    def test_sign_with_body_matches_official_documented_example(self) -> None:
        """
        Byte-exakte Gegenprobe MIT Body - aus demselben offiziellen
        Doku-Beispiel wie test_sign_matches_official_no_body_reference,
        diesmal fuer den Body-Anhang-Fall (POST/DELETE-Verhalten). Das
        Beispiel illustriert das zwar an einem GET-Pfad (die dokumentierte
        Inkonsistenz, siehe PionexClient._sign() Docstring), der
        resultierende Signaturwert selbst ist trotzdem ein korrekter,
        unabhaengig nachrechenbarer Beleg fuer "Body wird woertlich an
        METHOD+PATH_URL angehaengt" - exakt der jetzt fuer POST/DELETE
        implementierte Signing-Codepfad (_sign(..., body=...)).
        """
        client = PionexClient(api_key="k", api_secret="NFqv4MB3hB0SOiEsJNDP9e0jDdKPWbDqS_Z1dbU4")
        query_string = "limit=1&symbol=BTC_USDT&timestamp=1655896754515"
        body = '{"symbol": "BTC_USDT"}'

        signature = client._sign("GET", "/api/v1/trade/allOrders", query_string, body=body)

        assert signature == "ec83d21e1237cbe7e0172f79c0e3a4741c86f6b201ba762f21149bf195519be1"
        assert len(signature) == 64

    def test_sign_without_body_unaffected_by_new_parameter(self) -> None:
        """Default body="" darf das bestehende, bereits live verifizierte
        GET-Verhalten (siehe test_sign_matches_official_no_body_reference)
        nicht veraendern."""
        client = PionexClient(api_key="k", api_secret="s")
        with_default = client._sign("GET", "/api/v1/account/balances", "")
        with_explicit_empty = client._sign("GET", "/api/v1/account/balances", "", body="")
        assert with_default == with_explicit_empty


class TestPrivateGetRequiresCredentials:
    def test_private_get_raises_without_api_key(self) -> None:
        client = PionexClient(api_secret="s")
        with pytest.raises(PionexAuthenticationError):
            client.get_account_balances()

    def test_private_get_raises_without_api_secret(self) -> None:
        client = PionexClient(api_key="k")
        with pytest.raises(PionexAuthenticationError):
            client.get_account_balances()

    def test_private_get_raises_without_any_credentials(self) -> None:
        client = PionexClient()
        with pytest.raises(PionexAuthenticationError):
            client.get_futures_positions()


class TestPrivateGetSendsCorrectHeaders:
    def test_sends_pionex_key_and_signature_headers(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"balances": []})))
        client = PionexClient(session=session, api_key="my-key", api_secret="my-secret")

        client.get_account_balances()

        assert session.last_headers["PIONEX-KEY"] == "my-key"
        assert "PIONEX-SIGNATURE" in session.last_headers
        assert len(session.last_headers["PIONEX-SIGNATURE"]) == 64

    def test_timestamp_embedded_in_url_not_passed_as_params(self) -> None:
        """Die Signatur wird ueber eine selbst gebaute Query-String
        gebildet und direkt in die URL eingebettet (siehe _private_get()
        Docstring) - params= bleibt bewusst None, damit `requests` sie
        nicht ein zweites Mal (potenziell abweichend) kodiert."""
        session = FakeSession(FakeResponse(_ok_payload({"balances": []})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.get_account_balances()

        assert session.last_params is None
        assert "timestamp=" in session.last_url


class TestAccountAndBalanceEndpoints:
    def test_get_account_balances_spot(self) -> None:
        session = FakeSession(
            FakeResponse(
                _ok_payload({"balances": [{"coin": "USDT", "free": "100", "frozen": "0"}]})
            )
        )
        client = PionexClient(session=session, api_key="k", api_secret="s")

        balances = client.get_account_balances()

        assert balances == [{"coin": "USDT", "free": "100", "frozen": "0"}]
        assert session.last_url.startswith(f"{PionexClient.BASE_URL}/api/v1/account/balances")

    def test_get_futures_account_balances(self) -> None:
        session = FakeSession(
            FakeResponse(_ok_payload({"balances": [{"coin": "USDT", "free": "1"}], "isolates": []}))
        )
        client = PionexClient(session=session, api_key="k", api_secret="s")

        data = client.get_futures_account_balances()

        assert "balances" in data
        assert "/uapi/v1/account/balances" in session.last_url


class TestFuturesInstrumentEndpoints:
    def test_get_symbols_with_perp_type_filter(self) -> None:
        session = FakeSession(
            FakeResponse(_ok_payload({"symbols": [{"symbol": "BTC_USDT_PERP", "type": "PERP"}]}))
        )
        client = PionexClient(session=session)

        symbols = client.get_symbols(market_type="PERP")

        assert symbols[0]["symbol"] == "BTC_USDT_PERP"
        assert session.last_params == {"type": "PERP"}

    def test_get_symbols_rejects_invalid_market_type(self) -> None:
        client = PionexClient(session=FakeSession())
        with pytest.raises(ValueError, match="SPOT or PERP"):
            client.get_symbols(market_type="FUTURES")

    def test_get_futures_risk_table(self) -> None:
        session = FakeSession(
            FakeResponse(_ok_payload({"symbol": "BTC_USDT_PERP", "rows": [{"maxLeverage": "20"}]}))
        )
        client = PionexClient(session=session)

        rows = client.get_futures_risk_table("BTC_USDT_PERP")

        assert rows == [{"maxLeverage": "20"}]


class TestPositionEndpoint:
    def test_get_futures_positions(self) -> None:
        session = FakeSession(
            FakeResponse(
                _ok_payload(
                    {
                        "positions": [
                            {
                                "symbol": "BTC_USDT_PERP",
                                "positionSide": "LONG",
                                "netSize": "0.1",
                            }
                        ]
                    }
                )
            )
        )
        client = PionexClient(session=session, api_key="k", api_secret="s")

        positions = client.get_futures_positions()

        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTC_USDT_PERP"

    def test_get_futures_positions_filters_by_symbol(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"positions": []})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.get_futures_positions(symbol="ETH_USDT_PERP")

        assert "symbol=ETH_USDT_PERP" in session.last_url


class TestOpenOrdersEndpoint:
    def test_get_spot_open_orders_requires_symbol_param(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"orders": []})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.get_spot_open_orders("BTC_USDT")

        assert "symbol=BTC_USDT" in session.last_url

    def test_get_futures_open_orders_symbol_optional(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"orders": []})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.get_futures_open_orders()

        assert "symbol" not in session.last_url

    def test_get_futures_open_orders_validates_limit(self) -> None:
        client = PionexClient(session=FakeSession(), api_key="k", api_secret="s")
        with pytest.raises(ValueError, match="between 1 and 500"):
            client.get_futures_open_orders(limit=1000)


class TestOrderStatusEndpoint:
    def test_get_spot_order_by_id(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"orderId": 42, "status": "CLOSED"})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        order = client.get_spot_order(42)

        assert order["orderId"] == 42
        assert "orderId=42" in session.last_url

    def test_get_futures_order_requires_symbol_and_id(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"orderId": 42})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.get_futures_order("BTC_USDT_PERP", 42)

        assert "symbol=BTC_USDT_PERP" in session.last_url
        assert "orderId=42" in session.last_url


class TestFundingEndpoints:
    def test_get_funding_rates_public_no_auth_needed(self) -> None:
        session = FakeSession(
            FakeResponse(
                _ok_payload({"symbol": "BTC_USDT_PERP", "rates": [{"fundingRate": "0.0001"}]})
            )
        )
        client = PionexClient(session=session)  # keine Credentials

        data = client.get_funding_rates("BTC_USDT_PERP")

        assert data["rates"][0]["fundingRate"] == "0.0001"
        assert session.last_headers is None

    def test_get_open_interests_public_no_auth_needed(self) -> None:
        session = FakeSession(
            FakeResponse(_ok_payload({"openInterests": [{"symbol": "BTC_USDT_PERP"}]}))
        )
        client = PionexClient(session=session)

        rows = client.get_open_interests()

        assert rows[0]["symbol"] == "BTC_USDT_PERP"

    def test_get_futures_funding_fees_requires_auth(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"fundings": []})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.get_futures_funding_fees(symbol="BTC_USDT_PERP", limit=50)

        assert "PIONEX-SIGNATURE" in session.last_headers

    def test_get_futures_funding_fees_validates_limit(self) -> None:
        client = PionexClient(session=FakeSession(), api_key="k", api_secret="s")
        with pytest.raises(ValueError, match="between 1 and 200"):
            client.get_futures_funding_fees(limit=500)


class TestFuturesWriteEndpoints:
    """Futures Write Integration: create_futures_order/cancel_futures_order/
    cancel_all_futures_orders/set_futures_leverage/
    get_futures_order_by_client_id. Verifiziert gegen openapi_futures.yaml
    (siehe pionex_client.py Modul-Docstring) - AUSSCHLIESSLICH FakeSession,
    kein echtes Netzwerk, keine echte Order (siehe Aufgabenstellung)."""

    def test_create_futures_order_uses_post_and_signs_body(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"orderId": 123456789})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        order_id = client.create_futures_order(
            "BTC_USDT_PERP", "BUY", "MARKET_QTY", size="0.01", reduce_only=False
        )

        assert order_id == 123456789
        assert session.last_method == "POST"
        assert session.last_url.split("?")[0].endswith("/uapi/v1/trade/order")
        # timestamp gehoert in die Query-String, NICHT in den Body (siehe
        # Modul-Docstring "FUTURES WRITE INTEGRATION").
        assert "?timestamp=" in session.last_url
        assert "timestamp" not in session.last_data
        assert "PIONEX-SIGNATURE" in session.last_headers
        assert session.last_headers["Content-Type"] == "application/json"

    def test_create_futures_order_body_contains_exact_fields(self) -> None:
        import json

        session = FakeSession(FakeResponse(_ok_payload({"orderId": 1})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.create_futures_order(
            "BTC_USDT_PERP",
            "SELL",
            "LIMIT",
            size="0.02",
            price="65000",
            reduce_only=True,
            client_order_id="abc-123",
        )

        body = json.loads(session.last_data)
        assert body == {
            "clientOrderId": "abc-123",
            "symbol": "BTC_USDT_PERP",
            "side": "SELL",
            "type": "LIMIT",
            "size": "0.02",
            "price": "65000",
            "reduceOnly": True,
        }

    def test_create_futures_order_omits_none_fields_from_body(self) -> None:
        import json

        session = FakeSession(FakeResponse(_ok_payload({"orderId": 1})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.create_futures_order("BTC_USDT_PERP", "BUY", "MARKET_QTY", size="0.01")

        body = json.loads(session.last_data)
        assert "clientOrderId" not in body
        assert "price" not in body
        assert "reduceOnly" not in body

    def test_create_futures_order_signature_covers_the_sent_body(self) -> None:
        """Die Signatur muss ueber EXAKT denselben String gebildet werden,
        der auch gesendet wird - kein zweiter, unabhaengig serialisierter
        Body (siehe _private_write() Docstring)."""
        session = FakeSession(FakeResponse(_ok_payload({"orderId": 1})))
        client = PionexClient(session=session, api_key="k", api_secret="mysecret")

        client.create_futures_order("BTC_USDT_PERP", "BUY", "MARKET_QTY", size="0.01")

        sent_body = session.last_data
        query_string = session.last_url.split("?", 1)[1]
        expected_signature = hmac.new(
            b"mysecret",
            f"POST/uapi/v1/trade/order?{query_string}{sent_body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert session.last_headers["PIONEX-SIGNATURE"] == expected_signature

    def test_create_futures_order_missing_order_id_raises(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexAPIError, match="missing orderId"):
            client.create_futures_order("BTC_USDT_PERP", "BUY", "MARKET_QTY", size="0.01")

    def test_create_futures_order_requires_credentials(self) -> None:
        client = PionexClient(session=FakeSession())
        with pytest.raises(PionexAuthenticationError):
            client.create_futures_order("BTC_USDT_PERP", "BUY", "MARKET_QTY", size="0.01")

    def test_cancel_futures_order_uses_delete_with_symbol_and_order_id(self) -> None:
        import json

        session = FakeSession(FakeResponse({"result": True, "timestamp": 1}))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.cancel_futures_order("BTC_USDT_PERP", 42)

        assert session.last_method == "DELETE"
        assert session.last_url.split("?")[0].endswith("/uapi/v1/trade/order")
        assert json.loads(session.last_data) == {"symbol": "BTC_USDT_PERP", "orderId": 42}

    def test_cancel_futures_order_not_found_raises_api_error(self) -> None:
        session = FakeSession(
            FakeResponse({"result": False, "code": "TRADE_ORDER_NOT_FOUND", "message": "not found"})
        )
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexAPIError) as exc_info:
            client.cancel_futures_order("BTC_USDT_PERP", 999999)
        assert exc_info.value.code == "TRADE_ORDER_NOT_FOUND"

    def test_cancel_all_futures_orders_requires_symbol_in_body(self) -> None:
        import json

        session = FakeSession(FakeResponse({"result": True, "timestamp": 1}))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        client.cancel_all_futures_orders("ETH_USDT_PERP")

        assert session.last_method == "DELETE"
        assert session.last_url.split("?")[0].endswith("/uapi/v1/trade/allOrders")
        assert json.loads(session.last_data) == {"symbol": "ETH_USDT_PERP"}

    def test_set_futures_leverage_uses_post_with_string_leverage(self) -> None:
        import json

        session = FakeSession(
            FakeResponse(_ok_payload({"symbol": "BTC_USDT_PERP", "leverage": "10"}))
        )
        client = PionexClient(session=session, api_key="k", api_secret="s")

        result = client.set_futures_leverage("BTC_USDT_PERP", "10")

        assert result["leverage"] == "10"
        assert session.last_method == "POST"
        assert session.last_url.split("?")[0].endswith("/uapi/v1/account/leverage")
        assert json.loads(session.last_data) == {"symbol": "BTC_USDT_PERP", "leverage": "10"}

    def test_get_futures_order_by_client_id_uses_get_with_query_params(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"orderId": 42, "status": "OPEN"})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        data = client.get_futures_order_by_client_id("BTC_USDT_PERP", "abc-123")

        assert data["orderId"] == 42
        assert session.last_method == "GET"
        assert "symbol=BTC_USDT_PERP" in session.last_url
        assert "clientOrderId=abc-123" in session.last_url

    def test_get_futures_order_by_client_id_not_found_raises(self) -> None:
        session = FakeSession(
            FakeResponse({"result": False, "code": "TRADE_ORDER_NOT_FOUND", "message": "not found"})
        )
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexAPIError) as exc_info:
            client.get_futures_order_by_client_id("BTC_USDT_PERP", "no-such-id")
        assert exc_info.value.code == "TRADE_ORDER_NOT_FOUND"

    def test_write_methods_require_credentials(self) -> None:
        client = PionexClient(session=FakeSession())
        with pytest.raises(PionexAuthenticationError):
            client.cancel_futures_order("BTC_USDT_PERP", 1)
        with pytest.raises(PionexAuthenticationError):
            client.cancel_all_futures_orders("BTC_USDT_PERP")
        with pytest.raises(PionexAuthenticationError):
            client.set_futures_leverage("BTC_USDT_PERP", "5")

    def test_write_methods_map_http_429_to_rate_limit(self) -> None:
        session = FakeSession(FakeResponse({}, status_code=429))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexHTTPError) as exc_info:
            client.set_futures_leverage("BTC_USDT_PERP", "5")
        assert exc_info.value.status_code == 429

    def test_write_methods_map_malformed_json_response(self) -> None:
        class BadJsonResponse(FakeResponse):
            def json(self):
                raise ValueError("not json")

        session = FakeSession(BadJsonResponse({}))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexAPIError, match="invalid JSON"):
            client.cancel_futures_order("BTC_USDT_PERP", 1)

    def test_write_methods_map_connection_error(self) -> None:
        session = FakeSession(exception=requests.ConnectionError("network down"))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexHTTPError) as exc_info:
            client.create_futures_order("BTC_USDT_PERP", "BUY", "MARKET_QTY", size="0.01")
        assert exc_info.value.status_code == 0


class TestInvalidCredentialErrorMapping:
    """Der Client selbst wirft nur PionexAPIError mit dem von Pionex
    gemeldeten Code (kein eigenes Klassifizieren) - siehe
    sgr.exchanges.pionex._map_pionex_error() fuer die adapterseitige
    Klassifizierung in ExchangeAuthenticationError."""

    @pytest.mark.parametrize(
        "code",
        ["INVALID_APIKEY", "INVALID_SIGNATURE", "APIKEY_EXPIRED", "IP_NOT_WHITELISTED"],
    )
    def test_pionex_reports_auth_error_code(self, code) -> None:
        session = FakeSession(
            FakeResponse({"result": False, "code": code, "message": "auth failed"})
        )
        client = PionexClient(session=session, api_key="bad-key", api_secret="bad-secret")

        with pytest.raises(PionexAPIError) as exc_info:
            client.get_account_balances()

        assert exc_info.value.code == code


class TestRateLimit:
    def test_http_429_raises_pionex_http_error_with_status_code(self) -> None:
        session = FakeSession(FakeResponse({}, status_code=429))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexHTTPError) as exc_info:
            client.get_account_balances()

        assert exc_info.value.status_code == 429


class TestMalformedResponse:
    def test_invalid_json_raises_api_error(self) -> None:
        class BadJsonResponse(FakeResponse):
            def json(self):
                raise ValueError("not json")

        session = FakeSession(BadJsonResponse({}))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexAPIError, match="invalid JSON"):
            client.get_account_balances()

    def test_non_mapping_payload_raises_api_error(self) -> None:
        session = FakeSession(FakeResponse(["not", "a", "dict"]))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexAPIError, match="invalid response object"):
            client.get_account_balances()

    def test_missing_positions_array_raises_api_error(self) -> None:
        session = FakeSession(FakeResponse(_ok_payload({"positions": "not-a-list"})))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexAPIError, match="invalid positions data"):
            client.get_futures_positions()


class TestExchangeUnavailable:
    def test_connection_error_raises_http_error_with_zero_status(self) -> None:
        session = FakeSession(exception=requests.ConnectionError("network down"))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexHTTPError) as exc_info:
            client.get_account_balances()

        assert exc_info.value.status_code == 0

    def test_timeout_raises_http_error(self) -> None:
        session = FakeSession(exception=requests.Timeout("timed out"))
        client = PionexClient(session=session, api_key="k", api_secret="s")

        with pytest.raises(PionexHTTPError):
            client.get_futures_positions()
