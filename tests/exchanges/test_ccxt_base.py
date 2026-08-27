"""
Tests for sgr.exchanges.ccxt_base.CCXTBaseAdapter.

Strategy: subclass CCXTBaseAdapter with a fake `_ccxt_id`, then monkeypatch
`ccxt.async_support.<id>` (via the module the adapter imports internally)
to point at a FakeCCXTExchange we fully control. This exercises the real
adapter code paths (retry, error mapping, parsing) against a controllable
double, without ever hitting the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import ccxt
import pytest

from sgr.core.types import (
    ExchangeID,
    OrderRequest,
    OrderType,
    Side,
    Symbol,
    TradingMode,
)
from sgr.exchanges.base import (
    ExchangeConnectionError,
    ExchangeMaintenanceError,
    InsufficientFundsError,
    NotSupportedFeatureError,
    OrderNotFoundError,
    RateLimitError,
    SymbolNotFoundError,
)
from sgr.exchanges.ccxt_base import CCXTBaseAdapter


class FakeCCXTExchange:
    """Minimal stand-in for a ccxt.async_support exchange instance."""

    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}
        self.markets = {"BTC/USDT": {"maker": 0.0008, "taker": 0.001}}
        self.symbols = ["BTC/USDT", "ETH/USDT"]
        self.timeframes = {"1m": "1m", "1h": "1h"}
        self.has: dict[str, bool] = {}
        self.closed = False

        self.load_markets = AsyncMock(return_value=self.markets)
        self.close = AsyncMock()
        self.fetch_time = AsyncMock(return_value=1_700_000_000_000)
        self.fetch_ticker = AsyncMock(
            return_value={
                "bid": "64900.0",
                "ask": "64901.0",
                "last": "64900.5",
                "quoteVolume": "1000",
                "percentage": 1.5,
                "timestamp": 1_700_000_000_000,
            }
        )
        self.fetch_order_book = AsyncMock(
            return_value={
                "bids": [["64900.0", "1.0"], ["64899.0", "2.0"]],
                "asks": [["64901.0", "1.5"]],
                "timestamp": 1_700_000_000_000,
            }
        )
        self.fetch_ohlcv = AsyncMock(
            return_value=[
                [1_700_000_000_000, "100", "110", "90", "105", "10"],
                [1_700_000_060_000, "105", "115", "95", "110", "12"],
            ]
        )
        self.fetch_funding_rate = AsyncMock(
            return_value={
                "timestamp": 1_700_000_000_000,
                "fundingRate": "0.0001",
                "fundingDatetime": "2023-11-14T22:13:20+00:00",
            }
        )
        self.fetch_open_interest = AsyncMock(
            return_value={
                "openInterest": "500",
                "openInterestValue": "32000000",
                "timestamp": 1_700_000_000_000,
            }
        )
        self.fetch_balance = AsyncMock(
            return_value={
                "total": {"USDT": "1000", "BTC": "0.5"},
                "free": {"USDT": "800"},
                "used": {"USDT": "200"},
            }
        )
        self.fetch_positions = AsyncMock(return_value=[])
        self.create_order = AsyncMock(
            return_value={
                "id": "12345",
                "status": "open",
                "amount": "1",
                "filled": "0",
                "timestamp": 1_700_000_000_000,
            }
        )
        self.cancel_order = AsyncMock(return_value=None)
        self.fetch_order = AsyncMock(
            return_value={
                "id": "12345",
                "status": "closed",
                "amount": "1",
                "filled": "1",
                "timestamp": 1_700_000_000_000,
                "side": "buy",
            }
        )
        self.fetch_open_orders = AsyncMock(return_value=[])
        self.cancel_all_orders = AsyncMock(return_value=[])


class FakeAdapter(CCXTBaseAdapter):
    exchange_id = ExchangeID.BINANCE
    _ccxt_id = "fakeexchange"


def install_fake_ccxt(monkeypatch, fake_instance: FakeCCXTExchange | None = None):
    """
    Patches ccxt.async_support so that getattr(ccxt.async_support, "fakeexchange")
    returns a factory producing our FakeCCXTExchange.
    """
    import ccxt.async_support as ccxt_async

    holder = {"instance": fake_instance}

    def factory(options=None):
        inst = holder["instance"] or FakeCCXTExchange(options)
        holder["instance"] = inst
        return inst

    monkeypatch.setattr(ccxt_async, "fakeexchange", factory, raising=False)
    return holder


@pytest.fixture
def adapter():
    return FakeAdapter(
        api_key="key",
        secret="secret",
        trading_mode=TradingMode.LIVE,
    )


def make_order_request(side=Side.BUY, order_type=OrderType.MARKET, **kwargs) -> OrderRequest:
    return OrderRequest(
        id=uuid4(),
        signal_id=uuid4(),
        symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE),
        side=side,
        order_type=order_type,
        quantity=Decimal("1"),
        trading_mode=kwargs.pop("trading_mode", TradingMode.LIVE),
        **kwargs,
    )


# ---------------------------------------------------------------------
# Lifecycle: connect / close / ping
# ---------------------------------------------------------------------


class TestLifecycle:
    async def test_connect_success(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        assert adapter._connected is True
        assert adapter._ccxt is not None

    async def test_connect_paper_mode_uses_testnet_urls(self, monkeypatch):
        class PaperAdapter(CCXTBaseAdapter):
            exchange_id = ExchangeID.BINANCE
            _ccxt_id = "fakeexchange"
            _testnet_urls = {"public": "https://testnet.example.com"}

        holder = install_fake_ccxt(monkeypatch)
        paper_adapter = PaperAdapter(api_key="k", secret="s", trading_mode=TradingMode.PAPER)
        await paper_adapter.connect()
        assert holder["instance"].options["urls"] == {
            "api": {"public": "https://testnet.example.com"}
        }

    async def test_connect_ccxt_not_installed(self, adapter, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "ccxt.async_support":
                raise ImportError("no ccxt")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(RuntimeError, match="ccxt not installed"):
            await adapter.connect()

    async def test_connect_extra_options_merged(self, monkeypatch, adapter):
        adapter._extra_options = {"password": "passphrase"}
        holder = install_fake_ccxt(monkeypatch)
        await adapter.connect()
        assert holder["instance"].options["password"] == "passphrase"

    async def test_connect_load_markets_failure_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.load_markets = AsyncMock(side_effect=ccxt.NetworkError("boom"))
        install_fake_ccxt(monkeypatch, fake)
        with pytest.raises(ExchangeConnectionError):
            await adapter.connect()
        assert adapter._connected is False

    async def test_close_when_never_connected_is_noop(self, adapter):
        await adapter.close()  # should not raise

    async def test_close_after_connect(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        await adapter.close()
        assert adapter._connected is False
        assert adapter._ccxt is None

    async def test_close_swallows_exceptions(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        adapter._ccxt.close = AsyncMock(side_effect=RuntimeError("fail on close"))
        await adapter.close()  # must not raise
        assert adapter._connected is False

    async def test_ping_success(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        latency = await adapter.ping()
        assert latency >= 0

    async def test_ping_requires_connection(self, adapter):
        with pytest.raises(ExchangeConnectionError):
            await adapter.ping()

    async def test_ping_maps_errors(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        adapter._ccxt.fetch_time = AsyncMock(side_effect=RuntimeError("net down"))
        with pytest.raises(ExchangeConnectionError):
            await adapter.ping()


# ---------------------------------------------------------------------
# Market Data
# ---------------------------------------------------------------------


class TestMarketData:
    async def test_get_exchange_info_computes_and_caches(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        info = await adapter.get_exchange_info()
        assert "BTC/USDT" in info.symbols
        assert info.maker_fee == Decimal("0.0008")
        # second call should hit the cache path (exchange_info already set)
        info2 = await adapter.get_exchange_info()
        assert info2 is info

    async def test_get_exchange_info_requires_connection(self, adapter):
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_exchange_info()

    async def test_get_exchange_info_missing_btc_usdt_uses_defaults(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.markets = {}
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        info = await adapter.get_exchange_info()
        assert info.maker_fee == Decimal("0.001")

    async def test_get_exchange_info_bad_fee_value_uses_defaults(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.markets = {"BTC/USDT": {"maker": "not-a-number", "taker": 0.001}}
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        info = await adapter.get_exchange_info()
        # InvalidOperation is caught -> falls back to the pre-loop defaults
        assert info.maker_fee == Decimal("0.001")

    async def test_get_ticker(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        ticker = await adapter.get_ticker("BTC/USDT")
        assert ticker.bid == Decimal("64900.0")
        assert ticker.ask == Decimal("64901.0")

    async def test_get_ticker_requires_connection(self, adapter):
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_ticker("BTC/USDT")

    async def test_get_ticker_falls_back_to_last_price(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_ticker = AsyncMock(return_value={"last": "100", "timestamp": None})
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        ticker = await adapter.get_ticker("BTC/USDT")
        assert ticker.bid == Decimal("100")
        assert ticker.ask == Decimal("100")

    async def test_get_ticker_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_ticker = AsyncMock(side_effect=ccxt.BadSymbol("bad"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(SymbolNotFoundError):
            await adapter.get_ticker("XXX/YYY")

    async def test_get_ticker_maps_generic_non_ccxt_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_ticker = AsyncMock(side_effect=ValueError("totally unrelated"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017 - generic mapped ExchangeError
            await adapter.get_ticker("BTC/USDT")

    async def test_get_orderbook(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        ob = await adapter.get_orderbook("BTC/USDT", depth=5)
        assert len(ob.bids) == 2
        assert ob.bids[0].price == Decimal("64900.0")
        assert len(ob.asks) == 1

    async def test_get_orderbook_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_order_book = AsyncMock(side_effect=ccxt.ExchangeNotAvailable("down"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_orderbook("BTC/USDT")

    async def test_get_orderbook_maps_generic_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_order_book = AsyncMock(side_effect=KeyError("weird"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.get_orderbook("BTC/USDT")

    async def test_get_ohlcv(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        candles = await adapter.get_ohlcv("BTC/USDT", "1m", since=datetime.now(tz=UTC), limit=2)
        assert len(candles) == 2
        assert candles[0].timestamp <= candles[1].timestamp

    async def test_get_ohlcv_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_ohlcv = AsyncMock(side_effect=ValueError("bad data"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.get_ohlcv("BTC/USDT", "1m")

    async def test_get_ohlcv_maps_ccxt_network_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_ohlcv = AsyncMock(side_effect=ccxt.NetworkError("down"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_ohlcv("BTC/USDT", "1m")

    async def test_get_funding_rate(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        rate = await adapter.get_funding_rate("BTC/USDT")
        assert rate.rate == Decimal("0.0001")

    async def test_get_funding_rate_not_supported(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.has = {"fetchFundingRate": False}
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(NotSupportedFeatureError):
            await adapter.get_funding_rate("BTC/USDT")

    async def test_get_funding_rate_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_funding_rate = AsyncMock(side_effect=ccxt.NetworkError("x"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_funding_rate("BTC/USDT")

    async def test_get_funding_rate_maps_generic_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_funding_rate = AsyncMock(side_effect=TypeError("weird"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.get_funding_rate("BTC/USDT")

    async def test_get_open_interest(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        oi = await adapter.get_open_interest("BTC/USDT")
        assert oi.open_interest == Decimal("500")

    async def test_get_open_interest_not_supported(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.has = {"fetchOpenInterest": False}
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(NotSupportedFeatureError):
            await adapter.get_open_interest("BTC/USDT")

    async def test_get_open_interest_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_open_interest = AsyncMock(side_effect=ccxt.NetworkError("x"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_open_interest("BTC/USDT")

    async def test_get_open_interest_maps_generic_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_open_interest = AsyncMock(side_effect=TypeError("weird"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.get_open_interest("BTC/USDT")


# ---------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------


class TestAccount:
    async def test_get_balance(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        balance = await adapter.get_balance()
        assert balance.total == Decimal("1000")
        assert balance.free == Decimal("800")
        assert balance.used == Decimal("200")
        assert balance.assets["BTC"] == Decimal("0.5")

    async def test_get_balance_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_balance = AsyncMock(side_effect=ccxt.NetworkError("x"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_balance()

    async def test_get_balance_maps_generic_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_balance = AsyncMock(side_effect=TypeError("weird"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.get_balance()

    async def test_get_positions_spot_only_returns_empty(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.has = {"fetchPositions": False}
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        positions = await adapter.get_positions()
        assert positions == []
        fake.fetch_positions.assert_not_called()

    async def test_get_positions_filters_empty_and_parses(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.has = {"fetchPositions": True}
        fake.fetch_positions = AsyncMock(
            return_value=[
                {"contracts": "0", "symbol": "BTC/USDT:USDT"},  # filtered out
                {
                    "contracts": "2",
                    "side": "short",
                    "symbol": "BTC/USDT:USDT",
                    "entryPrice": "60000",
                    "markPrice": "61000",
                    "leverage": "5",
                    "unrealizedPnl": "-100",
                    "realizedPnl": "0",
                },
            ]
        )
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        positions = await adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("2")
        assert positions[0].leverage == Decimal("5")

    async def test_get_positions_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.has = {"fetchPositions": True}
        fake.fetch_positions = AsyncMock(side_effect=ccxt.NetworkError("x"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_positions()

    async def test_get_positions_maps_generic_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.has = {"fetchPositions": True}
        fake.fetch_positions = AsyncMock(side_effect=TypeError("weird"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.get_positions()


# ---------------------------------------------------------------------
# Order Management
# ---------------------------------------------------------------------


class TestOrderManagement:
    async def test_place_order_live(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        req = make_order_request()
        result = await adapter.place_order(req)
        assert result.exchange_order_id == "12345"

    async def test_place_order_live_with_limit_price_and_reduce_only(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        req = make_order_request(
            order_type=OrderType.LIMIT,
            limit_price=Decimal("65000"),
            reduce_only=True,
        )
        result = await adapter.place_order(req)
        assert result.exchange_order_id == "12345"
        _, kwargs = adapter._ccxt.create_order.call_args
        assert kwargs["params"] == {"reduceOnly": True}
        assert kwargs["price"] == 65000.0

    async def test_place_order_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.create_order = AsyncMock(side_effect=ccxt.InsufficientFunds("no funds"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        req = make_order_request()
        with pytest.raises(InsufficientFundsError):
            await adapter.place_order(req)

    async def test_place_order_maps_generic_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.create_order = AsyncMock(side_effect=TypeError("weird"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        req = make_order_request()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.place_order(req)

    async def test_place_order_paper_mode_simulates(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        req = make_order_request(trading_mode=TradingMode.PAPER)
        result = await adapter.place_order(req)
        assert result.status.value == "filled"
        assert result.exchange_order_id.startswith("PAPER-")
        assert result.average_fill_price > Decimal("64901.0")  # buy: ask + slippage

    async def test_simulate_order_sell_side_uses_bid(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        req = make_order_request(side=Side.SELL, trading_mode=TradingMode.PAPER)
        result = await adapter.place_order(req)
        assert result.average_fill_price < Decimal("64900.0")  # sell: bid - slippage

    async def test_simulate_order_ticker_failure_uses_limit_price_fallback(
        self, adapter, monkeypatch
    ):
        fake = FakeCCXTExchange()
        fake.fetch_ticker = AsyncMock(side_effect=RuntimeError("ticker down"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        req = make_order_request(trading_mode=TradingMode.PAPER, limit_price=Decimal("50000"))
        result = await adapter.place_order(req)
        assert result.average_fill_price == Decimal("50000")

    async def test_cancel_order_success(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        assert await adapter.cancel_order("123", "BTC/USDT") is True

    async def test_cancel_order_not_found_returns_false(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.cancel_order = AsyncMock(side_effect=ccxt.OrderNotFound("no such order"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        assert await adapter.cancel_order("123", "BTC/USDT") is False

    async def test_cancel_order_other_error_raises(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.cancel_order = AsyncMock(side_effect=ccxt.NetworkError("down"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.cancel_order("123", "BTC/USDT")

    async def test_get_order(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        result = await adapter.get_order("12345", "BTC/USDT")
        assert result.status.value == "filled"
        assert result.filled_quantity == Decimal("1")

    async def test_get_order_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_order = AsyncMock(side_effect=ccxt.OrderNotFound("missing"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(OrderNotFoundError):
            await adapter.get_order("nope", "BTC/USDT")

    async def test_get_order_maps_generic_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_order = AsyncMock(side_effect=TypeError("weird"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.get_order("1", "BTC/USDT")

    async def test_get_open_orders_empty(self, adapter, monkeypatch):
        install_fake_ccxt(monkeypatch)
        await adapter.connect()
        assert await adapter.get_open_orders() == []

    async def test_get_open_orders_parses_results(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_open_orders = AsyncMock(
            return_value=[
                {
                    "id": "1",
                    "symbol": "BTC/USDT",
                    "side": "sell",
                    "amount": "1",
                    "status": "open",
                    "filled": "0",
                    "timestamp": 1_700_000_000_000,
                }
            ]
        )
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        orders = await adapter.get_open_orders("BTC/USDT")
        assert len(orders) == 1
        assert orders[0].exchange_order_id == "1"

    async def test_get_open_orders_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_open_orders = AsyncMock(side_effect=ccxt.NetworkError("x"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_open_orders()

    async def test_get_open_orders_maps_generic_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_open_orders = AsyncMock(side_effect=TypeError("weird"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.get_open_orders()

    async def test_cancel_all_orders_with_symbol(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.cancel_all_orders = AsyncMock(return_value=[{"id": "1"}, {"id": "2"}])
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        count = await adapter.cancel_all_orders("BTC/USDT")
        assert count == 2

    async def test_cancel_all_orders_no_symbol_iterates_open_orders(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_open_orders = AsyncMock(
            return_value=[
                {
                    "id": "1",
                    "symbol": "BTC/USDT",
                    "side": "buy",
                    "amount": "1",
                    "status": "open",
                    "filled": "0",
                },
                {
                    "id": "2",
                    "symbol": "ETH/USDT",
                    "side": "buy",
                    "amount": "1",
                    "status": "open",
                    "filled": "0",
                },
            ]
        )
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        count = await adapter.cancel_all_orders()
        assert count == 2
        assert fake.cancel_all_orders.call_count == 2

    async def test_cancel_all_orders_no_symbol_partial_failure_logged_and_skipped(
        self, adapter, monkeypatch
    ):
        fake = FakeCCXTExchange()
        fake.fetch_open_orders = AsyncMock(
            return_value=[
                {
                    "id": "1",
                    "symbol": "BTC/USDT",
                    "side": "buy",
                    "amount": "1",
                    "status": "open",
                    "filled": "0",
                },
            ]
        )
        fake.cancel_all_orders = AsyncMock(side_effect=RuntimeError("fail"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        count = await adapter.cancel_all_orders()
        assert count == 0

    async def test_cancel_all_orders_maps_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.cancel_all_orders = AsyncMock(side_effect=ccxt.NetworkError("x"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.cancel_all_orders("BTC/USDT")

    async def test_cancel_all_orders_maps_generic_error(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.cancel_all_orders = AsyncMock(side_effect=TypeError("weird"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(Exception):  # noqa: B017
            await adapter.cancel_all_orders("BTC/USDT")

    async def test_cancel_all_orders_result_not_list_returns_zero(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.cancel_all_orders = AsyncMock(return_value={"status": "ok"})
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        count = await adapter.cancel_all_orders("BTC/USDT")
        assert count == 0


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


class TestHelpers:
    def test_require_connected_raises_when_not_connected(self, adapter):
        with pytest.raises(ExchangeConnectionError):
            adapter._require_connected()

    def test_require_feature_ccxt_none_is_noop(self, adapter):
        adapter._require_feature("fetchPositions")  # no ccxt instance -> no raise

    def test_parse_symbol_spot(self, adapter):
        sym = adapter._parse_symbol("BTC/USDT")
        assert sym.base == "BTC"
        assert sym.quote == "USDT"
        assert sym.asset_class.value == "spot"

    def test_parse_symbol_futures(self, adapter):
        sym = adapter._parse_symbol("BTC/USDT:USDT")
        assert sym.asset_class.value == "futures"

    def test_parse_symbol_invalid_raises(self, adapter):
        with pytest.raises(SymbolNotFoundError):
            adapter._parse_symbol("INVALID")

    def test_parse_ts_none_returns_now(self, adapter):
        ts = adapter._parse_ts(None)
        assert ts.tzinfo is not None

    def test_parse_ts_int_ms(self, adapter):
        ts = adapter._parse_ts(1_700_000_000_000)
        assert ts.year == 2023

    def test_parse_ts_iso_string(self, adapter):
        ts = adapter._parse_ts("2023-11-14T22:13:20Z")
        assert ts.year == 2023

    def test_parse_ts_datetime_naive_gets_utc(self, adapter):
        naive = datetime(2023, 1, 1)
        ts = adapter._parse_ts(naive)
        assert ts.tzinfo is not None

    def test_parse_ts_datetime_aware_passthrough(self, adapter):
        aware = datetime(2023, 1, 1, tzinfo=UTC)
        ts = adapter._parse_ts(aware)
        assert ts == aware

    def test_parse_ts_unknown_type_returns_now(self, adapter):
        ts = adapter._parse_ts(object())
        assert ts.tzinfo is not None

    def test_parse_order_result_partial_fill_status_override(self, adapter):
        req = make_order_request()
        raw = {
            "id": "1",
            "status": "open",
            "amount": "10",
            "filled": "4",
            "fee": {"cost": "0.5", "currency": "USDT"},
            "average": "100",
            "timestamp": 1_700_000_000_000,
            "lastTradeTimestamp": 1_700_000_100_000,
        }
        result = adapter._parse_order_result(raw, req)
        assert result.status.value == "partially_filled"
        assert result.filled_quantity == Decimal("4")
        assert result.fees == Decimal("0.5")
        assert result.filled_at is not None

    def test_parse_order_result_no_lasttrade_timestamp(self, adapter):
        req = make_order_request()
        raw = {"id": "1", "status": "open", "amount": "1", "filled": "0"}
        result = adapter._parse_order_result(raw, req)
        assert result.filled_at is None

    def test_parse_order_result_unknown_status_defaults_submitted(self, adapter):
        req = make_order_request()
        raw = {"id": "1", "status": "weird_status", "amount": "1", "filled": "0"}
        result = adapter._parse_order_result(raw, req)
        assert result.status.value == "submitted"

    @pytest.mark.parametrize(
        ("order_type", "expected"),
        [
            (OrderType.MARKET, "market"),
            (OrderType.LIMIT, "limit"),
            (OrderType.STOP_MARKET, "stop_market"),
            (OrderType.STOP_LIMIT, "stop_limit"),
            (OrderType.TWAP, "limit"),
        ],
    )
    def test_map_order_type(self, adapter, order_type, expected):
        assert adapter._map_order_type(order_type) == expected


# ---------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------


class TestErrorMapping:
    def test_rate_limit_exceeded(self, adapter):
        mapped = adapter._map_error(ccxt.RateLimitExceeded("slow down"))
        assert isinstance(mapped, RateLimitError)

    def test_network_error(self, adapter):
        mapped = adapter._map_error(ccxt.NetworkError("down"))
        assert isinstance(mapped, ExchangeConnectionError)

    def test_onmaintenance_is_shadowed_by_networkerror_check(self, adapter):
        """
        ccxt.OnMaintenance subclasses ccxt.NetworkError, and the NetworkError
        check in _map_error runs first, so OnMaintenance is unreachable in
        practice: maintenance errors get classified as the generic
        ExchangeConnectionError instead of ExchangeMaintenanceError.
        Both are retryable=True so there's no acute behavioral risk, but this
        is misleading for monitoring/alerting. Documented per deferred
        finding; not fixed here without an explicit decision from Gordon.
        """
        mapped = adapter._map_error(ccxt.OnMaintenance("under maintenance"))
        assert isinstance(mapped, ExchangeConnectionError)
        assert not isinstance(mapped, ExchangeMaintenanceError)
        assert mapped.retryable is True

    def test_requesttimeout_is_shadowed_by_networkerror_check(self, adapter):
        """
        Same shadowing issue: ccxt.RequestTimeout also subclasses
        ccxt.NetworkError, so the dedicated RequestTimeout branch below the
        NetworkError check is unreachable too.
        """
        mapped = adapter._map_error(ccxt.RequestTimeout("timed out"))
        assert isinstance(mapped, ExchangeConnectionError)

    def test_insufficient_funds(self, adapter):
        mapped = adapter._map_error(ccxt.InsufficientFunds("no money"))
        assert isinstance(mapped, InsufficientFundsError)

    def test_order_not_found(self, adapter):
        mapped = adapter._map_error(ccxt.OrderNotFound("gone"))
        assert isinstance(mapped, OrderNotFoundError)

    def test_bad_symbol(self, adapter):
        mapped = adapter._map_error(ccxt.BadSymbol("wat"))
        assert isinstance(mapped, SymbolNotFoundError)

    def test_not_supported(self, adapter):
        mapped = adapter._map_error(ccxt.NotSupported("nope"))
        assert isinstance(mapped, NotSupportedFeatureError)

    def test_generic_transient_keyword_match(self, adapter):
        mapped = adapter._map_error(RuntimeError("connection reset by peer"))
        assert isinstance(mapped, ExchangeConnectionError)

    def test_generic_transient_503(self, adapter):
        mapped = adapter._map_error(RuntimeError("HTTP 503 service unavailable"))
        assert isinstance(mapped, ExchangeConnectionError)

    def test_generic_permanent_error(self, adapter):
        mapped = adapter._map_error(ValueError("totally unrelated failure"))
        assert type(mapped).__name__ == "ExchangeError"
        assert mapped.retryable is False

    def test_map_error_ccxt_import_failure_returns_generic(self, adapter, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "ccxt" and not args[0:1] == ({},):
                raise ImportError("no ccxt")
            return real_import(name, *args, **kwargs)

        # Simpler: patch the "ccxt" name specifically for this bare import
        def fake_import2(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "ccxt" and fromlist == ():
                raise ImportError("no ccxt")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import2)
        mapped = adapter._map_error(RuntimeError("whatever"))
        assert type(mapped).__name__ == "ExchangeError"
        assert mapped.retryable is False


# ---------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------


class TestRetryBehavior:
    async def test_retryable_error_is_retried_then_succeeds(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_ticker = AsyncMock(
            side_effect=[ccxt.NetworkError("flaky"), fake.fetch_ticker.return_value]
        )
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        ticker = await adapter.get_ticker("BTC/USDT")
        assert ticker.bid == Decimal("64900.0")
        assert fake.fetch_ticker.call_count == 2

    async def test_non_retryable_error_raises_immediately(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_ticker = AsyncMock(side_effect=ccxt.BadSymbol("bad"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(SymbolNotFoundError):
            await adapter.get_ticker("BTC/USDT")
        assert fake.fetch_ticker.call_count == 1

    async def test_retryable_error_exhausts_attempts(self, adapter, monkeypatch):
        fake = FakeCCXTExchange()
        fake.fetch_ticker = AsyncMock(side_effect=ccxt.NetworkError("always flaky"))
        install_fake_ccxt(monkeypatch, fake)
        await adapter.connect()
        with pytest.raises(ExchangeConnectionError):
            await adapter.get_ticker("BTC/USDT")
        assert fake.fetch_ticker.call_count == 3  # max_attempts=3 for get_ticker
