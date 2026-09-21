"""
Integrationstests: Futures Write Integration (2026-09-21) gegen die
BESTEHENDEN, UNVERAENDERTEN SGR-Kernkomponenten - PreflightValidator,
SafeOrderExecutor, KillSwitch. Beweist, dass die neue Pionex-Write-
Implementierung (sgr/exchanges/pionex.py) sich in die vorhandene
Architektur einfuegt, OHNE dass eine dieser Komponenten angefasst werden
musste (siehe Aufgabenstellung Punkt 3: "ExecutionEngine, PreflightValidator,
SafeOrderExecutor, KillSwitch, ... NICHT verändern").

Ausschliesslich gegen einen Fake-PionexClient getestet (kein echtes
Netzwerk, keine echten Credentials, keine echte Order - siehe
Aufgabenstellung "KEINE echten Pionex Write Requests ausfuehren").
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from sgr.core.types import (
    ExchangeID,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
    TradingMode,
)
from sgr.exchanges.factory import ExchangePool
from sgr.exchanges.pionex import PionexAdapter
from sgr.execution.order_safety import SafeOrderExecutor
from sgr.execution.preflight import PreflightValidator
from sgr.risk.kill_switch import KillSwitch


class FakeWriteClient:
    """Minimaler, aber preflight-vollstaendiger Fake (im Unterschied zu
    tests/exchanges/test_pionex_private_api.py::FakePrivateClient liefert
    get_symbols() hier auch Precision/Limits-Felder, damit
    PreflightValidator._check_symbol_precision_and_limits() nicht nur
    "not supported", sondern tatsaechlich GRUEN durchlaeuft)."""

    KLINE_INTERVALS = {"1h": "60M"}

    def __init__(self, api_key=None, api_secret=None) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.orders: dict[int, dict] = {}
        self.client_order_index: dict[str, int] = {}
        self._next_order_id = 5000
        self.cancel_all_calls: list[str] = []

    def close(self):
        pass

    def get_symbols(self, symbols=None, market_type=None, status=None):
        return [
            {
                "symbol": "BTC_USDT",
                "baseCurrency": "BTC",
                "quoteCurrency": "USDT",
                "basePrecision": 6,
                "quotePrecision": 2,
                "minAmount": "0.0001",
            }
        ]

    def get_futures_account_balances(self):
        return {"balances": [{"coin": "USDT", "free": "5000", "frozen": "0"}], "isolates": []}

    def get_futures_positions(self, symbol=None):
        return []

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
        order_id = self._next_order_id
        self._next_order_id += 1
        immediate_fill = order_type == "MARKET_QTY"
        self.orders[order_id] = {
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "price": price,
            "size": size,
            "filledSize": size if immediate_fill else "0",
            "filledAmount": str(Decimal(size or "0") * Decimal(price or "60000"))
            if immediate_fill
            else "0",
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
        from sgr.exchanges.pionex_client import PionexAPIError

        order_id = self.client_order_index.get(client_order_id)
        if order_id is None:
            raise PionexAPIError("TRADE_ORDER_NOT_FOUND", code="TRADE_ORDER_NOT_FOUND")
        return dict(self.orders[order_id])

    def get_futures_order(self, symbol, order_id):
        return dict(self.orders[order_id])

    def cancel_futures_order(self, symbol, order_id):
        order = self.orders.get(order_id)
        if order is None or order["status"] == "CLOSED":
            from sgr.exchanges.pionex_client import PionexAPIError

            raise PionexAPIError("TRADE_ORDER_NOT_FOUND", code="TRADE_ORDER_NOT_FOUND")
        order["status"] = "CLOSED"

    def cancel_all_futures_orders(self, symbol):
        self.cancel_all_calls.append(symbol)
        for order in self.orders.values():
            if order["symbol"] == symbol and order["status"] == "OPEN":
                order["status"] = "CLOSED"

    def get_futures_open_orders(self, symbol=None, end_time=None, limit=100):
        return [
            dict(o)
            for o in self.orders.values()
            if o["status"] == "OPEN" and (symbol is None or o["symbol"] == symbol)
        ]

    def set_futures_leverage(self, symbol, leverage):
        return {"symbol": symbol, "leverage": leverage}


@pytest.fixture
def patch_client(monkeypatch):
    monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", FakeWriteClient)
    import ccxt.async_support as ccxt_async

    monkeypatch.delattr(ccxt_async, "pionex", raising=False)


def _market_order() -> OrderRequest:
    return OrderRequest(
        signal_id=uuid4(),
        symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX),
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        trading_mode=TradingMode.LIVE,
    )


async def _connected_pool_and_adapter(patch_client) -> tuple[ExchangePool, PionexAdapter]:
    adapter = PionexAdapter(
        api_key="k", secret="s", trading_mode=TradingMode.LIVE, futures_mode=True
    )
    await adapter.connect()
    pool = ExchangePool()
    pool._adapters[(ExchangeID.PIONEX, TradingMode.LIVE)] = adapter
    return pool, adapter


class TestPreflightValidatorAgainstPionexWritePath:
    """Beweist: PreflightValidator (unveraendert) laesst eine gueltige
    Pionex-Futures-MARKET-Order durch, OHNE jede Pionex-Sonderlogik -
    dieselben generischen Checks wie fuer Binance."""

    async def test_valid_market_order_is_eligible(self, patch_client) -> None:
        pool, _ = await _connected_pool_and_adapter(patch_client)
        validator = PreflightValidator(pool, TradingMode.LIVE)

        result = await validator.validate(_market_order())

        assert result.eligible is True, result.rejection_summary

    async def test_insufficient_balance_blocks_order(self, patch_client, monkeypatch) -> None:
        pool, adapter = await _connected_pool_and_adapter(patch_client)
        adapter._native_client.get_futures_account_balances = lambda: {
            "balances": [{"coin": "USDT", "free": "0", "frozen": "0"}],
            "isolates": [],
        }
        validator = PreflightValidator(pool, TradingMode.LIVE)

        result = await validator.validate(_market_order())

        assert result.eligible is False
        assert any(f.name == "balance_and_available_capital" for f in result.failures)


class TestSafeOrderExecutorAgainstPionexWritePath:
    """Beweist: SafeOrderExecutor (unveraendert) idempotency/duplicate-
    Schutz funktioniert identisch gegen den echten Pionex-Adapter-Call."""

    async def test_order_fills_through_safe_executor(self, patch_client) -> None:
        _, adapter = await _connected_pool_and_adapter(patch_client)
        safety = SafeOrderExecutor()
        order = _market_order()

        result = await safety.execute_safely(order, adapter.place_order)

        assert result.status == OrderStatus.FILLED
        assert result.raw_response.get("duplicate") is not True
        assert result.raw_response.get("unknown") is not True

    async def test_in_process_duplicate_blocked_before_second_adapter_call(
        self, patch_client
    ) -> None:
        _, adapter = await _connected_pool_and_adapter(patch_client)
        safety = SafeOrderExecutor()
        order = _market_order()

        first = await safety.execute_safely(order, adapter.place_order)
        # Placeholder wird erst durch release() freigegeben (siehe
        # ExecutionEngine._execute_internal()) - ohne release() muss ein
        # zweiter Aufruf blockiert werden, BEVOR der Adapter erneut
        # aufgerufen wird.
        second = await safety.execute_safely(order, adapter.place_order)

        assert second.raw_response.get("duplicate") is True
        assert len(adapter._native_client.orders) == 1
        assert first.exchange_order_id == second.raw_response.get("original_exchange_order_id")


class TestKillSwitchAgainstPionexWritePath:
    """Beweist: KillSwitch._cancel_all_orders() (unveraendert) erreicht
    den echten Pionex-Adapter ueber den ExchangePool."""

    async def test_trigger_cancels_open_pionex_orders(self, patch_client) -> None:
        pool, adapter = await _connected_pool_and_adapter(patch_client)
        limit_order = OrderRequest(
            signal_id=uuid4(),
            symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX),
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            limit_price=Decimal("58000"),
            trading_mode=TradingMode.LIVE,
        )
        await adapter.place_order(limit_order)

        kill_switch = KillSwitch(TradingMode.LIVE)
        kill_switch.inject_exchange_pool(pool)

        await kill_switch.trigger(reason="test", triggered_by="test")

        assert adapter._native_client.cancel_all_calls == ["BTC_USDT_PERP"]
        assert all(o["status"] == "CLOSED" for o in adapter._native_client.orders.values())
