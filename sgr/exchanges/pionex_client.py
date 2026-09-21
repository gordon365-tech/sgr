"""Synchronous client for Pionex's REST API - public market data AND
signed private endpoints: account balances, positions, leverage, margin
mode, open orders, order status, funding fee history (read), plus order
placement/cancellation and leverage-set (write, Futures Write
Integration - see module section below).

The client deliberately stays independent from the CCXT adapter.  It is useful
for exchange specific market endpoints where the SGR exchange abstraction needs
raw Pionex data or where CCXT does not expose an endpoint directly.

Verified against Pionex's official OpenAPI specification
(https://github.com/pionex-official/pionex-open-api, fetched 2026-09-20
for the read-only integration and re-fetched 2026-09-21 for the write
endpoints below - openapi.yaml for spot/general, openapi_futures.yaml
for futures) and the "Authentication" page of
https://pionex-doc.gitbook.io/apidocs. The worked signing example on
that page was cross-checked byte-for-byte against this implementation's
`_sign()` (see there for the one documentation inconsistency found and
how it was resolved).

FUTURES WRITE INTEGRATION (2026-09-21) - verifizierte Endpoint-Details:
    Quelle: openapi_futures.yaml (raw fetch von
    raw.githubusercontent.com/pionex-official/pionex-open-api/master/
    openapi_futures.yaml), Abschnitte `/uapi/v1/trade/order` (POST/DELETE),
    `/uapi/v1/trade/allOrders` (DELETE), `/uapi/v1/account/leverage`
    (POST), `/uapi/v1/trade/orderByClientOrderId` (GET).

    - POST/DELETE-Request-Body wird laut Doku-Intro ALS TEIL DER
      SIGNATUR mitgesendet ("POST/DELETE: METHOD + PATH_URL + QUERY +
      TIMESTAMP + body") - im Unterschied zu GET (siehe _sign() unten
      fuer beide Faelle). `timestamp` bleibt laut Parameter-Definition
      (`in: query`) AUCH bei POST/DELETE ein Query-Parameter, nicht Teil
      des JSON-Bodys - nur die eigentlichen Order-Felder gehen in den
      Body.
    - Order-Typen (`type`-Feld): `[LIMIT, MARKET_QTY, IOC, FOK,
      POSTONLY]` - Pionex kennt KEIN einfaches "MARKET", Markt-Orders
      heissen explizit `MARKET_QTY` (quantity-basiert). Ein `amount`
      (quote-waehrungsbasierter Markt-Kauf) existiert NICHT im
      Request-Schema fuer Futures-Orders (nur im Order-Response-Schema
      als Altfeld) - `size` (Basiswaehrungs-Menge) ist das einzige
      Mengenfeld im Request.
    - `positionSide` (`BOTH`/`LONG`/`SHORT`): "Required for hedge mode
      ... Defaults to BOTH for one-way mode." Wird von diesem Client
      bewusst NICHT gesendet (siehe sgr.exchanges.capabilities: Pionex
      `supports_hedge_mode=False` - SGR nutzt Pionex ausschliesslich im
      Default-One-Way-Modus).
    - `reduceOnly` (bool): "Only applicable in one-way mode (BUYSELL).
      Defaults to false." - direkt uebernehmbar aus SGRs
      `OrderRequest.reduce_only`.
    - `clientOrderId`: "Max 64 characters, alphanumeric and hyphen
      only", vom Server 2 Stunden lang fuer Deduplizierung geprueft -
      eine SGR-`order.id`-UUID (36 Zeichen, nur Hex+Bindestriche) passt
      exakt in diese Beschraenkung.
    - Create-Order-Response enthaelt NUR `{"orderId": <int64>}` (Order
      ist async/nicht sofort final) - der tatsaechliche Fuellstatus muss
      per `get_futures_order()` (bereits vorhanden) separat abgefragt
      werden.
    - Cancel-Order/Cancel-All-Response ist ein reiner `BaseResponse`
      (nur `result`/`timestamp`, kein `data`) - kein Rueckschluss auf
      "wie viele wurden storniert" aus der Antwort selbst moeglich.
    - `DELETE /uapi/v1/trade/allOrders` verlangt `symbol` als PFLICHT-
      Feld - es gibt KEINE "alle Symbole gleichzeitig"-Variante dieses
      Endpunkts (anders als bei manchen anderen Exchanges/ccxt).
    - Kein vollstaendiger Trade-spezifischer Fehlercode-Katalog in der
      Doku gefunden (ueber die bereits bekannten Auth-Codes und
      `TRADE_INVALID_SYMBOL`/`TRADE_INVAILD_SYMBOL` [dokumentierter
      Tippfehler, beide Schreibweisen vorhanden]/`TRADE_ORDER_NOT_FOUND`
      /`TRADE_PARAMETER_ERROR` hinaus) - siehe pionex.py `_map_pionex_error()`
      fuer die defensive Behandlung unbekannter Codes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

import requests


class PionexAPIError(RuntimeError):
    """Raised when Pionex returns an application level API error."""

    def __init__(self, message: str, code: str | int | None = None) -> None:
        super().__init__(message)
        self.code = code


class PionexHTTPError(RuntimeError):
    """Raised when Pionex returns an unsuccessful HTTP status code."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class PionexAuthenticationError(RuntimeError):
    """
    Raised when a private-endpoint call is attempted without configured
    api_key/api_secret. NOT raised for a rejected signature/expired key -
    that comes back as a normal PionexAPIError with a Pionex error code
    (e.g. "INVALID_SIGNATURE"); see sgr.exchanges.pionex for how the
    adapter layer classifies those into ExchangeAuthenticationError.
    This exception only covers the client-side precondition "no
    credentials configured at all".
    """


class PionexClient:
    """Synchronous client for Pionex REST endpoints - public market data
    (no authentication) and, when constructed with api_key/api_secret,
    signed private endpoints (read-only - see module docstring).

    ``requests.Session`` is reused so callers can keep connection pooling and
    configure a proxy or transport for tests without changing the client API.
    """

    BASE_URL = "https://api.pionex.com"
    TIMEOUT = 10

    # Private-Endpunkt-Fenster laut Pionex-Dokumentation: ein timestamp
    # aelter als 20000ms oder in der Zukunft wird als INVALID_TIMESTAMP
    # abgelehnt. Rein informativ hier (keine eigene Client-seitige
    # Pruefung) - der Server ist die Quelle der Wahrheit.
    SIGNATURE_TIMESTAMP_WINDOW_MS = 20_000

    KLINE_INTERVALS: dict[str, str] = {
        "1m": "1M",
        "5m": "5M",
        "15m": "15M",
        "30m": "30M",
        "1h": "60M",
        "4h": "4H",
        "8h": "8H",
        "12h": "12H",
        "1d": "1D",
        "1w": "1W",
        "1mo": "1m",
    }

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = TIMEOUT,
        session: requests.Session | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")

        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "SGR-PionexClient/1.0",
            }
        )
        # Nur fuer PRIVATE (signierte, read-only) Endpunkte benoetigt -
        # oeffentliche Marktdaten-Methoden funktionieren unveraendert ohne
        # diese Parameter (api_key/api_secret bleiben None).
        self.api_key = api_key
        self.api_secret = api_secret

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def __enter__(self) -> PionexClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            raise PionexHTTPError(
                status_code,
                f"Pionex HTTP {status_code}: {exc}",
            ) from exc
        except requests.RequestException as exc:
            raise PionexHTTPError(0, f"Pionex request failed: {exc}") from exc

        return self._handle_response(response)

    def _write(
        self,
        method: str,
        path: str,
        body: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Gemeinsamer POST/DELETE-Versand (Order erstellen/stornieren,
        Leverage setzen). `body` ist bereits ein fertig serialisierter
        JSON-String (siehe `_private_write()`) - wird ALS LITERALER
        STRING gesendet (`data=`, nicht `json=`), damit garantiert exakt
        dieselben Bytes uebertragen werden, die auch signiert wurden
        (kein zweiter, unabhaengig von `requests` serialisierter Body,
        der von der Signatur abweichen koennte).
        """
        session_method = getattr(self.session, method.lower())
        try:
            response = session_method(
                f"{self.base_url}{path}",
                data=body,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            raise PionexHTTPError(
                status_code,
                f"Pionex HTTP {status_code}: {exc}",
            ) from exc
        except requests.RequestException as exc:
            raise PionexHTTPError(0, f"Pionex request failed: {exc}") from exc

        return self._handle_response(response)

    @staticmethod
    def _handle_response(response: requests.Response) -> dict[str, Any]:
        """Gemeinsame Response-Auswertung fuer GET/POST/DELETE (siehe
        _get()/_write()): JSON-Parsing, Envelope-Validierung,
        result==false -> PionexAPIError mit Pionex-Fehlercode."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise PionexAPIError("Pionex returned invalid JSON") from exc

        if not isinstance(payload, Mapping):
            raise PionexAPIError("Pionex returned an invalid response object")

        if payload.get("result") is not True:
            code = payload.get("code", "UNKNOWN")
            message = payload.get("message", "Unknown Pionex API error")
            raise PionexAPIError(f"{code}: {message}", code=code)

        return dict(payload)

    @staticmethod
    def _data(payload: Mapping[str, Any]) -> dict[str, Any]:
        data = payload.get("data", {})
        if not isinstance(data, Mapping):
            raise PionexAPIError("Pionex returned an invalid data object")
        return dict(data)

    def get_symbols(
        self,
        symbols: str | None = None,
        market_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return Pionex symbol metadata.

        market_type: "SPOT" or "PERP" (defaults to SPOT server-side when
        omitted, per Pionex docs) - pass "PERP" to list Futures
        Instruments (Aufgabenstellung Punkt 4). symbols: comma-separated
        list to restrict the result to specific pairs.
        """
        params: dict[str, Any] = {}
        if symbols is not None:
            params["symbols"] = symbols
        if market_type is not None:
            normalized_type = market_type.upper()
            if normalized_type not in {"SPOT", "PERP"}:
                raise ValueError("market_type must be SPOT or PERP")
            params["type"] = normalized_type
        if status is not None:
            params["status"] = status

        payload = self._get("/api/v1/common/symbols", params or None)
        result = self._data(payload).get("symbols") or []
        if not isinstance(result, list):
            raise PionexAPIError("Pionex returned invalid symbols data")
        return result

    def get_futures_risk_table(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """
        Public endpoint (no auth): leverage tiers / maintenance-margin
        requirements per futures symbol
        (GET /api/v1/common/riskTable). Complements get_symbols(market_
        type="PERP") for Futures-Instrument-Metadaten (max. Leverage pro
        Notional-Stufe) - Punkt 4/9 der Aufgabenstellung.
        """
        params = {"symbol": symbol} if symbol is not None else None
        payload = self._get("/api/v1/common/riskTable", params)
        rows = self._data(payload).get("rows") or self._data(payload).get("riskTable")
        if rows is None:
            # Manche Pionex-Antworten verschachteln pro Symbol - Rohdaten
            # unveraendert zurueckgeben statt eine Form zu erzwingen, die
            # nicht verifiziert ist.
            return [self._data(payload)]
        if not isinstance(rows, list):
            raise PionexAPIError("Pionex returned invalid risk table data")
        return rows

    def get_funding_rates(
        self,
        symbol: str,
        end_time: int | None = None,
        limit: int = 1,
    ) -> dict[str, Any]:
        """
        Public endpoint (no auth): historical funding rates for a
        perpetual futures symbol (GET /api/v1/market/fundingRates).
        Punkt 8 der Aufgabenstellung.
        """
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        params: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if end_time is not None:
            params["endTime"] = end_time
        payload = self._get("/api/v1/market/fundingRates", params)
        return self._data(payload)

    def get_open_interests(self) -> list[dict[str, Any]]:
        """
        Public endpoint (no auth): open interest for all futures symbols
        (GET /api/v1/market/openInterests). Punkt 4/8 der Aufgabenstellung.
        """
        payload = self._get("/api/v1/market/openInterests")
        rows = self._data(payload).get("openInterests") or []
        if not isinstance(rows, list):
            raise PionexAPIError("Pionex returned invalid open interest data")
        return rows

    # ------------------------------------------------------------------
    # Private (signed) request helpers - shared by both the READ methods
    # below and the WRITE methods further down (order placement/
    # cancellation/leverage-set, see module docstring "FUTURES WRITE
    # INTEGRATION"). Margin-mode-SET still deliberately does not exist
    # on this client (out of scope, see sgr/exchanges/pionex.py
    # module docstring).
    # ------------------------------------------------------------------

    @staticmethod
    def _signed_query_string(params: dict[str, Any]) -> str:
        """
        Sortiert Key-Value-Paare in aufsteigender ASCII-Ordnung nach Key
        und verkettet sie mit '&' (Pionex Signing-Schritt 2+3) - Werte
        werden NICHT URL-encodiert (die Signatur wird ueber die rohen
        Werte gebildet, siehe Modul-Docstring "Verified against..." fuer
        die Gegenprobe anhand des offiziellen Doku-Beispiels). None-Werte
        werden ausgelassen (optionale Parameter).
        """
        items = sorted((str(k), str(v)) for k, v in params.items() if v is not None)
        return "&".join(f"{k}={v}" for k, v in items)

    def _sign(self, method: str, path: str, query_string: str, body: str = "") -> str:
        """
        HMAC-SHA256(secret, METHOD + PATH + '?' + query_string [+ body]),
        hex-codiert (Pionex Signing-Schritt 4-8).

        GET (body=""): Body-Anteil wird bewusst NICHT angehaengt - LIVE
        gegen einen echten Account verifiziert (Pionex Live Read-Only
        Verification, 2026-09-20/21: 20/20 signierte GET-Requests
        erfolgreich, u.a. Account/Balance/Positions/Orders) - die frueher
        hier dokumentierte Unsicherheit ist damit ausgeraeumt.

        POST/DELETE (body=<JSON-String>): Body WIRD angehaengt - laut
        Doku-Intro in openapi_futures.yaml ("POST/DELETE: METHOD +
        PATH_URL + QUERY + TIMESTAMP + body") und der Schritt-Anleitung
        ("Concatenate related entity body of POST and DELETE after step
        5. Skip this step if there is no entity body") - fuer POST/DELETE
        existiert immer ein Body (Order-Felder), der Schritt wird hier
        NICHT uebersprungen. Noch NICHT live gegen einen echten Account
        verifiziert (siehe Aufgabenstellung "verbleibende
        Unsicherheiten") - nur gegen die Dokumentation und offline
        Fake-Tests.
        """
        if not self.api_secret:
            raise PionexAuthenticationError(
                "Pionex API secret not configured - cannot sign private request"
            )
        prehash = f"{method}{path}"
        if query_string:
            prehash += f"?{query_string}"
        prehash += body
        return hmac.new(
            self.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _private_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Signierter GET-Request gegen einen privaten Pionex-Endpunkt.
        Baut die vollstaendige, signierte Query-String selbst (statt sie
        `requests` per params= bauen zu lassen) - die Signatur MUSS ueber
        exakt dieselbe Query-String gebildet werden, die auch tatsaechlich
        gesendet wird; ein zweiter, unabhaengig von `requests` gebauter
        Query-String schliesst jede Diskrepanz (Reihenfolge, Encoding)
        zwischen signiertem und gesendetem Request aus.
        """
        if not self.api_key or not self.api_secret:
            raise PionexAuthenticationError(
                "Pionex api_key/api_secret not configured for this client instance"
            )

        query_params = dict(params or {})
        query_params["timestamp"] = str(int(time.time() * 1000))
        query_string = self._signed_query_string(query_params)
        signature = self._sign("GET", path, query_string)

        full_path = f"{path}?{query_string}" if query_string else path
        headers = {"PIONEX-KEY": self.api_key, "PIONEX-SIGNATURE": signature}
        return self._get(full_path, params=None, headers=headers)

    def _private_write(self, method: str, path: str, body_params: dict[str, Any]) -> dict[str, Any]:
        """
        Signierter POST/DELETE-Request (Order erstellen/stornieren,
        Leverage setzen). `timestamp` geht - wie bei GET - in die
        Query-String, NICHT in den Body (siehe openapi_futures.yaml:
        `timestamp` ist ueberall `in: query`, auch fuer POST/DELETE, nur
        die eigentlichen Order-/Leverage-Felder gehen in den JSON-Body).

        Der Body wird EINMAL als kompakter JSON-String serialisiert und
        danach identisch fuer die Signatur UND als tatsaechlich
        gesendeter Request-Body verwendet (`_write()`) - analog zur
        Query-String-Begruendung in `_private_get()`: kein zweiter,
        unabhaengig serialisierter Body, der von der Signatur abweichen
        koennte. None-Werte werden vor der Serialisierung entfernt
        (optionale Felder wie `clientOrderId`/`price`/`reduceOnly`).
        """
        if not self.api_key or not self.api_secret:
            raise PionexAuthenticationError(
                "Pionex api_key/api_secret not configured for this client instance"
            )

        query_params = {"timestamp": str(int(time.time() * 1000))}
        query_string = self._signed_query_string(query_params)

        clean_body = {k: v for k, v in body_params.items() if v is not None}
        body_str = json.dumps(clean_body, separators=(",", ":"))

        signature = self._sign(method, path, query_string, body=body_str)

        full_path = f"{path}?{query_string}"
        headers = {
            "PIONEX-KEY": self.api_key,
            "PIONEX-SIGNATURE": signature,
            "Content-Type": "application/json",
        }
        return self._write(method, full_path, body_str, headers=headers)

    # ------------------------------------------------------------------
    # Private (signed) WRITE endpoints - Futures only. Verified against
    # openapi_futures.yaml (siehe Modul-Docstring "FUTURES WRITE
    # INTEGRATION"), NICHT gegen einen echten Account (siehe
    # Aufgabenstellung: "KEINE echten Pionex Write Requests ausfuehren"
    # fuer diese Session - nur Fake-Client-Tests).
    # ------------------------------------------------------------------

    def create_futures_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: str | None = None,
        price: str | None = None,
        reduce_only: bool | None = None,
        client_order_id: str | None = None,
    ) -> int:
        """
        Platziert eine Futures-Order (POST /uapi/v1/trade/order, signiert).

        side: "BUY" | "SELL". order_type: "LIMIT" | "MARKET_QTY" | "IOC"
        | "FOK" | "POSTONLY" - Pionex kennt KEIN einfaches "MARKET",
        siehe Modul-Docstring. size: Basiswaehrungs-Menge (fuer JEDEN
        Order-Typ, es existiert kein separates quote-waehrungsbasiertes
        `amount`-Feld im Request). price: nur fuer LIMIT-Orders.
        reduce_only: direkt aus OrderRequest.reduce_only uebernehmbar.
        client_order_id: max. 64 Zeichen, nur alphanumerisch/Bindestrich
        - eine SGR order.id-UUID passt.

        positionSide wird bewusst NICHT gesendet (Default BOTH fuer
        One-Way-Modus, siehe Modul-Docstring).

        Returns: die von Pionex vergebene numerische orderId. Die
        Antwort enthaelt sonst nichts (Order ist async) - der
        tatsaechliche Fuellstatus muss separat per get_futures_order()
        abgefragt werden.
        """
        body = {
            "clientOrderId": client_order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "size": size,
            "price": price,
            "reduceOnly": reduce_only,
        }
        payload = self._private_write("POST", "/uapi/v1/trade/order", body)
        data = self._data(payload)
        order_id = data.get("orderId")
        if order_id is None:
            raise PionexAPIError("Pionex order response missing orderId")
        try:
            return int(order_id)
        except (TypeError, ValueError) as exc:
            raise PionexAPIError(
                f"Pionex order response has non-numeric orderId: {order_id!r}"
            ) from exc

    def get_futures_order_by_client_id(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        """
        Order-Lookup per clientOrderId (GET /uapi/v1/trade/orderByClientOrderId,
        signiert) - fuer eine Exchange-seitige Idempotenz-Pruefung VOR
        einer Order-Neuerstellung (analog zu CCXTBaseAdapter.
        _find_existing_order_by_client_id() fuer Binance), damit ein
        Retry mit identischer order.id nicht versehentlich eine zweite
        echte Order erzeugt. Wirft PionexAPIError (typischerweise
        TRADE_ORDER_NOT_FOUND), wenn (noch) keine Order mit dieser
        clientOrderId existiert - vom Aufrufer als "keine Duplikat-Order
        vorhanden" zu behandeln, nicht als harter Fehler.
        """
        payload = self._private_get(
            "/uapi/v1/trade/orderByClientOrderId",
            {"symbol": symbol, "clientOrderId": client_order_id},
        )
        return self._data(payload)

    def cancel_futures_order(self, symbol: str, order_id: int) -> None:
        """
        Storniert eine einzelne Futures-Order (DELETE /uapi/v1/trade/order,
        signiert). Response ist ein reiner BaseResponse (kein `data`-Feld,
        siehe Modul-Docstring) - ein erfolgreicher Aufruf bedeutet
        lediglich "Stornierung angenommen" (async), nicht zwingend
        "sofort storniert". Wirft PionexAPIError (typischerweise
        TRADE_ORDER_NOT_FOUND), wenn die Order nicht existiert oder
        bereits terminiert (gefuellt/storniert) ist - der Aufrufer
        (PionexAdapter.cancel_order) bildet das auf das Protocol-
        vertraute False/OrderNotFoundError-Verhalten ab.
        """
        self._private_write(
            "DELETE", "/uapi/v1/trade/order", {"symbol": symbol, "orderId": order_id}
        )

    def cancel_all_futures_orders(self, symbol: str) -> None:
        """
        Storniert alle offenen Futures-Orders EINES Symbols (DELETE
        /uapi/v1/trade/allOrders, signiert). `symbol` ist laut
        Spezifikation PFLICHT - es existiert KEINE "alle Symbole
        gleichzeitig"-Variante dieses Endpunkts (siehe Modul-Docstring);
        der Aufrufer muss fuer eine account-weite Stornierung ueber alle
        offenen Symbole selbst iterieren (siehe PionexAdapter.
        cancel_all_orders(), analog zum bestehenden CCXTBaseAdapter-
        Fallback-Muster fuer Exchanges ohne globalen Cancel).
        """
        self._private_write("DELETE", "/uapi/v1/trade/allOrders", {"symbol": symbol})

    def set_futures_leverage(self, symbol: str, leverage: str) -> dict[str, Any]:
        """
        Setzt die Account-Leverage fuer ein Futures-Symbol (POST
        /uapi/v1/account/leverage, signiert). `leverage` als String
        (laut Schema, wie beim bereits vorhandenen Read-Pfad
        get_futures_leverage()).
        """
        payload = self._private_write(
            "POST", "/uapi/v1/account/leverage", {"symbol": symbol, "leverage": leverage}
        )
        return self._data(payload)

    # ------------------------------------------------------------------
    # Private (signed) READ endpoints.
    # ------------------------------------------------------------------

    def get_account_balances(self) -> list[dict[str, Any]]:
        """Spot account balances (GET /api/v1/account/balances, signed).
        Punkt 2 der Aufgabenstellung."""
        payload = self._private_get("/api/v1/account/balances")
        balances = self._data(payload).get("balances") or []
        if not isinstance(balances, list):
            raise PionexAPIError("Pionex returned invalid balances data")
        return balances

    def get_spot_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        """
        Spot open orders (GET /api/v1/trade/openOrders, signed). `symbol`
        is REQUIRED by this endpoint (verified against the official spec -
        unlike the futures equivalent, there is no "all symbols" spot
        variant). Punkt 6 der Aufgabenstellung.
        """
        payload = self._private_get("/api/v1/trade/openOrders", {"symbol": symbol})
        orders = self._data(payload).get("orders") or []
        if not isinstance(orders, list):
            raise PionexAPIError("Pionex returned invalid open orders data")
        return orders

    def get_spot_order(self, order_id: int) -> dict[str, Any]:
        """Spot single order lookup by orderId (GET /api/v1/trade/order,
        signed) - no `symbol` parameter required for spot. Punkt 7 der
        Aufgabenstellung."""
        payload = self._private_get("/api/v1/trade/order", {"orderId": order_id})
        return self._data(payload)

    def get_futures_account_balances(self) -> dict[str, Any]:
        """
        Futures account balances, cross AND isolated
        (GET /uapi/v1/account/balances, signed) - returns the raw
        {"balances": [...], "isolates": [...]} structure (isolated-margin
        balances are per-symbol, richer than the generic SGR Balance
        domain type - see sgr.exchanges.pionex for how this is mapped).
        Punkt 3 der Aufgabenstellung.
        """
        payload = self._private_get("/uapi/v1/account/balances")
        return self._data(payload)

    def get_futures_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Current futures positions (GET /uapi/v1/account/positions,
        signed). Punkt 5 der Aufgabenstellung."""
        params = {"symbol": symbol} if symbol is not None else None
        payload = self._private_get("/uapi/v1/account/positions", params)
        positions = self._data(payload).get("positions") or []
        if not isinstance(positions, list):
            raise PionexAPIError("Pionex returned invalid positions data")
        return positions

    def get_futures_leverage(self, symbol: str) -> dict[str, Any]:
        """Current account leverage for a futures symbol
        (GET /uapi/v1/account/leverage, signed). Punkt 9 der
        Aufgabenstellung."""
        payload = self._private_get("/uapi/v1/account/leverage", {"symbol": symbol})
        return self._data(payload)

    def get_futures_margin_mode(self, symbol: str) -> dict[str, Any]:
        """Current cross/isolated margin mode for a futures symbol
        (GET /uapi/v1/trade/isolatedMode, signed). Punkt 9 der
        Aufgabenstellung."""
        payload = self._private_get("/uapi/v1/trade/isolatedMode", {"symbol": symbol})
        return self._data(payload)

    def get_futures_open_orders(
        self,
        symbol: str | None = None,
        end_time: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Futures open orders (GET /uapi/v1/trade/openOrders, signed) -
        `symbol` is OPTIONAL here (unlike the spot equivalent). Punkt 6
        der Aufgabenstellung."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        params: dict[str, Any] = {"limit": limit}
        if symbol is not None:
            params["symbol"] = symbol
        if end_time is not None:
            params["endTime"] = end_time
        payload = self._private_get("/uapi/v1/trade/openOrders", params)
        orders = self._data(payload).get("orders") or []
        if not isinstance(orders, list):
            raise PionexAPIError("Pionex returned invalid open orders data")
        return orders

    def get_futures_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        """Futures single order lookup (GET /uapi/v1/trade/order, signed)
        - `symbol` AND `orderId` are both required (unlike spot). Punkt 7
        der Aufgabenstellung."""
        payload = self._private_get("/uapi/v1/trade/order", {"symbol": symbol, "orderId": order_id})
        return self._data(payload)

    def get_futures_funding_fees(
        self,
        symbol: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Historical funding fee PAYMENTS actually charged/credited to
        this account (GET /uapi/v1/trade/fundingFee, signed) - distinct
        from get_funding_rates() (public market rate, not account-
        specific). Punkt 8 der Aufgabenstellung."""
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        params: dict[str, Any] = {"limit": limit}
        if symbol is not None:
            params["symbol"] = symbol
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        payload = self._private_get("/uapi/v1/trade/fundingFee", params)
        # LIVE-verifiziert (kapitalarmer Account ohne Funding-Historie):
        # Pionex liefert hier `"fundings": null` statt `[]`, wenn keine
        # Funding-Zahlungen vorliegen - `dict.get(key, [])` greift NUR,
        # wenn der Key fehlt, nicht wenn er explizit auf null steht, und
        # haette das faelschlich als "invalid funding fee data" gemeldet
        # (siehe `... or []` unten sowie identischer Fix fuer alle
        # anderen Listen-Felder in dieser Datei - der Malformed-Response-
        # Schutz selbst bleibt unveraendert: ein echter Nicht-Listen-Wert
        # wie ein String oder Dict wird weiterhin abgelehnt).
        fundings = self._data(payload).get("fundings") or []
        if not isinstance(fundings, list):
            raise PionexAPIError("Pionex returned invalid funding fee data")
        return fundings

    def get_tickers(
        self,
        symbol: str | None = None,
        market_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return 24 hour tickers, optionally filtered by symbol and type."""
        params: dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol
        if market_type is not None:
            normalized_type = market_type.upper()
            if normalized_type not in {"SPOT", "PERP"}:
                raise ValueError("market_type must be SPOT or PERP")
            params["type"] = normalized_type

        payload = self._get("/api/v1/market/tickers", params or None)
        tickers = self._data(payload).get("tickers") or []
        if not isinstance(tickers, list):
            raise PionexAPIError("Pionex returned invalid tickers data")
        return tickers

    def get_ticker(self, symbol: str = "BTC_USDT") -> dict[str, Any]:
        """Return the 24 hour ticker for exactly one symbol."""
        tickers = self.get_tickers(symbol=symbol)
        if not tickers:
            raise PionexAPIError(f"No ticker returned for symbol {symbol}")
        return tickers[0]

    def get_book_tickers(
        self,
        symbol: str | None = None,
        market_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return best bid and ask prices."""
        params: dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol
        if market_type is not None:
            normalized_type = market_type.upper()
            if normalized_type not in {"SPOT", "PERP"}:
                raise ValueError("market_type must be SPOT or PERP")
            params["type"] = normalized_type

        payload = self._get("/api/v1/market/bookTickers", params or None)
        tickers = self._data(payload).get("tickers") or []
        if not isinstance(tickers, list):
            raise PionexAPIError("Pionex returned invalid book tickers data")
        return tickers

    def get_orderbook(
        self,
        symbol: str = "BTC_USDT",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return the current order book snapshot."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

        payload = self._get(
            "/api/v1/market/depth",
            {"symbol": symbol, "limit": limit},
        )
        return self._data(payload)

    def get_trades(
        self,
        symbol: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent spot or futures trades for a symbol."""
        if not 10 <= limit <= 500:
            raise ValueError("limit must be between 10 and 500")

        payload = self._get(
            "/api/v1/market/trades",
            {"symbol": symbol, "limit": limit},
        )
        trades = self._data(payload).get("trades") or []
        if not isinstance(trades, list):
            raise PionexAPIError("Pionex returned invalid trades data")
        return trades

    def get_ohlcv(
        self,
        symbol: str = "BTC_USDT",
        interval: str = "1m",
        limit: int = 100,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return OHLCV klines using SGR friendly interval names."""
        pionex_interval = self.KLINE_INTERVALS.get(interval.lower())
        if pionex_interval is None:
            valid = ", ".join(self.KLINE_INTERVALS)
            raise ValueError(f"Unsupported interval '{interval}'. Supported intervals: {valid}")

        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")

        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": pionex_interval,
            "limit": limit,
        }
        if end_time is not None:
            if end_time <= 0:
                raise ValueError("end_time must be greater than 0")
            params["endTime"] = end_time

        payload = self._get("/api/v1/market/klines", params)
        klines = self._data(payload).get("klines") or []
        if not isinstance(klines, list):
            raise PionexAPIError("Pionex returned invalid klines data")
        return klines
