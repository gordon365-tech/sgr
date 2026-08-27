"""
Tests for sgr.strategy.engine.StrategyEngine.

Strategy: use real Signal/MarketContext/FeatureSet Pydantic models (cheap to
construct), fake TradingStrategy plugins (simple objects with .name and
.generate_signal), and a fake FeatureStore / StrategyRegistry so we don't
depend on DB or the real strategy plugins registered globally.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from sgr.core.types import (
    ExchangeID,
    MarketRegime,
    Signal,
    SignalDirection,
    Symbol,
    TradingMode,
)
from sgr.market_data.types import FeatureSet, IndicatorValues, MarketContext
from sgr.strategy.engine import StrategyEngine

SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def make_feature_set(**overrides) -> FeatureSet:
    defaults = dict(
        symbol=SYMBOL,
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal("65000"),
        volume=Decimal("100"),
        indicators=IndicatorValues(),
    )
    defaults.update(overrides)
    return FeatureSet(**defaults)


def make_signal(
    direction: SignalDirection = SignalDirection.LONG,
    confidence: float = 0.7,
    strategy_name: str = "fake_strategy",
    regime: MarketRegime = MarketRegime.TRENDING_UP,
) -> Signal:
    return Signal(
        timestamp=datetime.now(tz=UTC),
        strategy_name=strategy_name,
        symbol=SYMBOL,
        direction=direction,
        confidence=confidence,
        regime=regime,
    )


class FakeStrategy:
    """Minimal TradingStrategy double: .name + .generate_signal(context)."""

    def __init__(self, name: str, signal: Signal | None = None, raises: Exception | None = None):
        self.name = name
        self._signal = signal
        self._raises = raises
        self.calls: list[MarketContext] = []

    def generate_signal(self, context: MarketContext) -> Signal | None:
        self.calls.append(context)
        if self._raises:
            raise self._raises
        return self._signal


class FakeFeatureStore:
    def __init__(self, features: FeatureSet | None):
        self._features = features
        self.calls: list[tuple[str, str]] = []

    async def get_latest(self, symbol_key: str, timeframe: str) -> FeatureSet | None:
        self.calls.append((symbol_key, timeframe))
        return self._features


class FakeRegistry:
    def __init__(self, active: list[FakeStrategy]):
        self._active = active
        self.calls: list[MarketRegime | None] = []

    def get_active(self, regime: MarketRegime | None = None) -> list[FakeStrategy]:
        self.calls.append(regime)
        return self._active


@pytest.fixture
def features():
    return make_feature_set()


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------


class TestLifecycle:
    async def test_start_sets_running_and_logs_active_count(self):
        registry = FakeRegistry([FakeStrategy("s1")])
        engine = StrategyEngine(TradingMode.PAPER, FakeFeatureStore(None), registry=registry)
        await engine.start()
        assert engine._running is True

    async def test_stop_without_tasks_is_noop(self):
        registry = FakeRegistry([])
        engine = StrategyEngine(TradingMode.PAPER, FakeFeatureStore(None), registry=registry)
        await engine.start()
        await engine.stop()
        assert engine._running is False

    async def test_stop_cancels_pending_tasks(self):
        import asyncio

        registry = FakeRegistry([])
        engine = StrategyEngine(TradingMode.PAPER, FakeFeatureStore(None), registry=registry)

        async def never_ending():
            await asyncio.sleep(1000)

        task = asyncio.create_task(never_ending())
        engine._tasks.append(task)
        await engine.stop()
        assert engine._running is False
        assert task.cancelled() or task.done()

    def test_default_registry_uses_singleton(self):
        engine = StrategyEngine(TradingMode.PAPER, FakeFeatureStore(None))
        from sgr.strategy.registry import StrategyRegistry

        assert engine._registry is StrategyRegistry.get()


# ---------------------------------------------------------------------
# process(): no features / no active strategies
# ---------------------------------------------------------------------


class TestProcessEarlyExits:
    async def test_process_returns_none_when_no_features(self):
        store = FakeFeatureStore(None)
        registry = FakeRegistry([FakeStrategy("s1")])
        engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

        result = await engine.process("binance:BTC/USDT", "1h")

        assert result is None
        assert store.calls == [("binance:BTC/USDT", "1h")]
        # Registry should never be consulted if there are no features
        assert registry.calls == []

    async def test_process_returns_none_when_no_active_strategies(self, features):
        store = FakeFeatureStore(features)
        registry = FakeRegistry([])
        engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

        result = await engine.process("binance:BTC/USDT", "1h", regime=MarketRegime.RANGING)

        assert result is None
        assert registry.calls == [MarketRegime.RANGING]

    async def test_process_returns_none_when_all_signals_below_threshold(self, features):
        low_conf_signal = make_signal(confidence=0.3)
        strat = FakeStrategy("weak", signal=low_conf_signal)
        store = FakeFeatureStore(features)
        registry = FakeRegistry([strat])
        engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

        result = await engine.process("binance:BTC/USDT", "1h")

        assert result is None

    async def test_process_returns_none_when_strategy_returns_none(self, features):
        strat = FakeStrategy("silent", signal=None)
        store = FakeFeatureStore(features)
        registry = FakeRegistry([strat])
        engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

        result = await engine.process("binance:BTC/USDT", "1h")

        assert result is None

    async def test_process_returns_none_when_aggregation_yields_conflict(self, features):
        """
        Two strategies produce valid, above-threshold signals but in
        opposing directions -> _aggregate() returns None -> process()
        must also return None (and never call _publish).
        """
        long_strat = FakeStrategy(
            "long_strat", signal=make_signal(direction=SignalDirection.LONG, confidence=0.9)
        )
        short_strat = FakeStrategy(
            "short_strat", signal=make_signal(direction=SignalDirection.SHORT, confidence=0.9)
        )
        store = FakeFeatureStore(features)
        registry = FakeRegistry([long_strat, short_strat])
        engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

        result = await engine.process("binance:BTC/USDT", "1h")

        assert result is None


# ---------------------------------------------------------------------
# process(): happy path + publishing
# ---------------------------------------------------------------------


class TestProcessHappyPath:
    async def test_process_returns_signal_and_publishes(self, features, monkeypatch):
        signal = make_signal(confidence=0.9)
        strat = FakeStrategy("s1", signal=signal)
        store = FakeFeatureStore(features)
        registry = FakeRegistry([strat])
        engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

        published = []

        async def fake_publish(sig):
            published.append(sig)

        monkeypatch.setattr(engine, "_publish", fake_publish)

        result = await engine.process("binance:BTC/USDT", "1h", regime=MarketRegime.TRENDING_UP)

        assert result is not None
        assert result.direction == SignalDirection.LONG
        assert published == [result]

    async def test_process_builds_context_with_regime_override(self, features):
        strat = FakeStrategy("s1", signal=None)
        store = FakeFeatureStore(features)
        registry = FakeRegistry([strat])
        engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

        await engine.process("binance:BTC/USDT", "1h", regime=MarketRegime.CRISIS)

        assert len(strat.calls) == 1
        ctx = strat.calls[0]
        assert ctx.regime == MarketRegime.CRISIS
        assert ctx.primary.regime == MarketRegime.CRISIS

    async def test_process_strategy_exception_is_caught_and_logged(self, features):
        failing = FakeStrategy("boom", raises=RuntimeError("strategy blew up"))
        ok_signal = make_signal(confidence=0.8, strategy_name="ok")
        ok = FakeStrategy("ok", signal=ok_signal)
        store = FakeFeatureStore(features)
        registry = FakeRegistry([failing, ok])
        engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

        result = await engine.process("binance:BTC/USDT", "1h")

        # Failing strategy must not crash process(); the healthy one still wins
        assert result is not None
        assert result.strategy_name == "ok"

    async def test_process_real_event_bus_publish_succeeds(self, features):
        """Exercise the real _publish path (not mocked) against the real event bus."""
        signal = make_signal(confidence=0.85)
        strat = FakeStrategy("s1", signal=signal)
        store = FakeFeatureStore(features)
        registry = FakeRegistry([strat])
        engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

        result = await engine.process("binance:BTC/USDT", "1h")

        assert result is not None


class TestPublish:
    async def test_publish_failure_is_caught_and_logged(self, monkeypatch):
        signal = make_signal()

        import sgr.strategy.engine as engine_module

        fake_bus = AsyncMock()
        fake_bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))
        monkeypatch.setattr(engine_module, "get_event_bus", lambda: fake_bus)

        registry = FakeRegistry([])
        engine = StrategyEngine(TradingMode.PAPER, FakeFeatureStore(None), registry=registry)

        await engine._publish(signal)  # must not raise
        fake_bus.publish.assert_awaited_once()


# ---------------------------------------------------------------------
# _aggregate()
# ---------------------------------------------------------------------


class TestAggregate:
    def test_aggregate_empty_list_returns_none(self):
        engine = StrategyEngine(
            TradingMode.PAPER, FakeFeatureStore(None), registry=FakeRegistry([])
        )
        assert engine._aggregate([]) is None

    def test_aggregate_single_signal_passthrough_no_boost(self):
        engine = StrategyEngine(
            TradingMode.PAPER, FakeFeatureStore(None), registry=FakeRegistry([])
        )
        sig = make_signal(confidence=0.7)
        result = engine._aggregate([sig])
        assert result is not None
        assert result.confidence == pytest.approx(0.7)

    def test_aggregate_conflicting_long_short_returns_none(self):
        engine = StrategyEngine(
            TradingMode.PAPER, FakeFeatureStore(None), registry=FakeRegistry([])
        )
        long_sig = make_signal(direction=SignalDirection.LONG, confidence=0.9)
        short_sig = make_signal(direction=SignalDirection.SHORT, confidence=0.9)
        assert engine._aggregate([long_sig, short_sig]) is None

    def test_aggregate_multiple_longs_picks_highest_confidence_with_boost(self):
        engine = StrategyEngine(
            TradingMode.PAPER, FakeFeatureStore(None), registry=FakeRegistry([])
        )
        weak = make_signal(confidence=0.6, strategy_name="weak")
        strong = make_signal(confidence=0.8, strategy_name="strong")
        result = engine._aggregate([weak, strong])
        assert result is not None
        assert result.strategy_name == "strong"
        # Boosted confidence: min(0.8*1.1 + avg(0.6,0.8)*0.1, 1.0)
        expected = min(0.8 * 1.1 + ((0.6 + 0.8) / 2) * 0.1, 1.0)
        assert result.confidence == pytest.approx(expected)

    def test_aggregate_boost_caps_at_one(self):
        engine = StrategyEngine(
            TradingMode.PAPER, FakeFeatureStore(None), registry=FakeRegistry([])
        )
        a = make_signal(confidence=0.95, strategy_name="a")
        b = make_signal(confidence=0.98, strategy_name="b")
        result = engine._aggregate([a, b])
        assert result is not None
        assert result.confidence == 1.0

    def test_aggregate_only_close_signals(self):
        engine = StrategyEngine(
            TradingMode.PAPER, FakeFeatureStore(None), registry=FakeRegistry([])
        )
        close_sig = make_signal(direction=SignalDirection.CLOSE, confidence=0.75)
        result = engine._aggregate([close_sig])
        assert result is not None
        assert result.direction == SignalDirection.CLOSE

    def test_aggregate_neutral_only_returns_none(self):
        """
        NEUTRAL direction signals never make it into longs/shorts/closes,
        so candidates stays empty and aggregate returns None even though
        the signals list itself was non-empty.
        """
        engine = StrategyEngine(
            TradingMode.PAPER, FakeFeatureStore(None), registry=FakeRegistry([])
        )
        neutral_sig = make_signal(direction=SignalDirection.NEUTRAL, confidence=0.9)
        assert engine._aggregate([neutral_sig]) is None

    def test_aggregate_multiple_shorts_picks_highest(self):
        engine = StrategyEngine(
            TradingMode.PAPER, FakeFeatureStore(None), registry=FakeRegistry([])
        )
        s1 = make_signal(direction=SignalDirection.SHORT, confidence=0.55, strategy_name="s1")
        s2 = make_signal(direction=SignalDirection.SHORT, confidence=0.65, strategy_name="s2")
        result = engine._aggregate([s1, s2])
        assert result is not None
        assert result.strategy_name == "s2"
        assert result.direction == SignalDirection.SHORT
