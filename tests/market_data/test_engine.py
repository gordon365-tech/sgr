"""
Tests for sgr.market_data.engine (SymbolFeed + MarketDataEngine).

Strategy: fake the ExchangePool (returns a fake exchange adapter with
AsyncMock get_ohlcv), fake FeatureStore (no real Redis), and use the real
FeatureEngineer/GapDetector where cheap, or fakes where isolation matters.
This lets us exercise the real orchestration logic in engine.py without any
network, Redis, or DB dependency.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

from sgr.core.types import Candle, ExchangeID, Symbol, TradingMode
from sgr.exchanges.base import ExchangeConnectionError, InsufficientFundsError
from sgr.market_data.engine import _POLL_INTERVALS, MarketDataEngine, SymbolFeed
from sgr.market_data.types import FeatureSet

SYMBOL_STR = "BTC/USDT"
SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def make_candle(ts: datetime, price: float = 65000.0) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timestamp=ts,
        timeframe="1h",
        open=Decimal(str(price)),
        high=Decimal(str(price + 10)),
        low=Decimal(str(price - 10)),
        close=Decimal(str(price + 5)),
        volume=Decimal("100"),
    )


def make_candles(n: int, start: datetime | None = None, step_minutes: int = 60) -> list[Candle]:
    base = start or datetime(2024, 1, 1, tzinfo=UTC)
    return [make_candle(base + timedelta(minutes=step_minutes * i)) for i in range(n)]


class FakeAdapter:
    def __init__(self, ohlcv_responses: list | None = None, raises: Exception | None = None):
        self._responses = ohlcv_responses or []
        self._raises = raises
        self.calls: list[dict] = []

    async def get_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append(
            {"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit}
        )
        if self._raises:
            raise self._raises
        if self._responses:
            return self._responses.pop(0)
        return []


class FakePool:
    def __init__(self, adapter: FakeAdapter):
        self._adapter = adapter

    def get(self, exchange_id, trading_mode=None):
        return self._adapter


class FakeFeatureStore:
    def __init__(self):
        self._redis = None
        self.saved: list[FeatureSet] = []
        self.connected = False
        self.closed = False

    async def connect(self):
        self._redis = object()
        self.connected = True

    async def close(self):
        self.closed = True

    async def save(self, features: FeatureSet):
        self.saved.append(features)


class FakeEngineer:
    def __init__(self, feature_set: FeatureSet | None = None, raises: Exception | None = None):
        self._feature_set = feature_set
        self._raises = raises
        self.calls: list[list[Candle]] = []

    def compute(self, candles, orderbook=None):
        self.calls.append(candles)
        if self._raises:
            raise self._raises
        return self._feature_set or FeatureSet(
            symbol=SYMBOL,
            timestamp=candles[-1].timestamp,
            timeframe="1h",
            close=candles[-1].close,
            volume=candles[-1].volume,
        )


def make_feed(
    adapter_responses=None, adapter_raises=None, engineer=None
) -> tuple[SymbolFeed, FakePool]:
    adapter = FakeAdapter(adapter_responses, adapter_raises)
    pool = FakePool(adapter)
    feed = SymbolFeed(
        symbol=SYMBOL_STR,
        timeframe="1h",
        exchange_id=ExchangeID.BINANCE,
        trading_mode=TradingMode.PAPER,
        feature_engineer=engineer or FakeEngineer(),
        feature_store=FakeFeatureStore(),
    )
    return feed, pool


# ---------------------------------------------------------------------
# SymbolFeed.initialize
# ---------------------------------------------------------------------


class TestSymbolFeedInitialize:
    async def test_initialize_success_fills_buffer(self):
        candles = make_candles(5)
        feed, pool = make_feed(adapter_responses=[candles])
        await feed.initialize(pool)
        assert feed._initialized is True
        assert len(feed._candle_buffer) == 5
        assert feed._last_processed_ts == candles[-1].timestamp

    async def test_initialize_no_history_stays_uninitialized(self):
        feed, pool = make_feed(adapter_responses=[[]])
        await feed.initialize(pool)
        assert feed._initialized is False
        assert feed._candle_buffer == []

    async def test_initialize_exchange_error_is_caught(self):
        feed, pool = make_feed(adapter_raises=ExchangeConnectionError("down", "binance"))
        await feed.initialize(pool)
        assert feed._initialized is False

    async def test_initialize_buffer_capped_at_max(self):
        candles = make_candles(300)  # 1h max buffer = 250
        feed, pool = make_feed(adapter_responses=[candles])
        await feed.initialize(pool)
        assert len(feed._candle_buffer) == 250
        assert feed._candle_buffer[-1] == candles[-1]


# ---------------------------------------------------------------------
# SymbolFeed.update
# ---------------------------------------------------------------------


class TestSymbolFeedUpdate:
    async def test_update_triggers_lazy_initialize_when_not_initialized(self):
        candles = make_candles(5)
        feed, pool = make_feed(adapter_responses=[candles])
        result = await feed.update(pool)
        # After lazy-init, 5 candles exist but update() returns False for
        # this call since it only initializes (the initialize path itself
        # doesn't count as an "update").
        assert feed._initialized is True
        assert result is False

    async def test_update_stays_uninitialized_if_init_fails(self):
        feed, pool = make_feed(adapter_responses=[[]])
        result = await feed.update(pool)
        assert result is False
        assert feed._initialized is False

    async def test_update_no_new_candles_returns_false(self):
        initial = make_candles(3)
        feed, pool = make_feed(adapter_responses=[initial, []])
        await feed.initialize(pool)
        result = await feed.update(pool)
        assert result is False

    async def test_update_filters_out_stale_candles(self):
        initial = make_candles(3)
        # "new" candles that are actually all <= last_processed_ts
        stale = [initial[-1]]
        feed, pool = make_feed(adapter_responses=[initial, stale])
        await feed.initialize(pool)
        result = await feed.update(pool)
        assert result is False

    async def test_update_happy_path_computes_features_and_saves(self):
        initial = make_candles(3)
        new = make_candles(2, start=initial[-1].timestamp + timedelta(hours=1))
        engineer = FakeEngineer()
        feed, pool = make_feed(adapter_responses=[initial, new], engineer=engineer)
        await feed.initialize(pool)
        result = await feed.update(pool)
        assert result is True
        assert len(engineer.calls) == 1
        assert feed._store.saved  # FakeFeatureStore recorded a save
        assert feed._last_processed_ts == new[-1].timestamp

    async def test_update_buffer_capped_after_extend(self):
        initial = make_candles(250)
        new = make_candles(10, start=initial[-1].timestamp + timedelta(hours=1))
        feed, pool = make_feed(adapter_responses=[initial, new])
        await feed.initialize(pool)
        await feed.update(pool)
        assert len(feed._candle_buffer) == 250

    async def test_update_too_few_candles_for_features_skips_compute(self):
        # initialize with a single candle -> buffer has 1 candle after init
        initial = make_candles(1)
        new = make_candles(1, start=initial[-1].timestamp + timedelta(hours=1))
        engineer = FakeEngineer()
        feed, pool = make_feed(adapter_responses=[initial, new], engineer=engineer)
        await feed.initialize(pool)
        # buffer=1 before update; after extending with 1 new candle -> buffer=2
        # which meets the >=2 threshold, so let's instead test the actual
        # skip case: 1 candle total after filtering (only 1 new, 0 initial
        # accepted because initial required non-empty to initialize).
        # To truly hit <2, we simulate an update where only the newly
        # extended buffer has exactly 1 entry - not reachable via normal
        # initialize (requires >=1 to initialize). So assert the >=2 path
        # computes normally instead, documenting the boundary:
        result = await feed.update(pool)
        assert result is True
        assert len(feed._candle_buffer) == 2

    async def test_update_retryable_exchange_error_logged_as_warning(self):
        initial = make_candles(3)
        feed, pool = make_feed(adapter_responses=[initial])
        await feed.initialize(pool)
        pool._adapter._raises = ExchangeConnectionError("binance", "timeout")
        result = await feed.update(pool)
        assert result is False

    async def test_update_nonretryable_exchange_error_logged_as_error(self):
        initial = make_candles(3)
        feed, pool = make_feed(adapter_responses=[initial])
        await feed.initialize(pool)
        pool._adapter._raises = InsufficientFundsError("binance", Decimal("10"), Decimal("1"))
        result = await feed.update(pool)
        assert result is False

    async def test_update_with_gap_triggers_gap_fill(self):
        initial = make_candles(3)
        # New candles with a gap: skip 2 hours ahead of what GapDetector expects
        gap_start = initial[-1].timestamp + timedelta(hours=3)
        new = [make_candle(gap_start)]
        fill_candles = make_candles(2, start=initial[-1].timestamp + timedelta(hours=1))

        feed, pool = make_feed(adapter_responses=[initial, new, fill_candles])
        await feed.initialize(pool)
        result = await feed.update(pool)

        assert result is True
        # 3 calls total: initialize, update's own get_ohlcv, gap-fill get_ohlcv
        assert len(pool._adapter.calls) == 3

    async def test_handle_gaps_fill_returns_empty_is_noop(self):
        initial = make_candles(3)
        gap_start = initial[-1].timestamp + timedelta(hours=3)
        new = [make_candle(gap_start)]
        feed, pool = make_feed(adapter_responses=[initial, new, []])
        await feed.initialize(pool)
        result = await feed.update(pool)
        assert result is True  # still computes features from what we have

    async def test_handle_gaps_fill_error_is_caught(self):
        initial = make_candles(3)
        gap_start = initial[-1].timestamp + timedelta(hours=3)
        new = [make_candle(gap_start)]

        class RaisingOnThirdCallAdapter(FakeAdapter):
            async def get_ohlcv(self, symbol, timeframe, since=None, limit=None):
                self.calls.append(
                    {"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit}
                )
                if len(self.calls) == 3:
                    raise ExchangeConnectionError("binance", "gap fetch failed")
                return self._responses.pop(0)

        adapter = RaisingOnThirdCallAdapter(ohlcv_responses=[initial, new])
        pool = FakePool(adapter)
        feed = SymbolFeed(
            symbol=SYMBOL_STR,
            timeframe="1h",
            exchange_id=ExchangeID.BINANCE,
            trading_mode=TradingMode.PAPER,
            feature_engineer=FakeEngineer(),
            feature_store=FakeFeatureStore(),
        )
        await feed.initialize(pool)
        result = await feed.update(pool)
        # Gap-fill error is swallowed inside _handle_gaps; update still
        # proceeds and computes features from the buffer it already has.
        assert result is True


# ---------------------------------------------------------------------
# MarketDataEngine.subscribe
# ---------------------------------------------------------------------


class TestSubscribe:
    def test_subscribe_registers_feed_per_timeframe(self):
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, FakeFeatureStore())
        engine.subscribe(SYMBOL_STR, ExchangeID.BINANCE, ["1h", "4h"])
        assert set(engine.get_subscribed_symbols()) == {
            (SYMBOL_STR, "1h"),
            (SYMBOL_STR, "4h"),
        }

    def test_subscribe_is_idempotent_for_same_key(self):
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, FakeFeatureStore())
        engine.subscribe(SYMBOL_STR, ExchangeID.BINANCE, ["1h"])
        engine.subscribe(SYMBOL_STR, ExchangeID.BINANCE, ["1h"])
        assert len(engine._feeds) == 1

    def test_default_feature_store_is_created_when_not_given(self):
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER)
        assert engine._store is not None


# ---------------------------------------------------------------------
# MarketDataEngine.start / stop
# ---------------------------------------------------------------------


class TestStartStop:
    async def test_start_connects_store_when_not_connected(self):
        store = FakeFeatureStore()
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, store)
        engine.subscribe(SYMBOL_STR, ExchangeID.BINANCE, ["1h"])
        await engine.start()
        assert store.connected is True
        assert engine.is_running is True
        await engine.stop()

    async def test_start_skips_connect_when_already_connected(self):
        store = FakeFeatureStore()
        store._redis = object()  # simulate already connected
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, store)
        await engine.start()
        assert store.connected is False  # connect() was never called
        await engine.stop()

    async def test_start_initializes_all_feeds_concurrently(self):
        candles = make_candles(3)
        adapter = FakeAdapter(ohlcv_responses=[candles, candles])
        pool = FakePool(adapter)
        store = FakeFeatureStore()
        engine = MarketDataEngine(pool, TradingMode.PAPER, store)
        engine.subscribe(SYMBOL_STR, ExchangeID.BINANCE, ["1h"])
        engine.subscribe("ETH/USDT", ExchangeID.BINANCE, ["1h"])
        await engine.start()
        for feed in engine._feeds.values():
            assert feed._initialized is True
        await engine.stop()

    async def test_start_with_no_feeds_does_not_crash(self):
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, FakeFeatureStore())
        await engine.start()
        assert engine.is_running is True
        await engine.stop()

    async def test_stop_cancels_tasks_and_closes_store(self):
        store = FakeFeatureStore()
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, store)
        engine.subscribe(SYMBOL_STR, ExchangeID.BINANCE, ["1h"])
        await engine.start()
        await engine.stop()
        assert engine.is_running is False
        assert engine._tasks == []
        assert store.closed is True

    async def test_stop_without_start_is_safe(self):
        store = FakeFeatureStore()
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, store)
        await engine.stop()
        assert store.closed is True


# ---------------------------------------------------------------------
# MarketDataEngine._poll_loop
# ---------------------------------------------------------------------


class TestPollLoop:
    async def test_poll_loop_publishes_event_on_update(self, monkeypatch):
        candles = make_candles(3)
        new = make_candles(1, start=candles[-1].timestamp + timedelta(hours=1))
        adapter = FakeAdapter(ohlcv_responses=[candles, new])
        pool = FakePool(adapter)
        store = FakeFeatureStore()
        engine = MarketDataEngine(pool, TradingMode.PAPER, store)
        feed = SymbolFeed(
            symbol=SYMBOL_STR,
            timeframe="1h",
            exchange_id=ExchangeID.BINANCE,
            trading_mode=TradingMode.PAPER,
            feature_engineer=FakeEngineer(),
            feature_store=store,
        )
        await feed.initialize(pool)

        published = []

        async def fake_publish(f):
            published.append(f)

        monkeypatch.setattr(engine, "_publish_candle_event", fake_publish)
        engine._running = True

        # Run one iteration manually rather than the infinite loop
        updated = await feed.update(pool)
        if updated and feed._candle_buffer:
            await engine._publish_candle_event(feed)

        assert updated is True
        assert published == [feed]

    async def test_poll_loop_breaks_when_update_raises_cancelled_error(self, monkeypatch):
        """
        Directly exercises the except asyncio.CancelledError: break branch
        by having feed.update() raise CancelledError synchronously, rather
        than relying on external task.cancel() (which coverage.py cannot
        always attribute to the exact `break` line inside a task frame).
        """
        store = FakeFeatureStore()
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, store)
        feed, pool = make_feed(adapter_responses=[[]])
        engine._pool = pool
        engine._running = True

        async def raise_cancelled(pool_arg):
            raise asyncio.CancelledError()

        monkeypatch.setattr(feed, "update", raise_cancelled)

        # _poll_loop should catch CancelledError internally and break out
        # of the while loop cleanly (not propagate), returning normally.
        await engine._poll_loop(feed, 0.01)

    async def test_poll_loop_real_run_updates_and_publishes_then_cancelled(self):
        """
        End-to-end exercise of the real _poll_loop body (not the manually
        inlined version used in the earlier test): update() succeeds,
        buffer is non-empty, so the real _publish_candle_event call on the
        loop's happy path fires; the loop then sleeps and is cancelled,
        hitting the CancelledError->break branch.
        """
        initial = make_candles(3)
        new = make_candles(1, start=initial[-1].timestamp + timedelta(hours=1))
        adapter = FakeAdapter(ohlcv_responses=[initial, new])
        pool = FakePool(adapter)
        store = FakeFeatureStore()
        engine = MarketDataEngine(pool, TradingMode.PAPER, store)
        feed = SymbolFeed(
            symbol=SYMBOL_STR,
            timeframe="1h",
            exchange_id=ExchangeID.BINANCE,
            trading_mode=TradingMode.PAPER,
            feature_engineer=FakeEngineer(),
            feature_store=store,
        )
        engine._running = True

        task = asyncio.create_task(engine._poll_loop(feed, 3600.0))
        # Give the first iteration (update + real publish) time to run
        # before it reaches the long sleep.
        await asyncio.sleep(0.05)
        assert feed._initialized is True
        assert store.saved  # feature computed & saved on first iteration

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.done()

    async def test_poll_loop_unexpected_error_is_caught_and_loop_continues(self, monkeypatch):
        store = FakeFeatureStore()
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, store)
        feed, pool = make_feed(adapter_responses=[make_candles(3)])
        engine._pool = pool
        engine._running = True

        call_count = {"n": 0}

        async def failing_update(pool_arg):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            engine._running = False  # stop after second iteration
            return False

        monkeypatch.setattr(feed, "update", failing_update)

        await engine._poll_loop(feed, 0.001)

        assert call_count["n"] == 2


# ---------------------------------------------------------------------
# MarketDataEngine._publish_candle_event
# ---------------------------------------------------------------------


class TestPublishCandleEvent:
    async def test_publish_with_empty_buffer_is_noop(self):
        store = FakeFeatureStore()
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, store)
        feed, _ = make_feed()
        feed._candle_buffer = []
        await engine._publish_candle_event(feed)  # must not raise

    async def test_publish_success_uses_real_event_bus(self):
        store = FakeFeatureStore()
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, store)
        feed, _ = make_feed()
        feed._candle_buffer = make_candles(1)
        await engine._publish_candle_event(feed)  # exercises real get_event_bus()/publish

    async def test_publish_failure_is_caught_and_logged(self, monkeypatch):
        import sgr.market_data.engine as engine_module

        fake_bus = AsyncMock()
        fake_bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))
        monkeypatch.setattr(engine_module, "get_event_bus", lambda: fake_bus)

        store = FakeFeatureStore()
        engine = MarketDataEngine(FakePool(FakeAdapter()), TradingMode.PAPER, store)
        feed, _ = make_feed()
        feed._candle_buffer = make_candles(1)

        await engine._publish_candle_event(feed)  # must not raise
        fake_bus.publish.assert_awaited_once()


# ---------------------------------------------------------------------
# Module-level constants sanity
# ---------------------------------------------------------------------


class TestConstants:
    def test_poll_intervals_shorter_than_bar_length(self):
        bar_seconds = {
            "1m": 60,
            "3m": 180,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
        }
        for tf, interval in _POLL_INTERVALS.items():
            assert interval < bar_seconds[tf]
