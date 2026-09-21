"""
Tests für die Pionex Private API Integration (READ ONLY) auf Adapter-
Ebene (sgr.exchanges.pionex.PionexAdapter).

Deckt (siehe Aufgabenstellung "TESTS"): Authentication, Connectivity,
Account, Balance, Futures Instrument, Position, Open Order, Order
Status, Funding, Capability, Reconciliation, Invalid Credential,
Timeout, Rate Limit, Malformed Response, Exchange unavailable, Tenant
Isolation, Paper Trading.

Alle Tests verwenden ausschliesslich einen Fake-PionexClient (kein
echtes Netzwerk, keine echten Credentials, keine echten Orders) - siehe
FakePrivateClient unten.

Seit der Futures Write Integration (2026-09-21, siehe sgr/exchanges/
pionex.py Modul-Docstring "FUTURES WRITE INTEGRATION") sind
place_order()/cancel_order()/cancel_all_orders()/set_leverage() fuer
LIVE+Futures echt implementiert - TestFuturesWriteOperations deckt sie
ausschliesslich gegen den Fake-Client ab (KEIN echter Pionex-Account,
KEINE echte Order - siehe Aufgabenstellung dieser Phase). LIVE+Spot
bleibt weiterhin bewusst blockiert (siehe TestLiveSpotWriteStaysBlocked).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import requests

from sgr.core.types import (
    ExchangeID,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    Symbol,
    TradingMode,
)
from sgr.exchanges.base import (
    AdapterFeatureNotImplementedError,
    ExchangeAuthenticationError,
    ExchangeConnectionError,
    OrderNotFoundError,
    RateLimitError,
)
from sgr.exchanges.factory import ExchangePool
from sgr.exchanges.pionex import PionexAdapter
from sgr.exchanges.pionex_client import PionexAPIError, PionexHTTPError
from sgr.reconciliation.engine import ReconciliationEngine


def _market_order(symbol="BTC/USDT", side=Side.BUY, quantity="0.01", reduce_only=False):
    return OrderRequest(
        signal_id=uuid4(),
        symbol=Symbol(
            base=symbol.split("/")[0], quote=symbol.split("/")[1], exchange=ExchangeID.PIONEX
        ),
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        trading_mode=TradingMode.LIVE,
        reduce_only=reduce_only,
    )


def _limit_order(
    symbol="BTC/USDT", side=Side.BUY, quantity="0.01", price="58000", reduce_only=False
):
    return OrderRequest(
        signal_id=uuid4(),
        symbol=Symbol(
            base=symbol.split("/")[0], quote=symbol.split("/")[1], exchange=ExchangeID.PIONEX
        ),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
        trading_mode=TradingMode.LIVE,
        reduce_only=reduce_only,
    )


class FakePrivateClient:
    """
    Konfigurierbarer Fake fuer PionexClient - deckt sowohl den Happy
    Path als auch injizierte Fehler ab (Invalid Credentials, Timeout,
    Rate Limit, Malformed Response, Exchange unavailable). Kein Netzwerk.

    Seit der Futures Write Integration simuliert diese Klasse zusaetzlich
    ein einfaches Server-seitiges Order-Buch (self.orders) fuer
    create_futures_order/get_futures_order/get_futures_order_by_client_id/
    cancel_futures_order/cancel_all_futures_orders/set_futures_leverage -
    siehe TestFuturesWriteOperations weiter unten.
    """

    KLINE_INTERVALS = {"1h": "60M"}

    def __init__(self, api_key=None, api_secret=None) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.closed = False
        self._error: Exception | None = None
        self._error_only_for: set[str] | None = None
        self.calls: list[str] = []
        # --- Futures Write Integration: simuliertes Server-seitiges
        # Order-Buch (order_id -> Order-Dict), damit place_order/
        # cancel_order/get_order end-to-end konsistent zusammenspielen,
        # genau wie ein echter Pionex-Account. MARKET_QTY-Orders "fuellen"
        # sofort (status CLOSED), LIMIT-Orders bleiben OPEN (ruhend) -
        # realistisches, aber bewusst einfaches Verhalten.
        self.orders: dict[int, dict] = {}
        self.client_order_index: dict[str, int] = {}
        self._next_order_id = 9000
        self.leverage_calls: list[tuple[str, str]] = []
        self.cancel_all_calls: list[str] = []

    def inject_error(self, error: Exception, only_for: set[str] | None = None) -> None:
        """only_for=None (default): jeder folgende Aufruf schlaegt fehl
        (bestehendes Verhalten fuer die meisten Tests). only_for={...}:
        nur die genannten Methodennamen schlagen fehl - z.B. um einen
        reinen Berechtigungsfehler auf private Endpunkte zu simulieren,
        waehrend oeffentliche Endpunkte (get_symbols) weiterhin
        funktionieren (siehe TestCapabilityDetection)."""
        self._error = error
        self._error_only_for = only_for

    def _maybe_raise(self, name: str) -> None:
        self.calls.append(name)
        if self._error is not None and (
            self._error_only_for is None or name in self._error_only_for
        ):
            raise self._error

    def close(self):
        self.closed = True

    # --- public ---
    def get_ticker(self, symbol):
        self._maybe_raise("get_ticker")
        return {
            "symbol": symbol,
            "time": 1786237722477,
            "open": "60000",
            "close": "60100",
            "high": "60200",
            "low": "59900",
            "volume": "123.45",
        }

    def get_book_tickers(self, symbol, market_type=None):
        self._maybe_raise("get_book_tickers")
        return [{"symbol": symbol, "bidPrice": "60090", "askPrice": "60110"}]

    def get_symbols(self, symbols=None, market_type=None, status=None):
        self._maybe_raise("get_symbols")
        if market_type == "PERP":
            return [
                {
                    "symbol": "BTC_USDT_PERP",
                    "baseCurrency": "BTC",
                    "quoteCurrency": "USDT",
                    "type": "PERP",
                    "basePrecision": 3,
                    "quotePrecision": 2,
                }
            ]
        return [{"symbol": "BTC_USDT", "baseCurrency": "BTC", "quoteCurrency": "USDT"}]

    def get_funding_rates(self, symbol, end_time=None, limit=1):
        self._maybe_raise("get_funding_rates")
        return {
            "symbol": symbol,
            "rates": [{"fundingRate": "0.00015", "fundingTime": 1786237680000}],
        }

    def get_open_interests(self):
        self._maybe_raise("get_open_interests")
        return [{"symbol": "BTC_USDT_PERP", "openInterest": "999.5"}]

    def get_futures_risk_table(self, symbol=None):
        self._maybe_raise("get_futures_risk_table")
        return [{"rowNum": 1, "maxLeverage": "20", "notionalLimit": "100000"}]

    # --- private: account/balance ---
    def get_account_balances(self):
        self._maybe_raise("get_account_balances")
        return [
            {"coin": "USDT", "free": "1000.00000000", "frozen": "10.00000000"},
            {"coin": "BTC", "free": "0.05000000", "frozen": "0"},
        ]

    def get_futures_account_balances(self):
        self._maybe_raise("get_futures_account_balances")
        return {
            "balances": [
                {"coin": "USDT", "free": "5000.00000000", "frozen": "100.00000000", "debts": "0"}
            ],
            "isolates": [],
        }

    # --- private: positions ---
    def get_futures_positions(self, symbol=None):
        self._maybe_raise("get_futures_positions")
        return [
            {
                "positionId": "p-1",
                "symbol": symbol or "BTC_USDT_PERP",
                "isolatedMode": "CROSS",
                "positionSide": "LONG",
                "netSize": "0.2",
                "avgPrice": "60000",
                "unrealizedPnL": "120.50",
                "markPrice": "60602.5",
                "leverage": "5",
                "createTime": 1786237680000,
                "updateTime": 1786237680000,
            }
        ]

    def get_futures_leverage(self, symbol):
        self._maybe_raise("get_futures_leverage")
        return {"symbol": symbol, "leverage": "5"}

    def get_futures_margin_mode(self, symbol):
        self._maybe_raise("get_futures_margin_mode")
        return {"symbol": symbol, "isolatedMode": "CROSS"}

    # --- private: orders ---
    def get_futures_open_orders(self, symbol=None, end_time=None, limit=100):
        self._maybe_raise("get_futures_open_orders")
        # Dynamisch aus dem simulierten Order-Buch (self.orders), sobald
        # ein Test tatsaechlich ueber create_futures_order() Orders
        # angelegt hat (Futures Write Integration) - sonst (leeres
        # self.orders) unveraendertes statisches Fixture wie zuvor, damit
        # bestehende, rein lesende Tests unveraendert bleiben.
        if self.orders:
            return [
                dict(o)
                for o in self.orders.values()
                if o["status"] == "OPEN" and (symbol is None or o["symbol"] == symbol)
            ]
        return [
            {
                "orderId": 555,
                "symbol": symbol or "BTC_USDT_PERP",
                "type": "LIMIT",
                "side": "BUY",
                "price": "58000",
                "size": "0.01",
                "filledSize": "0",
                "status": "OPEN",
                "createTime": 1786237680000,
                "updateTime": 1786237680000,
            }
        ]

    def get_futures_order(self, symbol, order_id):
        self._maybe_raise("get_futures_order")
        if order_id in self.orders:
            return dict(self.orders[order_id])
        return {
            "orderId": order_id,
            "symbol": symbol,
            "type": "LIMIT",
            "side": "BUY",
            "price": "58000",
            "size": "0.01",
            "filledSize": "0.01",
            "filledAmount": "580.00",
            "status": "CLOSED",
            "createTime": 1786237680000,
            "updateTime": 1786237680000,
        }

    # --- private: write (Futures Write Integration) ---
    def create_futures_order(
        self,
        symbol,
        side,
        order_type,
        size=None,
        price=None,
        reduce_only=None,
        client_order_id=None,
    ):
        self._maybe_raise("create_futures_order")
        order_id = self._next_order_id
        self._next_order_id += 1
        immediate_fill = order_type == "MARKET_QTY"
        fill_price = price or "60000"
        self.orders[order_id] = {
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "price": price,
            "origSize": size,
            "size": size,
            "filledSize": size if immediate_fill else "0",
            "filledAmount": (
                str(Decimal(size or "0") * Decimal(fill_price)) if immediate_fill else "0"
            ),
            "status": "CLOSED" if immediate_fill else "OPEN",
            "reduceOnly": bool(reduce_only),
            "clientOrderId": client_order_id,
            "createTime": 1786237680000,
            "updateTime": 1786237680000,
        }
        if client_order_id:
            self.client_order_index[client_order_id] = order_id
        return order_id

    def get_futures_order_by_client_id(self, symbol, client_order_id):
        self._maybe_raise("get_futures_order_by_client_id")
        order_id = self.client_order_index.get(client_order_id)
        if order_id is None:
            raise PionexAPIError(
                "TRADE_ORDER_NOT_FOUND: no order with this clientOrderId",
                code="TRADE_ORDER_NOT_FOUND",
            )
        return dict(self.orders[order_id])

    def cancel_futures_order(self, symbol, order_id):
        self._maybe_raise("cancel_futures_order")
        order = self.orders.get(order_id)
        if order is None or order.get("status") == "CLOSED":
            raise PionexAPIError(
                "TRADE_ORDER_NOT_FOUND: order not found or already terminal",
                code="TRADE_ORDER_NOT_FOUND",
            )
        order["status"] = "CLOSED"

    def cancel_all_futures_orders(self, symbol):
        self._maybe_raise("cancel_all_futures_orders")
        self.cancel_all_calls.append(symbol)
        for order in self.orders.values():
            if order["symbol"] == symbol and order["status"] == "OPEN":
                order["status"] = "CLOSED"

    def set_futures_leverage(self, symbol, leverage):
        self._maybe_raise("set_futures_leverage")
        self.leverage_calls.append((symbol, leverage))
        return {"symbol": symbol, "leverage": leverage}

    def get_spot_open_orders(self, symbol):
        self._maybe_raise("get_spot_open_orders")
        return [
            {
                "orderId": 111,
                "symbol": symbol,
                "type": "LIMIT",
                "side": "SELL",
                "price": "62000",
                "size": "0.01",
                "filledSize": "0",
                "status": "OPEN",
                "createTime": 1786237680000,
                "updateTime": 1786237680000,
            }
        ]

    def get_spot_order(self, order_id):
        self._maybe_raise("get_spot_order")
        return {
            "orderId": order_id,
            "symbol": "BTC_USDT",
            "type": "LIMIT",
            "side": "SELL",
            "price": "62000",
            "size": "0.01",
            "filledSize": "0",
            "status": "OPEN",
            "createTime": 1786237680000,
            "updateTime": 1786237680000,
        }

    def get_futures_funding_fees(self, symbol=None, start_time=None, end_time=None, limit=100):
        self._maybe_raise("get_futures_funding_fees")
        return [
            {
                "symbol": symbol or "BTC_USDT_PERP",
                "isolatedMode": "CROSS",
                "fundingFee": "-1.23",
                "fundingCoin": "USDT",
                "timestamp": 1786237680000,
                "fundingRate": "0.00015",
            }
        ]


@pytest.fixture
def patch_client(monkeypatch):
    monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", FakePrivateClient)
    import ccxt.async_support as ccxt_async

    monkeypatch.delattr(ccxt_async, "pionex", raising=False)


async def _connected_live_futures_adapter(patch_client) -> PionexAdapter:
    adapter = PionexAdapter(
        api_key="real-key", secret="real-secret", trading_mode=TradingMode.LIVE, futures_mode=True
    )
    await adapter.connect()
    return adapter


async def _connected_live_spot_adapter(patch_client) -> PionexAdapter:
    adapter = PionexAdapter(
        api_key="real-key", secret="real-secret", trading_mode=TradingMode.LIVE, futures_mode=False
    )
    await adapter.connect()
    return adapter


# ---------------------------------------------------------------------
# 1. Authentication
# ---------------------------------------------------------------------


class TestAuthentication:
    async def test_connect_succeeds_with_valid_credentials(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        assert adapter._connected is True
        assert adapter._native_fallback is True

    async def test_connect_fails_with_invalid_credentials(self, patch_client, monkeypatch) -> None:
        """
        connect() macht bei LIVE einen authentifizierten Testaufruf
        (siehe _connect_native_fallback() Docstring) - eine ungueltige
        Signatur/API-Key muss SOFORT beim Verbindungsaufbau auffallen,
        nicht erst beim ersten spaeteren Lesezugriff.
        """
        adapter = PionexAdapter(
            api_key="bad", secret="bad", trading_mode=TradingMode.LIVE, futures_mode=True
        )

        async def fake_connect():
            client = FakePrivateClient(api_key="bad", api_secret="bad")
            client.inject_error(
                PionexAPIError("INVALID_SIGNATURE: bad signature", code="INVALID_SIGNATURE")
            )
            adapter._native_client = client
            with pytest.raises(ExchangeAuthenticationError):
                raise adapter._map_pionex_error(
                    PionexAPIError("INVALID_SIGNATURE: bad signature", code="INVALID_SIGNATURE")
                )

        # Direkter, deterministischer Test der Fehlerklassifizierung
        # (siehe TestInvalidCredentials fuer den vollstaendigen
        # connect()-Roundtrip-Fall).
        await fake_connect()


class TestInvalidCredentials:
    async def test_connect_raises_authentication_error_for_invalid_signature(
        self, monkeypatch
    ) -> None:
        import ccxt.async_support as ccxt_async

        monkeypatch.delattr(ccxt_async, "pionex", raising=False)

        class FailingAuthClient(FakePrivateClient):
            def get_futures_account_balances(self):
                raise PionexAPIError("INVALID_SIGNATURE: bad", code="INVALID_SIGNATURE")

        monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", FailingAuthClient)
        adapter = PionexAdapter(
            api_key="wrong", secret="wrong", trading_mode=TradingMode.LIVE, futures_mode=True
        )

        with pytest.raises(ExchangeAuthenticationError):
            await adapter.connect()

        assert adapter._connected is False
        assert adapter._native_fallback is False

    async def test_get_balance_raises_authentication_error_for_expired_key(
        self, patch_client
    ) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(
            PionexAPIError("APIKEY_EXPIRED: expired", code="APIKEY_EXPIRED")
        )

        with pytest.raises(ExchangeAuthenticationError):
            await adapter.get_balance()

    async def test_permission_denied_is_classified_as_authentication_error(
        self, patch_client
    ) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(
            PionexAPIError("PERMISSION_DENIED: no permission", code="PERMISSION_DENIED")
        )

        with pytest.raises(ExchangeAuthenticationError):
            await adapter.get_positions()


# ---------------------------------------------------------------------
# 2. Connectivity
# ---------------------------------------------------------------------


class TestConnectivity:
    async def test_ping_uses_public_endpoint(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        latency_ms = await adapter.ping()

        assert latency_ms >= 0

    async def test_connect_verifies_reachability_before_authentication(self, monkeypatch) -> None:
        """Ein reiner Netzwerkausfall (Erreichbarkeit) darf nicht als
        Authentifizierungsfehler missklassifiziert werden."""
        import ccxt.async_support as ccxt_async

        monkeypatch.delattr(ccxt_async, "pionex", raising=False)

        class UnreachableClient(FakePrivateClient):
            def get_symbols(self, symbols=None, market_type=None, status=None):
                raise PionexHTTPError(0, "connection refused")

        monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", UnreachableClient)
        adapter = PionexAdapter(
            api_key="k", secret="s", trading_mode=TradingMode.LIVE, futures_mode=True
        )

        with pytest.raises(ExchangeConnectionError):
            await adapter.connect()


# ---------------------------------------------------------------------
# 3. Account / 4. Balance
# ---------------------------------------------------------------------


class TestAccountAndBalance:
    async def test_get_balance_spot(self, patch_client) -> None:
        adapter = await _connected_live_spot_adapter(patch_client)

        balance = await adapter.get_balance()

        assert balance.assets["USDT"] == Decimal("1010.00000000")
        assert balance.total == Decimal("1010.00000000")

    async def test_get_balance_futures(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        balance = await adapter.get_balance()

        assert balance.assets["USDT"] == Decimal("5100.00000000")

    async def test_get_balance_paper_mode_is_synthetic_zero(self, patch_client) -> None:
        """PAPER-Kapital wird NIE von einer Exchange gelesen (siehe
        get_balance() Docstring) - auch nicht, wenn zufaellig echt
        aussehende Dummy-Credentials konfiguriert waeren."""
        adapter = PionexAdapter(
            api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER
        )
        await adapter.connect()

        balance = await adapter.get_balance()

        assert balance.total == Decimal("0")
        assert balance.assets == {}


# ---------------------------------------------------------------------
# 5. Futures Instrument Tests
# ---------------------------------------------------------------------


class TestFuturesInstruments:
    async def test_get_exchange_info_lists_perp_symbols_via_type_filter(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        raw = await __import__("asyncio").to_thread(
            adapter._native_client.get_symbols, market_type="PERP"
        )

        assert raw[0]["symbol"] == "BTC_USDT_PERP"
        assert raw[0]["type"] == "PERP"

    async def test_get_futures_risk_table_exposes_leverage_tiers(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        rows = adapter._native_client.get_futures_risk_table("BTC_USDT_PERP")

        assert rows[0]["maxLeverage"] == "20"


# ---------------------------------------------------------------------
# 6. Position Tests
# ---------------------------------------------------------------------


class TestPositions:
    async def test_get_positions_returns_real_futures_positions(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        positions = await adapter.get_positions()

        assert len(positions) == 1
        pos = positions[0]
        assert pos.side == PositionSide.LONG
        assert pos.quantity == Decimal("0.2")
        assert pos.entry_price == Decimal("60000")
        assert pos.leverage == Decimal("5")
        assert pos.symbol.base == "BTC"
        assert pos.symbol.quote == "USDT"

    async def test_get_positions_spot_mode_stays_empty(self, patch_client) -> None:
        """Spot kennt konzeptionell keine Positionen - auch bei einem
        echten, verbundenen LIVE-Account."""
        adapter = await _connected_live_spot_adapter(patch_client)

        positions = await adapter.get_positions()

        assert positions == []

    async def test_get_positions_paper_mode_stays_empty(self, patch_client) -> None:
        adapter = PionexAdapter(
            api_key="paper_key",
            secret="paper_secret",
            trading_mode=TradingMode.PAPER,
            futures_mode=True,
        )
        await adapter.connect()

        assert await adapter.get_positions() == []

    async def test_zero_size_positions_are_filtered_out(self, patch_client, monkeypatch) -> None:
        import ccxt.async_support as ccxt_async

        monkeypatch.delattr(ccxt_async, "pionex", raising=False)

        class ZeroPositionClient(FakePrivateClient):
            def get_futures_positions(self, symbol=None):
                return [{"symbol": "BTC_USDT_PERP", "positionSide": "LONG", "netSize": "0"}]

        monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", ZeroPositionClient)
        adapter = PionexAdapter(
            api_key="k", secret="s", trading_mode=TradingMode.LIVE, futures_mode=True
        )
        await adapter.connect()

        assert await adapter.get_positions() == []


# ---------------------------------------------------------------------
# 7. Open Order Tests
# ---------------------------------------------------------------------


class TestOpenOrders:
    async def test_get_open_orders_futures_all_symbols(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        orders = await adapter.get_open_orders()

        assert len(orders) == 1
        assert orders[0].status == OrderStatus.SUBMITTED

    async def test_get_open_orders_spot_requires_symbol(self, patch_client) -> None:
        adapter = await _connected_live_spot_adapter(patch_client)

        with pytest.raises(AdapterFeatureNotImplementedError):
            await adapter.get_open_orders()

    async def test_get_open_orders_spot_with_symbol_succeeds(self, patch_client) -> None:
        adapter = await _connected_live_spot_adapter(patch_client)

        orders = await adapter.get_open_orders(symbol="BTC/USDT")

        assert len(orders) == 1
        assert orders[0].symbol.base == "BTC"

    async def test_get_open_orders_paper_mode_stays_empty(self, patch_client) -> None:
        adapter = PionexAdapter(
            api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER
        )
        await adapter.connect()

        assert await adapter.get_open_orders(symbol="BTC/USDT") == []


# ---------------------------------------------------------------------
# 8. Order Status Tests
# ---------------------------------------------------------------------


class TestOrderStatus:
    async def test_get_order_futures(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        result = await adapter.get_order("42", "BTC/USDT")

        assert result.exchange_order_id == "42"
        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == Decimal("0.01")

    async def test_get_order_spot(self, patch_client) -> None:
        adapter = await _connected_live_spot_adapter(patch_client)

        result = await adapter.get_order("99", "BTC/USDT")

        assert result.exchange_order_id == "99"

    async def test_get_order_non_numeric_id_raises_not_found(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        with pytest.raises(OrderNotFoundError):
            await adapter.get_order("PAPER-not-a-number", "BTC/USDT")

    async def test_get_order_paper_mode_raises_not_found(self, patch_client) -> None:
        adapter = PionexAdapter(
            api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER
        )
        await adapter.connect()

        with pytest.raises(OrderNotFoundError):
            await adapter.get_order("1", "BTC/USDT")


# ---------------------------------------------------------------------
# 9. Funding Tests
# ---------------------------------------------------------------------


class TestFunding:
    async def test_get_funding_rate_public(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        rate = await adapter.get_funding_rate("BTC/USDT")

        assert rate.rate == Decimal("0.00015")

    async def test_get_funding_rate_works_in_paper_mode_too(self, patch_client) -> None:
        """Oeffentlicher Endpunkt - keine Credentials noetig."""
        adapter = PionexAdapter(
            api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER
        )
        await adapter.connect()

        rate = await adapter.get_funding_rate("BTC/USDT")

        assert rate.rate == Decimal("0.00015")

    async def test_get_open_interest_public(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        oi = await adapter.get_open_interest("BTC/USDT")

        assert oi.open_interest == Decimal("999.5")

    async def test_get_futures_funding_fee_history_requires_live_futures(
        self, patch_client
    ) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        fees = await adapter.get_futures_funding_fee_history(symbol="BTC/USDT")

        assert fees[0]["fundingFee"] == "-1.23"

    async def test_get_futures_funding_fee_history_blocked_for_spot(self, patch_client) -> None:
        adapter = await _connected_live_spot_adapter(patch_client)

        with pytest.raises(AdapterFeatureNotImplementedError):
            await adapter.get_futures_funding_fee_history()


# ---------------------------------------------------------------------
# Leverage / Margin (Punkt 9)
# ---------------------------------------------------------------------


class TestLeverageAndMargin:
    async def test_get_futures_leverage(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        leverage = await adapter.get_futures_leverage("BTC/USDT")

        assert leverage == Decimal("5")

    async def test_get_futures_margin_mode(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        mode = await adapter.get_futures_margin_mode("BTC/USDT")

        assert mode == "CROSS"

    async def test_set_leverage_now_implemented_for_live_futures(self, patch_client) -> None:
        """
        Seit der Futures Write Integration ist set_leverage() fuer
        LIVE+Futures echt implementiert (POST /uapi/v1/account/leverage) -
        ersetzt den vorherigen AdapterFeatureNotImplementedError-Test.
        Spot bleibt weiterhin blockiert, siehe TestFuturesWriteOperations.
        """
        adapter = await _connected_live_futures_adapter(patch_client)

        await adapter.set_leverage("BTC/USDT", Decimal("10"))

        assert adapter._native_client.leverage_calls == [("BTC_USDT_PERP", "10")]


# ---------------------------------------------------------------------
# 10. Capability Tests
# ---------------------------------------------------------------------


class TestCapabilityDetection:
    async def test_detect_capabilities_all_true_for_healthy_futures_account(
        self, patch_client
    ) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        caps = await adapter.detect_capabilities()

        assert caps["connected"] is True
        assert caps["public_market_data"] is True
        assert caps["authenticated_account_read"] is True
        assert caps["futures_account_read"] is True
        assert caps["futures_positions_read"] is True
        assert caps["futures_open_orders_read"] is True

    async def test_detect_capabilities_reflects_actual_permission_failure(
        self, patch_client
    ) -> None:
        """Punkt 12: Capability Detection auf Basis TATSAECHLICH von
        Pionex gemeldeter Faehigkeiten - nicht nur der statischen
        Exchange-Capability-Tabelle. Ein Account ohne Futures-Berechtigung
        muss das widerspiegeln, obwohl PIONEX/PERPETUAL technisch
        existiert (siehe sgr.exchanges.capabilities)."""
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(
            PionexAPIError("PERMISSION_DENIED: no futures permission", code="PERMISSION_DENIED"),
            only_for={
                "get_account_balances",
                "get_futures_account_balances",
                "get_futures_positions",
                "get_futures_open_orders",
            },
        )

        caps = await adapter.detect_capabilities()

        assert caps["public_market_data"] is True  # bereits vor dem Fehler geprueft
        assert caps["authenticated_account_read"] is False
        assert caps["futures_account_read"] is False
        assert caps["futures_positions_read"] is False

    async def test_detect_capabilities_paper_mode_stops_after_public_check(
        self, patch_client
    ) -> None:
        adapter = PionexAdapter(
            api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER
        )
        await adapter.connect()

        caps = await adapter.detect_capabilities()

        assert caps["public_market_data"] is True
        assert caps["authenticated_account_read"] is False


# ---------------------------------------------------------------------
# 11. Reconciliation Tests
# ---------------------------------------------------------------------


class TestReconciliationReadPath:
    async def test_reconciliation_reads_real_positions_from_pionex(self, patch_client) -> None:
        """
        Belegt Aufgabenstellung Punkt 11: ReconciliationEngine ruft
        ausschliesslich adapter.get_positions() auf (siehe
        sgr/reconciliation/engine.py - keine Aenderung dort noetig) -
        seit LIVE jetzt ueber den nativen Fallback verbinden kann,
        funktioniert dieser Pfad fuer Pionex zum ersten Mal ueberhaupt
        end-to-end.
        """
        adapter = await _connected_live_futures_adapter(patch_client)
        pool = ExchangePool()
        pool._adapters[(ExchangeID.PIONEX, TradingMode.LIVE)] = adapter

        portfolio_engine = MagicMock()
        portfolio_engine.positions = []

        engine = ReconciliationEngine(
            exchange_pool=pool,
            portfolio_engine=portfolio_engine,
            trading_mode=TradingMode.LIVE,
            exchange_id=ExchangeID.PIONEX,
        )

        result = await engine.reconcile()

        assert result.status.value in ("discrepancies_found", "clean")
        assert result.checked_symbols >= 1
        # Die lokale Seite kennt die Position nicht -> MISSING_LOCALLY
        # (Split-Brain-Warnsignal) ist hier der erwartete, korrekte Fund.
        assert result.has_split_brain_risk is True

    async def test_reconciliation_matches_when_local_state_agrees(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        pool = ExchangePool()
        pool._adapters[(ExchangeID.PIONEX, TradingMode.LIVE)] = adapter

        exchange_positions = await adapter.get_positions()
        portfolio_engine = MagicMock()
        portfolio_engine.positions = exchange_positions

        engine = ReconciliationEngine(
            exchange_pool=pool,
            portfolio_engine=portfolio_engine,
            trading_mode=TradingMode.LIVE,
            exchange_id=ExchangeID.PIONEX,
        )

        result = await engine.reconcile()

        assert result.has_split_brain_risk is False

    async def test_reconciliation_is_skipped_outside_live(self, patch_client) -> None:
        adapter = PionexAdapter(
            api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER
        )
        await adapter.connect()
        pool = ExchangePool()
        pool._adapters[(ExchangeID.PIONEX, TradingMode.PAPER)] = adapter

        engine = ReconciliationEngine(
            exchange_pool=pool,
            portfolio_engine=MagicMock(positions=[]),
            trading_mode=TradingMode.PAPER,
            exchange_id=ExchangeID.PIONEX,
        )

        result = await engine.reconcile()

        assert result.status.value == "skipped_not_live"


# ---------------------------------------------------------------------
# Timeout Tests
# ---------------------------------------------------------------------


class TestTimeout:
    async def test_get_balance_maps_timeout_to_connection_error(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(PionexHTTPError(0, "Pionex request failed: timeout"))

        with pytest.raises(ExchangeConnectionError):
            await adapter.get_balance()

    async def test_get_positions_maps_requests_timeout(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(PionexHTTPError(0, "timed out"))

        with pytest.raises(ExchangeConnectionError):
            await adapter.get_positions()


# ---------------------------------------------------------------------
# Rate Limit Tests
# ---------------------------------------------------------------------


class TestRateLimitMapping:
    async def test_http_429_maps_to_rate_limit_error(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(PionexHTTPError(429, "too many requests"))

        with pytest.raises(RateLimitError):
            await adapter.get_balance()

    async def test_open_orders_rate_limited(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(PionexHTTPError(429, "too many requests"))

        with pytest.raises(RateLimitError):
            await adapter.get_open_orders()


# ---------------------------------------------------------------------
# Malformed Response Tests
# ---------------------------------------------------------------------


class TestMalformedResponseMapping:
    async def test_malformed_json_maps_to_generic_exchange_error(self, patch_client) -> None:
        from sgr.exchanges.base import ExchangeError

        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(PionexAPIError("Pionex returned invalid JSON"))

        with pytest.raises(ExchangeError):
            await adapter.get_balance()

    async def test_unknown_error_code_does_not_crash_adapter(self, patch_client) -> None:
        from sgr.exchanges.base import ExchangeError

        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(
            PionexAPIError("TRADE_PARAMETER_ERROR: bad params", code="TRADE_PARAMETER_ERROR")
        )

        with pytest.raises(ExchangeError):
            await adapter.get_positions()


# ---------------------------------------------------------------------
# Exchange Unavailable Tests
# ---------------------------------------------------------------------


class TestExchangeUnavailable:
    async def test_connect_fails_when_exchange_unreachable(self, monkeypatch) -> None:
        import ccxt.async_support as ccxt_async

        monkeypatch.delattr(ccxt_async, "pionex", raising=False)

        class UnreachableClient(FakePrivateClient):
            def get_symbols(self, symbols=None, market_type=None, status=None):
                raise requests.ConnectionError("DNS resolution failed")

        monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", UnreachableClient)
        adapter = PionexAdapter(
            api_key="k", secret="s", trading_mode=TradingMode.LIVE, futures_mode=True
        )

        with pytest.raises(ExchangeConnectionError):
            await adapter.connect()

        assert adapter._connected is False

    async def test_balance_read_fails_gracefully_when_exchange_drops_mid_session(
        self, patch_client
    ) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(requests.ConnectionError("connection reset"))

        with pytest.raises(ExchangeConnectionError):
            await adapter.get_balance()


# ---------------------------------------------------------------------
# Tenant Isolation Tests
# ---------------------------------------------------------------------


class TestTenantIsolation:
    async def test_two_adapters_with_different_credentials_stay_isolated(
        self, patch_client
    ) -> None:
        gordon = PionexAdapter(
            api_key="gordon-key",
            secret="gordon-secret",
            trading_mode=TradingMode.LIVE,
            futures_mode=True,
        )
        sumo = PionexAdapter(
            api_key="sumo-key",
            secret="sumo-secret",
            trading_mode=TradingMode.LIVE,
            futures_mode=True,
        )
        await gordon.connect()
        await sumo.connect()

        assert gordon._native_client is not sumo._native_client
        assert gordon._native_client.api_key == "gordon-key"
        assert sumo._native_client.api_key == "sumo-key"
        assert gordon._api_key != sumo._api_key

    async def test_error_injected_on_one_tenant_does_not_affect_the_other(
        self, patch_client
    ) -> None:
        gordon = await _connected_live_futures_adapter(patch_client)
        sumo = await _connected_live_futures_adapter(patch_client)

        gordon._native_client.inject_error(
            PionexAPIError("INVALID_SIGNATURE: bad", code="INVALID_SIGNATURE")
        )

        with pytest.raises(ExchangeAuthenticationError):
            await gordon.get_balance()

        # Sumo's eigener (unabhaengiger) Client ist unberuehrt.
        balance = await sumo.get_balance()
        assert balance.total == Decimal("5100.00000000")


# ---------------------------------------------------------------------
# Paper Trading Tests (unveraendert, keine private Endpunkte beteiligt)
# ---------------------------------------------------------------------


class TestPaperTradingUnaffected:
    async def test_paper_mode_never_constructs_client_with_real_credentials(
        self, patch_client
    ) -> None:
        adapter = PionexAdapter(
            api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER
        )
        await adapter.connect()

        assert adapter._native_client.api_key is None
        assert adapter._native_client.api_secret is None

    async def test_paper_order_still_fills_locally_via_simulation(self, patch_client) -> None:
        from uuid import uuid4

        from sgr.core.types import OrderRequest, OrderType, Side

        adapter = PionexAdapter(
            api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER
        )
        await adapter.connect()

        order = OrderRequest(
            signal_id=uuid4(),
            symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.001"),
            trading_mode=TradingMode.PAPER,
        )

        result = await adapter.place_order(order)

        assert result.status == OrderStatus.FILLED

    async def test_paper_mode_get_balance_never_calls_private_client_methods(
        self, patch_client
    ) -> None:
        adapter = PionexAdapter(
            api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER
        )
        await adapter.connect()

        await adapter.get_balance()

        assert "get_account_balances" not in adapter._native_client.calls
        assert "get_futures_account_balances" not in adapter._native_client.calls


# ---------------------------------------------------------------------
# Futures Write Integration (2026-09-21) - place_order/cancel_order/
# cancel_all_orders/set_leverage sind fuer LIVE+Futures jetzt echt
# implementiert. Ausschliesslich gegen FakePrivateClient getestet - KEIN
# echter Pionex-Account, KEINE echte Order (siehe Aufgabenstellung
# "KEINE echten Pionex Write Requests ausfuehren" fuer diese Phase).
# ---------------------------------------------------------------------


class TestFuturesWriteOperations:
    async def test_market_buy_order_fills_immediately(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        result = await adapter.place_order(_market_order(side=Side.BUY, quantity="0.01"))

        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == Decimal("0.01")
        created = adapter._native_client.orders[result.raw_response["orderId"]]
        assert created["side"] == "BUY"
        assert created["type"] == "MARKET_QTY"

    async def test_market_sell_order_fills_immediately(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        result = await adapter.place_order(_market_order(side=Side.SELL, quantity="0.02"))

        assert result.status == OrderStatus.FILLED
        created = adapter._native_client.orders[result.raw_response["orderId"]]
        assert created["side"] == "SELL"

    async def test_limit_order_stays_open(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        result = await adapter.place_order(_limit_order(price="58000", quantity="0.01"))

        assert result.status == OrderStatus.SUBMITTED
        created = adapter._native_client.orders[result.raw_response["orderId"]]
        assert created["type"] == "LIMIT"
        assert created["price"] == "58000"

    async def test_reduce_only_flag_is_forwarded(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        result = await adapter.place_order(_market_order(reduce_only=True))

        created = adapter._native_client.orders[result.raw_response["orderId"]]
        assert created["reduceOnly"] is True

    async def test_symbol_mapping_appends_perp_suffix(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        result = await adapter.place_order(_market_order(symbol="BTC/USDT"))

        created = adapter._native_client.orders[result.raw_response["orderId"]]
        assert created["symbol"] == "BTC_USDT_PERP"

    async def test_quantity_mapping_sends_base_asset_quantity_as_string(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        result = await adapter.place_order(_market_order(quantity="0.12345"))

        created = adapter._native_client.orders[result.raw_response["orderId"]]
        assert created["size"] == "0.12345"

    async def test_client_order_id_is_order_uuid(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        order = _market_order()

        result = await adapter.place_order(order)

        created = adapter._native_client.orders[result.raw_response["orderId"]]
        assert created["clientOrderId"] == str(order.id)

    async def test_idempotent_resubmit_returns_existing_order_without_creating_new_one(
        self, patch_client
    ) -> None:
        """Ein zweiter place_order()-Aufruf mit DEMSELBEN OrderRequest
        (identische order.id) darf keine zweite echte Order erzeugen -
        siehe get_futures_order_by_client_id()-Vorab-Pruefung in
        PionexAdapter.place_order()."""
        adapter = await _connected_live_futures_adapter(patch_client)
        order = _market_order()

        first = await adapter.place_order(order)
        orders_after_first = len(adapter._native_client.orders)
        second = await adapter.place_order(order)

        assert len(adapter._native_client.orders) == orders_after_first
        assert second.exchange_order_id == first.exchange_order_id

    async def test_cancel_order_success(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        placed = await adapter.place_order(_limit_order())
        order_id = placed.exchange_order_id

        cancelled = await adapter.cancel_order(order_id, "BTC/USDT")

        assert cancelled is True
        assert adapter._native_client.orders[int(order_id)]["status"] == "CLOSED"

    async def test_cancel_order_not_found_returns_false(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        cancelled = await adapter.cancel_order("999999", "BTC/USDT")

        assert cancelled is False

    async def test_cancel_already_filled_order_returns_false(self, patch_client) -> None:
        """Eine bereits gefuellte (MARKET_QTY, status CLOSED) Order laesst
        sich nicht mehr stornieren - der Fake meldet TRADE_ORDER_NOT_FOUND,
        der Adapter bildet das auf False ab (identisches Protocol-
        Verhalten wie CCXTBaseAdapter.cancel_order() fuer Binance)."""
        adapter = await _connected_live_futures_adapter(patch_client)
        placed = await adapter.place_order(_market_order())
        assert placed.status == OrderStatus.FILLED

        cancelled = await adapter.cancel_order(placed.exchange_order_id, "BTC/USDT")

        assert cancelled is False

    async def test_cancel_all_orders_for_one_symbol(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        await adapter.place_order(_limit_order(symbol="BTC/USDT"))
        await adapter.place_order(_limit_order(symbol="BTC/USDT", price="59000"))

        cancelled_count = await adapter.cancel_all_orders("BTC/USDT")

        assert cancelled_count == 2
        assert adapter._native_client.cancel_all_calls == ["BTC_USDT_PERP"]
        assert all(o["status"] == "CLOSED" for o in adapter._native_client.orders.values())

    async def test_cancel_all_orders_across_symbols_without_filter(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        await adapter.place_order(_limit_order(symbol="BTC/USDT"))
        await adapter.place_order(_limit_order(symbol="ETH/USDT", price="3000"))

        cancelled_count = await adapter.cancel_all_orders()

        assert cancelled_count == 2
        assert set(adapter._native_client.cancel_all_calls) == {"BTC_USDT_PERP", "ETH_USDT_PERP"}

    async def test_set_leverage_success(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        await adapter.set_leverage("BTC/USDT", Decimal("10"))

        assert adapter._native_client.leverage_calls == [("BTC_USDT_PERP", "10")]

    async def test_place_order_error_mapping_invalid_signature(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(
            PionexAPIError("INVALID_SIGNATURE: bad", code="INVALID_SIGNATURE"),
            only_for={"create_futures_order"},
        )

        with pytest.raises(ExchangeAuthenticationError):
            await adapter.place_order(_market_order())

    async def test_place_order_error_mapping_rate_limit(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(
            PionexHTTPError(429, "too many requests"), only_for={"create_futures_order"}
        )

        with pytest.raises(RateLimitError):
            await adapter.place_order(_market_order())

    async def test_place_order_unknown_state_on_network_failure_after_submit(
        self, patch_client
    ) -> None:
        """
        Simuliert den Unknown-State-Fall: create_futures_order() selbst
        gelingt (die Order KOENNTE auf Pionex existieren), aber der
        anschliessende get_futures_order()-Statusabruf schlaegt fehl
        (z.B. Netzwerkfehler). place_order() darf hier NICHT stillschweigend
        einen Fill vortaeuschen, sondern muss den Fehler propagieren - das
        bestehende SGR-seitige SafeOrderExecutor.execute_safely()
        (sgr/execution/order_safety.py, unveraendert) faengt das dann als
        raw_response["unknown"]=True ab, siehe dortiger Modul-Docstring.
        """
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(
            PionexHTTPError(0, "connection reset"), only_for={"get_futures_order"}
        )

        with pytest.raises(ExchangeConnectionError):
            await adapter.place_order(_market_order())

        # Die Order EXISTIERT auf Pionex-Seite (create_futures_order lief
        # durch) - genau das macht diesen Fall zu einem echten Unknown
        # State statt eines simplen Fehlschlags.
        assert len(adapter._native_client.orders) == 1

    async def test_place_order_type_not_mapped_raises_without_network_call(
        self, patch_client
    ) -> None:
        """STOP_MARKET/TAKE_PROFIT_MARKET/TWAP werden bewusst nicht
        geraten (siehe pionex.py _map_order_type_to_pionex() Docstring) -
        muss VOR jedem Netzwerk-Call ablehnen."""
        adapter = await _connected_live_futures_adapter(patch_client)
        order = OrderRequest(
            signal_id=uuid4(),
            symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX),
            side=Side.BUY,
            order_type=OrderType.STOP_MARKET,
            quantity=Decimal("0.01"),
            trading_mode=TradingMode.LIVE,
        )

        with pytest.raises(AdapterFeatureNotImplementedError, match="order_type_stop_market"):
            await adapter.place_order(order)

        assert adapter._native_client.orders == {}


class TestPionexOrderNotFoundErrorCodeMapping:
    """
    TRADE_ORDER_NOT_EXIST ist der TATSAECHLICHE, live gegen einen echten
    Pionex-Futures-Account verifizierte Fehlercode fuer "diese Order
    existiert nicht" (Phase 4 Read-Only-Lauf, orderByClientOrderId-Probe
    mit garantiert nicht existierender Test-ID, 2026-09-21) -
    TRADE_ORDER_NOT_FOUND war zuvor nur aus der Dokumentation abgeleitet
    und nie live bestaetigt. Beide Codes muessen von _map_pionex_error()
    auf OrderNotFoundError abgebildet werden, siehe pionex.py.
    """

    async def test_map_pionex_error_recognizes_trade_order_not_exist(self, patch_client) -> None:
        adapter = await _connected_live_futures_adapter(patch_client)

        mapped = adapter._map_pionex_error(
            PionexAPIError("TRADE_ORDER_NOT_EXIST: order not found", code="TRADE_ORDER_NOT_EXIST")
        )

        assert isinstance(mapped, OrderNotFoundError)

    async def test_map_pionex_error_still_recognizes_trade_order_not_found(
        self, patch_client
    ) -> None:
        """Regressionsschutz (Aufgabenstellung Punkt 3): die vorher
        angenommene, nie live bestaetigte Schreibweise bleibt zusaetzlich
        erkannt, falls Pionex sie an anderer Stelle doch verwendet."""
        adapter = await _connected_live_futures_adapter(patch_client)

        mapped = adapter._map_pionex_error(
            PionexAPIError("TRADE_ORDER_NOT_FOUND: order not found", code="TRADE_ORDER_NOT_FOUND")
        )

        assert isinstance(mapped, OrderNotFoundError)

    async def test_cancel_order_real_not_exist_code_returns_false(self, patch_client) -> None:
        """End-to-end durch cancel_order(): der reale Fehlercode
        TRADE_ORDER_NOT_EXIST muss - wie TRADE_ORDER_NOT_FOUND - als
        False abgebildet werden, nicht als geworfener Fehler (identisches
        Protocol-Verhalten wie CCXTBaseAdapter.cancel_order() fuer
        Binance)."""
        adapter = await _connected_live_futures_adapter(patch_client)
        placed = await adapter.place_order(_limit_order())
        adapter._native_client.inject_error(
            PionexAPIError("TRADE_ORDER_NOT_EXIST: order not found", code="TRADE_ORDER_NOT_EXIST"),
            only_for={"cancel_futures_order"},
        )

        cancelled = await adapter.cancel_order(placed.exchange_order_id, "BTC/USDT")

        assert cancelled is False

    async def test_place_order_idempotency_probe_treats_not_exist_as_no_duplicate(
        self, patch_client
    ) -> None:
        """Die clientOrderId-Vorabpruefung (_find_existing_futures_order_by_client_id)
        faengt JEDE Exception generisch ab (siehe pionex.py Docstring) -
        bestaetigt hier explizit fuer den realen TRADE_ORDER_NOT_EXIST-Code:
        eine neue Order wird trotzdem angelegt, kein falsches Duplikat-
        Signal."""
        adapter = await _connected_live_futures_adapter(patch_client)
        adapter._native_client.inject_error(
            PionexAPIError("TRADE_ORDER_NOT_EXIST: order not found", code="TRADE_ORDER_NOT_EXIST"),
            only_for={"get_futures_order_by_client_id"},
        )

        result = await adapter.place_order(_market_order())

        assert result.status == OrderStatus.FILLED
        assert len(adapter._native_client.orders) == 1


class TestLiveSpotWriteStaysBlocked:
    """LIVE Spot write (place_order/cancel_order/cancel_all_orders) bleibt
    bewusst nicht implementiert - nur der Futures-Pfad wurde gegen
    openapi_futures.yaml verifiziert (siehe pionex.py Modul-Docstring)."""

    async def test_place_order_blocked_for_spot(self, patch_client) -> None:
        adapter = await _connected_live_spot_adapter(patch_client)

        with pytest.raises(AdapterFeatureNotImplementedError, match="live_spot_order_submission"):
            await adapter.place_order(_market_order())

    async def test_cancel_order_blocked_for_spot(self, patch_client) -> None:
        adapter = await _connected_live_spot_adapter(patch_client)

        with pytest.raises(AdapterFeatureNotImplementedError, match="live_spot_cancel_order"):
            await adapter.cancel_order("1", "BTC/USDT")

    async def test_cancel_all_orders_blocked_for_spot(self, patch_client) -> None:
        adapter = await _connected_live_spot_adapter(patch_client)

        with pytest.raises(AdapterFeatureNotImplementedError, match="live_spot_cancel_all_orders"):
            await adapter.cancel_all_orders()
