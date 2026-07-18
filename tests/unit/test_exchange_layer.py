"""
Tests für den Exchange Layer.
Alle Tests laufen gegen MockExchangeAdapter – keine echten API-Calls.

Tested:
- Adapter Lifecycle (connect, close, ping)
- Market Data Methoden (Rückgabetypen, Wertebereiche)
- Order Flow (place, cancel, get)
- Error Injection (RateLimitError, InsufficientFunds)
- Paper Simulation (fill price, fees)
- Factory Pattern
- ExchangePool
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
from sgr.exchanges.base import (
    ExchangeConnectionError,
    InsufficientFundsError,
    RateLimitError,
)
from sgr.exchanges.factory import ExchangeFactory, ExchangePool
from tests.mocks.mock_exchange import MockExchangeAdapter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> MockExchangeAdapter:
    return MockExchangeAdapter(trading_mode=TradingMode.PAPER)


@pytest.fixture
async def connected_adapter() -> MockExchangeAdapter:
    a = MockExchangeAdapter(trading_mode=TradingMode.PAPER)
    await a.connect()
    return a


@pytest.fixture
def btc_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


@pytest.fixture
def sample_order(btc_symbol: Symbol) -> OrderRequest:
    return OrderRequest(
        id=uuid4(),
        signal_id=uuid4(),
        symbol=btc_symbol,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.1"),
        trading_mode=TradingMode.PAPER,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestAdapterLifecycle:
    async def test_connect_sets_connected(self, adapter: MockExchangeAdapter) -> None:
        assert not adapter._connected
        await adapter.connect()
        assert adapter._connected

    async def test_close_disconnects(self, adapter: MockExchangeAdapter) -> None:
        await adapter.connect()
        await adapter.close()
        assert not adapter._connected

    async def test_close_idempotent(self, adapter: MockExchangeAdapter) -> None:
        await adapter.connect()
        await adapter.close()
        await adapter.close()  # Should not raise

    async def test_ping_returns_latency(self, connected_adapter: MockExchangeAdapter) -> None:
        latency = await connected_adapter.ping()
        assert latency > 0

    async def test_ping_with_simulated_latency(self) -> None:
        adapter = MockExchangeAdapter(simulated_latency_ms=50.0)
        await adapter.connect()
        latency = await adapter.ping()
        assert latency == pytest.approx(50.0)

    async def test_connect_tracked(self, adapter: MockExchangeAdapter) -> None:
        await adapter.connect()
        assert adapter.call_count("connect") == 1


# ---------------------------------------------------------------------------
# Market Data
# ---------------------------------------------------------------------------


class TestMarketData:
    async def test_get_ticker_returns_ticker(self, connected_adapter: MockExchangeAdapter) -> None:
        ticker = await connected_adapter.get_ticker("BTC/USDT")
        assert ticker.symbol == "BTC/USDT"
        assert ticker.bid < ticker.ask
        assert ticker.last > 0

    async def test_get_ticker_tracked(self, connected_adapter: MockExchangeAdapter) -> None:
        await connected_adapter.get_ticker("BTC/USDT")
        assert connected_adapter.call_count("get_ticker") == 1
        assert connected_adapter.last_call("get_ticker") == {"symbol": "BTC/USDT"}

    async def test_get_orderbook_correct_depth(
        self, connected_adapter: MockExchangeAdapter
    ) -> None:
        ob = await connected_adapter.get_orderbook("BTC/USDT", depth=10)
        assert len(ob.bids) == 10
        assert len(ob.asks) == 10

    async def test_get_orderbook_bid_lt_ask(self, connected_adapter: MockExchangeAdapter) -> None:
        ob = await connected_adapter.get_orderbook("BTC/USDT")
        assert ob.best_bid < ob.best_ask

    async def test_get_ohlcv_returns_candles(self, connected_adapter: MockExchangeAdapter) -> None:
        candles = await connected_adapter.get_ohlcv("BTC/USDT", "1h", limit=100)
        assert len(candles) == 100
        for c in candles:
            assert c.high >= c.low
            assert c.volume >= 0

    async def test_get_ohlcv_custom_price(self, connected_adapter: MockExchangeAdapter) -> None:
        connected_adapter.ticker_price = Decimal("30000")
        candles = await connected_adapter.get_ohlcv("BTC/USDT", "1h", limit=5)
        assert len(candles) == 5

    async def test_get_exchange_info(self, connected_adapter: MockExchangeAdapter) -> None:
        info = await connected_adapter.get_exchange_info()
        assert "BTC/USDT" in info.symbols
        assert info.taker_fee > 0

    async def test_get_balance(self, connected_adapter: MockExchangeAdapter) -> None:
        balance = await connected_adapter.get_balance()
        assert balance.total > 0
        assert balance.free <= balance.total
        assert "USDT" in balance.assets

    async def test_get_positions_empty_default(
        self, connected_adapter: MockExchangeAdapter
    ) -> None:
        positions = await connected_adapter.get_positions()
        assert positions == []


# ---------------------------------------------------------------------------
# Order Management
# ---------------------------------------------------------------------------


class TestOrderManagement:
    async def test_place_order_returns_result(
        self,
        connected_adapter: MockExchangeAdapter,
        sample_order: OrderRequest,
    ) -> None:
        result = await connected_adapter.place_order(sample_order)
        assert result.request_id == sample_order.id
        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == sample_order.quantity

    async def test_place_order_calculates_fees(
        self,
        connected_adapter: MockExchangeAdapter,
        sample_order: OrderRequest,
    ) -> None:
        result = await connected_adapter.place_order(sample_order)
        assert result.fees > 0

    async def test_place_order_paper_mode(
        self,
        connected_adapter: MockExchangeAdapter,
        sample_order: OrderRequest,
    ) -> None:
        result = await connected_adapter.place_order(sample_order)
        assert result.trading_mode == TradingMode.PAPER

    async def test_cancel_order_returns_true(
        self,
        connected_adapter: MockExchangeAdapter,
    ) -> None:
        result = await connected_adapter.cancel_order("ORDER-123", "BTC/USDT")
        assert result is True

    async def test_cancel_all_orders(
        self,
        connected_adapter: MockExchangeAdapter,
    ) -> None:
        count = await connected_adapter.cancel_all_orders()
        assert isinstance(count, int)

    async def test_get_open_orders_empty(
        self,
        connected_adapter: MockExchangeAdapter,
    ) -> None:
        orders = await connected_adapter.get_open_orders()
        assert orders == []

    async def test_place_order_tracked(
        self,
        connected_adapter: MockExchangeAdapter,
        sample_order: OrderRequest,
    ) -> None:
        await connected_adapter.place_order(sample_order)
        assert connected_adapter.call_count("place_order") == 1


# ---------------------------------------------------------------------------
# Error Injection
# ---------------------------------------------------------------------------


class TestErrorInjection:
    async def test_rate_limit_error(self, connected_adapter: MockExchangeAdapter) -> None:
        connected_adapter.inject_error(RateLimitError("binance", retry_after_seconds=1.0))
        with pytest.raises(RateLimitError) as exc_info:
            await connected_adapter.get_ticker("BTC/USDT")
        assert exc_info.value.retryable is True
        assert exc_info.value.retry_after_seconds == 1.0

    async def test_insufficient_funds_error(
        self,
        connected_adapter: MockExchangeAdapter,
        sample_order: OrderRequest,
    ) -> None:
        connected_adapter.inject_error(
            InsufficientFundsError("binance", Decimal("1000"), Decimal("100"))
        )
        with pytest.raises(InsufficientFundsError) as exc_info:
            await connected_adapter.place_order(sample_order)
        assert exc_info.value.retryable is False

    async def test_connection_error_is_retryable(
        self,
        connected_adapter: MockExchangeAdapter,
    ) -> None:
        connected_adapter.inject_error(ExchangeConnectionError("binance", "timeout"))
        with pytest.raises(ExchangeConnectionError) as exc_info:
            await connected_adapter.ping()
        assert exc_info.value.retryable is True

    async def test_error_consumed_after_raise(
        self,
        connected_adapter: MockExchangeAdapter,
    ) -> None:
        """Error is injected once – next call succeeds."""
        connected_adapter.inject_error(RateLimitError("binance"))
        with pytest.raises(RateLimitError):
            await connected_adapter.get_ticker("BTC/USDT")
        # Second call succeeds
        ticker = await connected_adapter.get_ticker("BTC/USDT")
        assert ticker is not None


# ---------------------------------------------------------------------------
# Paper Simulation
# ---------------------------------------------------------------------------


class TestPaperSimulation:
    async def test_paper_order_fills_immediately(
        self,
        connected_adapter: MockExchangeAdapter,
        sample_order: OrderRequest,
    ) -> None:
        result = await connected_adapter.place_order(sample_order)
        assert result.status == OrderStatus.FILLED
        assert result.filled_at is not None

    async def test_paper_fill_uses_current_price(
        self,
        connected_adapter: MockExchangeAdapter,
        btc_symbol: Symbol,
    ) -> None:
        connected_adapter.ticker_price = Decimal("42000")
        order = OrderRequest(
            id=uuid4(),
            signal_id=uuid4(),
            symbol=btc_symbol,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1.0"),
            trading_mode=TradingMode.PAPER,
        )
        result = await connected_adapter.place_order(order)
        # Fill price should be near ticker price
        assert result.average_fill_price is not None
        assert abs(result.average_fill_price - Decimal("42000")) < Decimal("100")

    async def test_partial_fill_simulation(
        self,
        connected_adapter: MockExchangeAdapter,
        sample_order: OrderRequest,
    ) -> None:
        connected_adapter.order_fill_status = OrderStatus.PARTIALLY_FILLED
        result = await connected_adapter.place_order(sample_order)
        assert result.status == OrderStatus.PARTIALLY_FILLED


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestExchangeFactory:
    def test_available_exchanges(self) -> None:
        exchanges = ExchangeFactory.available_exchanges()
        assert ExchangeID.BINANCE in exchanges
        assert ExchangeID.PIONEX in exchanges

    def test_register_custom_adapter(self) -> None:
        """Test registering a new exchange via decorator."""

        @ExchangeFactory.register(ExchangeID.BYBIT)
        class BybitMock(MockExchangeAdapter):
            exchange_id = ExchangeID.BYBIT

            @classmethod
            def from_config(cls, trading_mode: TradingMode, **kwargs):  # type: ignore
                return cls(trading_mode=trading_mode)

        assert ExchangeID.BYBIT in ExchangeFactory.available_exchanges()


# ---------------------------------------------------------------------------
# Exchange Pool
# ---------------------------------------------------------------------------


class TestExchangePool:
    async def test_pool_get_after_initialize(self) -> None:
        """Pool returns connected adapter after initialize."""
        # We can't test real ExchangeFactory.create with mock easily here,
        # so we test pool mechanics directly
        pool = ExchangePool()

        # Manually insert mock adapter
        mock = MockExchangeAdapter(TradingMode.PAPER)
        await mock.connect()
        pool._adapters[(ExchangeID.BINANCE, TradingMode.PAPER)] = mock

        adapter = pool.get(ExchangeID.BINANCE, TradingMode.PAPER)
        assert adapter._connected is True

    async def test_pool_get_missing_raises(self) -> None:
        pool = ExchangePool()
        with pytest.raises(KeyError):
            pool.get(ExchangeID.BINANCE, TradingMode.LIVE)

    async def test_pool_close_all(self) -> None:
        pool = ExchangePool()
        mock1 = MockExchangeAdapter(TradingMode.PAPER)
        mock2 = MockExchangeAdapter(TradingMode.PAPER)
        await mock1.connect()
        await mock2.connect()

        pool._adapters[(ExchangeID.BINANCE, TradingMode.PAPER)] = mock1
        pool._adapters[(ExchangeID.PIONEX, TradingMode.PAPER)] = mock2

        await pool.close_all()
        assert len(pool) == 0
        assert not mock1._connected
        assert not mock2._connected
