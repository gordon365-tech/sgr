"""
Tests fuer sgr.strategy.engine.StrategyEngine.process().

Root-Cause-Fund (Autonomous-Paper-Trading-Rollout): kein Aufrufer im
Produktionscode hat regime jemals mit einem echten Wert belegt -
Orchestrator.on_candle_event() rief run_cycle(symbol_key, timeframe)
ohne regime-Argument auf, das dadurch immer auf UNKNOWN blieb.
get_active(regime=UNKNOWN) filtert dadurch JEDE registrierte Strategie
heraus (UNKNOWN ist in keiner supported_regimes-Liste enthalten) -
StrategyEngine konnte strukturell NIE ein Signal erzeugen. Diese Tests
decken die Behebung ab: automatische Regime-Klassifikation aus den
bereits geladenen Indikatoren, wenn der Aufrufer UNKNOWN (den Default)
uebergibt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sgr.core.types import ExchangeID, MarketRegime, SignalDirection, TradingMode
from sgr.market_data.types import FeatureSet, IndicatorValues, Symbol
from sgr.strategy.engine import StrategyEngine
from sgr.strategy.registry import StrategyRegistry
from sgr.strategy.symbol_gate import SymbolStrategyGate


def _symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _trending_indicators(**overrides) -> IndicatorValues:
    defaults = dict(
        rsi_14=65.0,
        adx_14=30.0,
        di_plus=28.0,
        di_minus=12.0,
        atr_14=Decimal("500"),
        atr_pct=0.01,
        bb_upper=Decimal("52000"),
        bb_middle=Decimal("50000"),
        bb_lower=Decimal("48000"),
        bb_width=0.04,
        bb_position=0.6,
        ema_9=Decimal("50500"),
        ema_21=Decimal("50200"),
        ema_50=Decimal("49500"),
        vwap=Decimal("49800"),
        volume_ratio=1.2,
        macd_histogram=10.0,
    )
    defaults.update(overrides)
    return IndicatorValues(**defaults)


def _feature_set(
    indicators: IndicatorValues,
    regime: MarketRegime = MarketRegime.UNKNOWN,
    symbol: Symbol | None = None,
) -> FeatureSet:
    return FeatureSet(
        symbol=symbol or _symbol(),
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal("50000"),
        volume=Decimal("1000"),
        indicators=indicators,
        regime=regime,
        returns_1=0.005,
        returns_5=0.02,
        returns_10=0.015,
    )


def _fake_feature_store(feature_set: FeatureSet | None) -> MagicMock:
    store = MagicMock()
    store.get_latest = AsyncMock(return_value=feature_set)
    return store


def _fresh_registry() -> StrategyRegistry:
    registry = StrategyRegistry.get()
    registry.clear()
    return registry


class FakeTrendStrategy:
    name = "fake_trend"
    version = "1.0.0"
    supported_regimes = [MarketRegime.TRENDING_UP]

    def generate_signal(self, context):
        return None

    def get_parameters(self):
        from sgr.strategy.base import StrategyParameters

        return StrategyParameters(name=self.name, version=self.version, params={})

    def validate_context(self, context) -> bool:
        return True


class SignalingStrategy(FakeTrendStrategy):
    name = "fake_signaling"

    def __init__(self, direction: SignalDirection, confidence: float = 0.8) -> None:
        self._direction = direction
        self._confidence = confidence

    def generate_signal(self, context):
        from sgr.strategy.base import Signal

        return Signal(
            timestamp=datetime.now(tz=UTC),
            strategy_name=self.name,
            symbol=context.symbol,
            direction=self._direction,
            confidence=self._confidence,
            regime=context.regime,
        )


class TestRegimeAutoClassification:
    async def test_unknown_regime_is_auto_classified_from_indicators(self, monkeypatch) -> None:
        registry = _fresh_registry()
        try:
            strategy = SignalingStrategy(SignalDirection.LONG)
            registry.register_instance(strategy)
            await registry.activate(strategy.name)

            fs = _feature_set(_trending_indicators())  # ADX=30, DI+>DI- -> TRENDING_UP
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_signal_generated", MagicMock())

            signal = await engine.process("binance:BTC/USDT", "1h")  # regime default = UNKNOWN

            assert signal is not None
            assert signal.direction == SignalDirection.LONG
        finally:
            registry.clear()

    async def test_explicit_non_unknown_regime_is_not_overridden(self, monkeypatch) -> None:
        """Ein vom Aufrufer explizit gesetztes Regime (z.B. in Tests oder
        einem manuellen Trigger) darf nicht durch die automatische
        Klassifikation ueberschrieben werden."""
        registry = _fresh_registry()
        try:
            strategy = SignalingStrategy(SignalDirection.SHORT)
            strategy.supported_regimes = [MarketRegime.RANGING]
            registry.register_instance(strategy)
            await registry.activate(strategy.name)

            # Indikatoren würden klar TRENDING_UP klassifizieren...
            fs = _feature_set(_trending_indicators())
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_signal_generated", MagicMock())

            # ...aber der Aufrufer gibt explizit RANGING vor.
            signal = await engine.process("binance:BTC/USDT", "1h", regime=MarketRegime.RANGING)

            assert signal is not None
            assert signal.direction == SignalDirection.SHORT
        finally:
            registry.clear()

    async def test_no_features_returns_none_without_classifying(self, monkeypatch) -> None:
        registry = _fresh_registry()
        try:
            store = _fake_feature_store(None)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)

            signal = await engine.process("binance:BTC/USDT", "1h")

            assert signal is None
        finally:
            registry.clear()

    async def test_regime_with_no_matching_strategy_yields_no_signal(self, monkeypatch) -> None:
        """LOW_VOLATILITY hat aktuell keine registrierte Strategie -
        get_active() liefert korrekt eine leere Liste, kein Signal."""
        registry = _fresh_registry()
        try:
            strategy = FakeTrendStrategy()
            registry.register_instance(strategy)
            await registry.activate(strategy.name)

            low_vol_indicators = _trending_indicators(adx_14=10.0, atr_pct=0.005)
            fs = _feature_set(low_vol_indicators)
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())

            signal = await engine.process("binance:BTC/USDT", "1h")

            assert signal is None
        finally:
            registry.clear()


class TestSignalMetricsWiring:
    async def test_generated_signal_is_recorded(self, monkeypatch) -> None:
        registry = _fresh_registry()
        try:
            strategy = SignalingStrategy(SignalDirection.LONG)
            registry.register_instance(strategy)
            await registry.activate(strategy.name)

            fs = _feature_set(_trending_indicators())
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())
            record_signal_mock = MagicMock()
            monkeypatch.setattr("sgr.strategy.engine.record_signal_generated", record_signal_mock)

            await engine.process("binance:BTC/USDT", "1h")

            record_signal_mock.assert_called_once_with(strategy.name, "long", 0.8)
        finally:
            registry.clear()

    async def test_conflicting_signals_recorded_as_rejected(self, monkeypatch) -> None:
        registry = _fresh_registry()
        try:
            long_strategy = SignalingStrategy(SignalDirection.LONG)
            long_strategy.name = "fake_long"
            short_strategy = SignalingStrategy(SignalDirection.SHORT)
            short_strategy.name = "fake_short"
            registry.register_instance(long_strategy)
            registry.register_instance(short_strategy)
            await registry.activate(long_strategy.name)
            await registry.activate(short_strategy.name)

            fs = _feature_set(_trending_indicators())
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_signal_generated", MagicMock())
            record_rejected_mock = MagicMock()
            monkeypatch.setattr("sgr.strategy.engine.record_signal_rejected", record_rejected_mock)

            signal = await engine.process("binance:BTC/USDT", "1h")

            assert signal is None
            record_rejected_mock.assert_called_once_with("binance:BTC/USDT", "conflicting_signals")
        finally:
            registry.clear()

    async def test_evaluation_is_recorded_even_without_active_strategies(self, monkeypatch) -> None:
        registry = _fresh_registry()
        try:
            fs = _feature_set(_trending_indicators())
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            eval_mock = MagicMock()
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", eval_mock)

            await engine.process("binance:BTC/USDT", "1h")

            eval_mock.assert_called_once_with("binance:BTC/USDT")
        finally:
            registry.clear()


class TestSymbolGateIntegration:
    """Autonomous-Strategy-Universe-Rollout, Phase 12 Production
    Integration: sgr/strategy/symbol_gate.py als additiver Zusatzfilter
    in process(). Siehe dortigen Docstring fuer das Fallback-Prinzip."""

    async def test_symbol_with_no_batch_result_is_unaffected(self, monkeypatch) -> None:
        registry = _fresh_registry()
        gate = SymbolStrategyGate.get()
        gate._active_strategy_by_symbol.clear()
        try:
            strategy = SignalingStrategy(SignalDirection.LONG)
            registry.register_instance(strategy)
            await registry.activate(strategy.name)

            fs = _feature_set(_trending_indicators())
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_signal_generated", MagicMock())
            monkeypatch.setattr(gate, "refresh_if_stale", AsyncMock())

            signal = await engine.process("binance:BTC/USDT", "1h")

            assert signal is not None
        finally:
            registry.clear()

    async def test_strategy_not_marked_active_for_symbol_is_blocked(self, monkeypatch) -> None:
        registry = _fresh_registry()
        gate = SymbolStrategyGate.get()
        gate._active_strategy_by_symbol.clear()
        try:
            strategy = SignalingStrategy(SignalDirection.LONG)
            registry.register_instance(strategy)
            await registry.activate(strategy.name)

            # BTC/USDT wurde batch-validiert, aber eine ANDERE Strategie
            # gewann - fake_signaling darf fuer dieses Symbol kein
            # Signal mehr erzeugen, obwohl global aktiv + regime-passend.
            gate._active_strategy_by_symbol["BTC/USDT"] = "some_other_strategy"

            fs = _feature_set(_trending_indicators())
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())
            monkeypatch.setattr(gate, "refresh_if_stale", AsyncMock())

            signal = await engine.process("binance:BTC/USDT", "1h")

            assert signal is None
        finally:
            registry.clear()
            gate._active_strategy_by_symbol.clear()

    async def test_strategy_marked_active_for_symbol_is_allowed(self, monkeypatch) -> None:
        registry = _fresh_registry()
        gate = SymbolStrategyGate.get()
        gate._active_strategy_by_symbol.clear()
        try:
            strategy = SignalingStrategy(SignalDirection.LONG)
            registry.register_instance(strategy)
            await registry.activate(strategy.name)

            gate._active_strategy_by_symbol["BTC/USDT"] = strategy.name

            fs = _feature_set(_trending_indicators())
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_signal_generated", MagicMock())
            monkeypatch.setattr(gate, "refresh_if_stale", AsyncMock())

            signal = await engine.process("binance:BTC/USDT", "1h")

            assert signal is not None
        finally:
            registry.clear()
            gate._active_strategy_by_symbol.clear()

    async def test_paper_test_disable_symbol_gate_bypasses_for_allowlisted_symbol(
        self, monkeypatch
    ) -> None:
        """SGRConfig.paper_test_disable_symbol_gate=True muss das Gate fuer
        ein Symbol aus _PAPER_TEST_SYMBOL_GATE_ALLOWLIST (z.B. BTC/USDT)
        umgehen, obwohl eine andere Strategie als "best" markiert ist."""
        registry = _fresh_registry()
        gate = SymbolStrategyGate.get()
        gate._active_strategy_by_symbol.clear()
        try:
            strategy = SignalingStrategy(SignalDirection.LONG)
            registry.register_instance(strategy)
            await registry.activate(strategy.name)
            gate._active_strategy_by_symbol["BTC/USDT"] = "some_other_strategy"

            fs = _feature_set(_trending_indicators())
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_signal_generated", MagicMock())
            monkeypatch.setattr(gate, "refresh_if_stale", AsyncMock())
            monkeypatch.setattr(
                "sgr.strategy.engine.get_config",
                lambda: SimpleNamespace(paper_test_disable_symbol_gate=True),
            )

            signal = await engine.process("binance:BTC/USDT", "1h")

            assert signal is not None
        finally:
            registry.clear()
            gate._active_strategy_by_symbol.clear()

    async def test_paper_test_disable_symbol_gate_does_not_bypass_other_symbols(
        self, monkeypatch
    ) -> None:
        """Die Scope-Einschraenkung (2026-09-26, Nutzer-Entscheidung) muss
        greifen: ein Symbol ausserhalb von _PAPER_TEST_SYMBOL_GATE_ALLOWLIST
        (z.B. USDC/USDT) bleibt blockiert, selbst mit dem Flag aktiv."""
        registry = _fresh_registry()
        gate = SymbolStrategyGate.get()
        gate._active_strategy_by_symbol.clear()
        try:
            strategy = SignalingStrategy(SignalDirection.LONG)
            registry.register_instance(strategy)
            await registry.activate(strategy.name)
            gate._active_strategy_by_symbol["USDC/USDT"] = "some_other_strategy"

            usdc_symbol = Symbol(base="USDC", quote="USDT", exchange=ExchangeID.BINANCE)
            fs = _feature_set(_trending_indicators(), symbol=usdc_symbol)
            store = _fake_feature_store(fs)
            engine = StrategyEngine(TradingMode.PAPER, store, registry=registry)
            monkeypatch.setattr("sgr.strategy.engine.record_market_regime", MagicMock())
            monkeypatch.setattr("sgr.strategy.engine.record_strategy_evaluation", MagicMock())
            monkeypatch.setattr(gate, "refresh_if_stale", AsyncMock())
            monkeypatch.setattr(
                "sgr.strategy.engine.get_config",
                lambda: SimpleNamespace(paper_test_disable_symbol_gate=True),
            )

            signal = await engine.process("binance:USDC/USDT", "1h")

            assert signal is None
        finally:
            registry.clear()
            gate._active_strategy_by_symbol.clear()
