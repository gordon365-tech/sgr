"""
Tests für sgr.ml.engine.MLEngine.

Teststrategie:
    - initialize(): mit/ohne model_path, mit/ohne training_features,
      Fallback wenn kein Training möglich, Fehlerpfad beim Laden.
    - run(): vollständige Inference-Pipeline, mit und ohne trainierten
      StrategySelector, Registry-Update-Verzweigungen (aktivieren/
      deaktivieren, nicht validiert, unbekannte Strategie).
    - _log_prediction(): Rolling-Window-Verhalten (max 10.000 Einträge).
    - get_prediction_accuracy(): mit/ohne gelabelte Einträge.
    - is_initialized Property.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sgr.core.types import ExchangeID, MarketRegime, Symbol
from sgr.market_data.types import FeatureSet, IndicatorValues
from sgr.ml.engine import MLEngine
from sgr.ml.types import RegimePrediction, StrategyScore, VolatilityForecast
from sgr.strategy.mean_reversion import MeanReversionStrategy
from sgr.strategy.registry import StrategyRegistry
from sgr.strategy.trend_following import TrendFollowingStrategy

# ---------------------------------------------------------------------------
# Fixtures / Factories
# ---------------------------------------------------------------------------


def _make_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _make_feature_set() -> FeatureSet:
    return FeatureSet(
        symbol=_make_symbol(),
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal("50000"),
        volume=Decimal("1000"),
        indicators=IndicatorValues(
            rsi_14=55.0,
            rsi_7=58.0,
            macd_line=100.0,
            macd_signal=90.0,
            macd_histogram=10.0,
            adx_14=30.0,
            di_plus=28.0,
            di_minus=15.0,
            atr_14=Decimal("500"),
            atr_pct=0.01,
            bb_width=0.04,
            bb_position=0.5,
            volume_ratio=1.2,
            obv=0.05,
        ),
        returns_1=0.005,
        returns_5=0.02,
        returns_10=0.03,
        returns_20=0.05,
    )


def _make_regime_prediction(
    confidence: float = 0.9,
    regime: MarketRegime = MarketRegime.TRENDING_UP,
) -> RegimePrediction:
    return RegimePrediction(
        regime=regime,
        confidence=confidence,
        probabilities={regime.value: confidence},
        feature_importance={"rsi_14": 0.5, "adx_14": 0.3},
        model_version="test-v1",
        predicted_at=datetime.now(tz=UTC),
    )


def _make_vol_forecast() -> VolatilityForecast:
    return VolatilityForecast(
        symbol="BTC/USDT",
        timeframe="1h",
        horizon_bars=5,
        predicted_volatility_pct=2.5,
        lower_bound_pct=1.5,
        upper_bound_pct=3.5,
        garch_alpha=0.1,
        garch_beta=0.8,
        model_version="test-v1",
        predicted_at=datetime.now(tz=UTC),
    )


def _make_strategy_score(
    name: str = "trend_following_v1",
    recommended: bool = True,
    score: float = 0.8,
) -> StrategyScore:
    return StrategyScore(
        strategy_name=name,
        score=score,
        expected_sharpe=1.2,
        regime_fit=0.75,
        feature_alignment={"rsi_14": 0.4},
        recommended=recommended,
    )


@pytest.fixture(autouse=True)
def reset_registry() -> Iterator[None]:
    """
    Jeder Test bekommt eine frische, isolierte Registry.

    Wichtig: Die globale StrategyRegistry wird sonst nur einmalig beim
    Modul-Import von trend_following.py/mean_reversion.py via
    @StrategyRegistry.register befuellt. Ein clear() ohne anschliessende
    Wiederherstellung wuerde die Registry fuer alle danach laufenden
    Testmodule (z.B. test_strategy_selector.py) dauerhaft leeren, da die
    Decorator-Registrierung nicht erneut ausgefuehrt wird. Deshalb nach
    jedem Test die Standard-Strategien wiederherstellen.
    """
    StrategyRegistry.get().clear()
    yield
    StrategyRegistry.get().clear()
    StrategyRegistry.get().register_instance(TrendFollowingStrategy())
    StrategyRegistry.get().register_instance(MeanReversionStrategy())


@pytest.fixture
def registry() -> StrategyRegistry:
    reg = StrategyRegistry.get()
    reg.register_instance(TrendFollowingStrategy())
    reg.register_instance(MeanReversionStrategy())
    return reg


# ---------------------------------------------------------------------------
# __init__ / is_initialized
# ---------------------------------------------------------------------------


class TestInit:
    def test_default_construction_not_initialized(self, registry: StrategyRegistry) -> None:
        engine = MLEngine(registry=registry)
        assert engine.is_initialized is False

    def test_uses_provided_registry(self, registry: StrategyRegistry) -> None:
        engine = MLEngine(registry=registry)
        assert engine._registry is registry

    def test_defaults_to_global_registry(self) -> None:
        engine = MLEngine()
        assert engine._registry is StrategyRegistry.get()


# ---------------------------------------------------------------------------
# initialize()
# ---------------------------------------------------------------------------


class TestInitialize:
    async def test_initialize_without_training_data_uses_fallback(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()

        assert engine.is_initialized is True
        assert engine._regime_detector.is_trained is False
        assert engine._strategy_selector is not None

    async def test_initialize_with_model_path_load_failure_falls_back(
        self, registry: StrategyRegistry, tmp_path: Path
    ) -> None:
        """Ungültiger model_path -> load() wirft -> Warnung, trotzdem initialisiert."""
        engine = MLEngine(registry=registry)
        await engine.initialize(model_path=str(tmp_path / "does_not_exist"))

        assert engine.is_initialized is True
        assert engine._regime_detector.is_trained is False

    async def test_initialize_with_training_features_trains_models(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        engine._regime_detector.train = MagicMock(return_value={"accuracy": 0.8})  # type: ignore[method-assign]
        engine._vol_forecaster.fit = MagicMock(return_value={"alpha": 0.1})  # type: ignore[method-assign]
        # is_trained bleibt False (Mock trainiert nicht wirklich) -> Zweig wird trotzdem betreten
        features = [_make_feature_set() for _ in range(40)]

        await engine.initialize(training_features=features)

        engine._regime_detector.train.assert_called_once()
        engine._vol_forecaster.fit.assert_called_once()
        assert engine.is_initialized is True

    async def test_train_all_handles_regime_detector_exception(
        self, registry: StrategyRegistry
    ) -> None:
        """Fehler beim Regime-Detector-Training darf Initialisierung nicht abbrechen."""
        engine = MLEngine(registry=registry)
        engine._regime_detector.train = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        features = [_make_feature_set() for _ in range(40)]

        await engine.initialize(training_features=features)

        assert engine.is_initialized is True

    async def test_train_all_handles_vol_forecaster_exception(
        self, registry: StrategyRegistry
    ) -> None:
        """Fehler beim Vol-Forecaster-Training darf Initialisierung nicht abbrechen."""
        engine = MLEngine(registry=registry)
        engine._vol_forecaster.fit = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        features = [_make_feature_set() for _ in range(40)]

        await engine.initialize(training_features=features)

        assert engine.is_initialized is True

    async def test_train_all_skips_vol_forecaster_with_too_few_samples(
        self, registry: StrategyRegistry
    ) -> None:
        """<= 30 Samples -> vol_forecaster.fit wird nicht aufgerufen."""
        engine = MLEngine(registry=registry)
        engine._regime_detector.train = MagicMock(return_value={"accuracy": 0.8})  # type: ignore[method-assign]
        engine._vol_forecaster.fit = MagicMock()  # type: ignore[method-assign]
        features = [_make_feature_set() for _ in range(10)]

        await engine.initialize(training_features=features)

        engine._vol_forecaster.fit.assert_not_called()

    async def test_initialize_loads_model_successfully(
        self, registry: StrategyRegistry, tmp_path: Path
    ) -> None:
        engine = MLEngine(registry=registry)
        engine._regime_detector.load = MagicMock()  # type: ignore[method-assign]
        (tmp_path / "regime").mkdir()

        await engine.initialize(model_path=str(tmp_path))

        engine._regime_detector.load.assert_called_once()
        assert engine.is_initialized is True


# ---------------------------------------------------------------------------
# run() - Inference Pipeline
# ---------------------------------------------------------------------------


class TestRun:
    async def test_run_without_fitted_selector_returns_empty_strategy_scores(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()

        result = await engine.run(_make_feature_set())

        assert result["strategy_scores"] == []
        assert "regime" in result
        assert "volatility_pct" in result

    async def test_run_returns_expected_result_shape(self, registry: StrategyRegistry) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()

        result = await engine.run(_make_feature_set(), symbol="BTC/USDT", timeframe="4h")

        assert set(result.keys()) == {
            "regime",
            "regime_confidence",
            "regime_probabilities",
            "top_regime_features",
            "volatility_pct",
            "volatility_regime",
            "volatility_ci",
            "strategy_scores",
        }
        assert result["volatility_ci"].keys() == {"lower", "upper"}

    async def test_run_uses_symbol_override_when_provided(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        engine._vol_forecaster.predict = MagicMock(return_value=_make_vol_forecast())  # type: ignore[method-assign]

        await engine.run(_make_feature_set(), symbol="ETH/USDT")

        _, kwargs = engine._vol_forecaster.predict.call_args
        assert kwargs["symbol"] == "ETH/USDT"

    async def test_run_falls_back_to_features_symbol_when_none_provided(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        engine._vol_forecaster.predict = MagicMock(return_value=_make_vol_forecast())  # type: ignore[method-assign]
        fs = _make_feature_set()

        await engine.run(fs, symbol=None)

        _, kwargs = engine._vol_forecaster.predict.call_args
        assert kwargs["symbol"] == str(fs.symbol)

    async def test_run_calls_strategy_selector_when_fitted(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        mock_selector = MagicMock()
        mock_selector.is_fitted = True
        mock_selector.select = MagicMock(
            return_value=[_make_strategy_score(recommended=False)]
        )
        engine._strategy_selector = mock_selector
        engine._regime_detector.predict = MagicMock(  # type: ignore[method-assign]
            return_value=_make_regime_prediction(confidence=0.3)
        )

        result = await engine.run(_make_feature_set())

        mock_selector.select.assert_called_once()
        assert len(result["strategy_scores"]) == 1
        assert result["strategy_scores"][0]["strategy"] == "trend_following_v1"

    async def test_run_limits_strategy_scores_to_top_three(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        mock_selector = MagicMock()
        mock_selector.is_fitted = True
        scores = [_make_strategy_score(name=f"s{i}") for i in range(5)]
        mock_selector.select = MagicMock(return_value=scores)
        engine._strategy_selector = mock_selector
        engine._regime_detector.predict = MagicMock(  # type: ignore[method-assign]
            return_value=_make_regime_prediction(confidence=0.3)
        )

        result = await engine.run(_make_feature_set())

        assert len(result["strategy_scores"]) == 3

    async def test_run_limits_top_regime_features_to_five(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        many_features = {f"f{i}": float(i) for i in range(10)}
        pred = replace(_make_regime_prediction(), feature_importance=many_features)
        engine._regime_detector.predict = MagicMock(return_value=pred)  # type: ignore[method-assign]

        result = await engine.run(_make_feature_set())

        assert len(result["top_regime_features"]) <= 5

    async def test_run_appends_to_prediction_log(self, registry: StrategyRegistry) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()

        await engine.run(_make_feature_set())

        assert len(engine._prediction_log) == 1
        entry = engine._prediction_log[0]
        assert entry["actual_regime"] is None
        assert entry["actual_vol_pct"] is None


# ---------------------------------------------------------------------------
# _update_registry() - Verzweigungen
# ---------------------------------------------------------------------------


class TestUpdateRegistry:
    async def test_low_confidence_skips_registry_update(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        pred = _make_regime_prediction(confidence=0.2)

        await engine._update_registry(pred, [_make_strategy_score(recommended=True)])

        assert not registry.is_active("trend_following_v1")

    async def test_recommended_and_validated_gets_activated(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        entry = registry.get_entry("trend_following_v1")
        assert entry is not None
        entry.is_validated = True
        pred = _make_regime_prediction(confidence=0.9)

        await engine._update_registry(
            pred, [_make_strategy_score(name="trend_following_v1", recommended=True)]
        )

        assert registry.is_active("trend_following_v1")

    async def test_recommended_but_not_validated_stays_inactive(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        pred = _make_regime_prediction(confidence=0.9)

        await engine._update_registry(
            pred, [_make_strategy_score(name="trend_following_v1", recommended=True)]
        )

        assert not registry.is_active("trend_following_v1")

    async def test_not_recommended_deactivates_active_strategy(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        await registry.activate("trend_following_v1")
        pred = _make_regime_prediction(confidence=0.9)

        await engine._update_registry(
            pred,
            [_make_strategy_score(name="trend_following_v1", recommended=False, score=0.1)],
        )

        assert not registry.is_active("trend_following_v1")

    async def test_unknown_strategy_name_is_skipped(self, registry: StrategyRegistry) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        pred = _make_regime_prediction(confidence=0.9)

        # Sollte keine Exception werfen, obwohl "ghost_strategy" nicht registriert ist.
        await engine._update_registry(
            pred, [_make_strategy_score(name="ghost_strategy", recommended=True)]
        )

    async def test_recommended_and_already_active_is_noop(
        self, registry: StrategyRegistry
    ) -> None:
        engine = MLEngine(registry=registry)
        await engine.initialize()
        entry = registry.get_entry("trend_following_v1")
        assert entry is not None
        entry.is_validated = True
        await registry.activate("trend_following_v1")
        pred = _make_regime_prediction(confidence=0.9)

        await engine._update_registry(
            pred, [_make_strategy_score(name="trend_following_v1", recommended=True)]
        )

        assert registry.is_active("trend_following_v1")


# ---------------------------------------------------------------------------
# _log_prediction() - Rolling Window
# ---------------------------------------------------------------------------


class TestLogPrediction:
    def test_log_prediction_appends_entry(self, registry: StrategyRegistry) -> None:
        engine = MLEngine(registry=registry)
        fs = _make_feature_set()
        pred = _make_regime_prediction()
        vol = _make_vol_forecast()

        engine._log_prediction(fs, pred, vol)

        assert len(engine._prediction_log) == 1
        assert engine._prediction_log[0]["predicted_regime"] == pred.regime.value

    def test_log_prediction_enforces_rolling_window(self, registry: StrategyRegistry) -> None:
        engine = MLEngine(registry=registry)
        fs = _make_feature_set()
        pred = _make_regime_prediction()
        vol = _make_vol_forecast()

        # Fülle über die 10_000-Grenze hinaus (klein gehalten via direkter Manipulation).
        engine._prediction_log = [{"actual_regime": None} for _ in range(10_000)]
        engine._log_prediction(fs, pred, vol)

        assert len(engine._prediction_log) == 10_000


# ---------------------------------------------------------------------------
# get_prediction_accuracy()
# ---------------------------------------------------------------------------


class TestGetPredictionAccuracy:
    def test_no_labeled_entries_returns_zero(self, registry: StrategyRegistry) -> None:
        engine = MLEngine(registry=registry)

        result = engine.get_prediction_accuracy()

        assert result == {"regime_accuracy": 0.0, "sample_size": 0}

    def test_computes_accuracy_from_labeled_entries(self, registry: StrategyRegistry) -> None:
        engine = MLEngine(registry=registry)
        engine._prediction_log = [
            {"predicted_regime": "trending_up", "actual_regime": "trending_up"},
            {"predicted_regime": "trending_up", "actual_regime": "ranging"},
            {"predicted_regime": "ranging", "actual_regime": None},
        ]

        result = engine.get_prediction_accuracy()

        assert result["sample_size"] == 2
        assert result["regime_accuracy"] == pytest.approx(0.5)
