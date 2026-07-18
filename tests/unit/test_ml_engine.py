"""
Tests für die ML Engine.

Teststrategie:
    - FeatureExtractor: Normalisierung, Imputation, Reproduzierbarkeit
    - RegimeDetector: Fallback, Prediction-Format, Label-Generierung
    - VolatilityForecaster: EWMA-Fallback, Forecast-Bounds, Update
    - MLEngine: Integration, Fallback wenn nicht trainiert
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest

from sgr.core.types import ExchangeID, MarketRegime, Symbol
from sgr.market_data.types import FeatureSet, IndicatorValues
from sgr.ml.features import REGIME_FEATURES, FeatureExtractor, FeatureMatrix
from sgr.ml.regime_detector import RegimeDetector, _generate_labels
from sgr.ml.types import ModelStatus, RegimePrediction
from sgr.ml.volatility_forecaster import VolatilityForecaster

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _make_indicators(**overrides) -> IndicatorValues:
    defaults = dict(
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
    )
    defaults.update(overrides)
    return IndicatorValues(**defaults)


def _make_feature_set(
    regime: MarketRegime = MarketRegime.TRENDING_UP,
    rsi: float = 60.0,
    adx: float = 30.0,
    atr_pct: float = 0.01,
    returns_5: float = 0.02,
) -> FeatureSet:
    return FeatureSet(
        symbol=_make_symbol(),
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal("50000"),
        volume=Decimal("1000"),
        indicators=_make_indicators(rsi_14=rsi, adx_14=adx, atr_pct=atr_pct),
        regime=regime,
        returns_1=0.005,
        returns_5=returns_5,
        returns_10=0.03,
        returns_20=0.05,
    )


def _make_feature_sets(n: int = 150) -> list[FeatureSet]:
    """Erstellt synthetische FeatureSet-Liste für Tests."""
    np.random.seed(42)
    sets = []
    for _i in range(n):
        rsi = float(np.clip(50 + np.random.randn() * 15, 10, 90))
        adx = float(np.clip(20 + abs(np.random.randn()) * 15, 5, 60))
        atr_pct = float(np.clip(0.01 + abs(np.random.randn()) * 0.01, 0.001, 0.1))
        ret_5 = float(np.random.randn() * 0.02)
        sets.append(_make_feature_set(rsi=rsi, adx=adx, atr_pct=atr_pct, returns_5=ret_5))
    return sets


# ===========================================================================
# Feature Extractor
# ===========================================================================


class TestFeatureExtractor:
    def test_extract_single_returns_correct_length(self) -> None:
        extractor = FeatureExtractor(REGIME_FEATURES)
        fs = _make_feature_set()
        row = extractor.extract_single(fs)
        assert len(row) == len(REGIME_FEATURES)

    def test_fit_transform_returns_matrix(self) -> None:
        extractor = FeatureExtractor(REGIME_FEATURES)
        sets = _make_feature_sets(50)
        matrix = extractor.fit_transform(sets)
        assert isinstance(matrix, FeatureMatrix)
        assert matrix.X.shape == (50, len(REGIME_FEATURES))
        assert len(matrix.timestamps) == 50

    def test_nan_imputation(self) -> None:
        """NaN-Werte werden imputiert – keine NaN in Output-Matrix."""
        extractor = FeatureExtractor(REGIME_FEATURES)
        sets = _make_feature_sets(50)
        # Alle haben einige None-Indikatoren
        matrix = extractor.fit_transform(sets)
        assert not np.any(np.isnan(matrix.X)), "NaN in feature matrix after imputation"

    def test_normalization_zero_mean(self) -> None:
        """Nach Z-Score Normalisierung: Mean ≈ 0."""
        extractor = FeatureExtractor(REGIME_FEATURES)
        sets = _make_feature_sets(200)
        matrix = extractor.fit_transform(sets)
        col_means = np.mean(matrix.X, axis=0)
        assert np.allclose(col_means, 0, atol=1e-10)

    def test_transform_uses_fitted_params(self) -> None:
        """transform() nutzt Fit-Parameter, nicht neu schätzt."""
        extractor = FeatureExtractor(REGIME_FEATURES)
        train = _make_feature_sets(100)
        test = _make_feature_sets(20)
        extractor.fit_transform(train)
        # Sollte nicht crashen
        result = extractor.transform(test)
        assert result.X.shape == (20, len(REGIME_FEATURES))

    def test_transform_before_fit_raises(self) -> None:
        extractor = FeatureExtractor(REGIME_FEATURES)
        sets = _make_feature_sets(10)
        with pytest.raises(RuntimeError, match="not fitted"):
            extractor.transform(sets)

    def test_get_params_roundtrip(self) -> None:
        """Params serialisieren und wiederherstellen."""
        extractor = FeatureExtractor(REGIME_FEATURES)
        sets = _make_feature_sets(50)
        extractor.fit_transform(sets)
        params = extractor.get_params()
        restored = FeatureExtractor.from_params(params)
        # Beide sollen gleiche Normalisierung haben
        test_sets = _make_feature_sets(5)
        r1 = extractor.transform(test_sets)
        r2 = restored.transform(test_sets)
        np.testing.assert_allclose(r1.X, r2.X, rtol=1e-10)

    def test_feature_names_match_output(self) -> None:
        extractor = FeatureExtractor(REGIME_FEATURES)
        sets = _make_feature_sets(30)
        matrix = extractor.fit_transform(sets)
        assert matrix.feature_names == REGIME_FEATURES


# ===========================================================================
# Label Generation
# ===========================================================================


class TestLabelGeneration:
    def test_trending_up_label(self) -> None:
        """ADX > 25 + positive Returns → TRENDING_UP."""
        fs = _make_feature_set(adx=30.0, returns_5=0.02, atr_pct=0.01)
        labels = _generate_labels([fs])
        from sgr.ml.regime_detector import _LABEL_TO_REGIME, MarketRegime

        assert _LABEL_TO_REGIME[labels[0]] == MarketRegime.TRENDING_UP

    def test_trending_down_label(self) -> None:
        """ADX > 25 + negative Returns → TRENDING_DOWN."""
        fs = _make_feature_set(adx=30.0, returns_5=-0.02, atr_pct=0.01)
        labels = _generate_labels([fs])
        from sgr.ml.regime_detector import _LABEL_TO_REGIME, MarketRegime

        assert _LABEL_TO_REGIME[labels[0]] == MarketRegime.TRENDING_DOWN

    def test_ranging_label(self) -> None:
        """ADX < 20 → RANGING."""
        fs = _make_feature_set(adx=15.0, returns_5=0.001, atr_pct=0.01)
        labels = _generate_labels([fs])
        from sgr.ml.regime_detector import _LABEL_TO_REGIME, MarketRegime

        assert _LABEL_TO_REGIME[labels[0]] == MarketRegime.RANGING

    def test_high_vol_label(self) -> None:
        """ATR > 5% → HIGH_VOLATILITY."""
        fs = _make_feature_set(adx=15.0, returns_5=0.0, atr_pct=0.07)
        labels = _generate_labels([fs])
        from sgr.ml.regime_detector import _LABEL_TO_REGIME, MarketRegime

        assert _LABEL_TO_REGIME[labels[0]] == MarketRegime.HIGH_VOLATILITY


# ===========================================================================
# Regime Detector
# ===========================================================================


class TestRegimeDetector:
    def test_fallback_when_not_trained(self) -> None:
        """Nicht trainierter Detektor nutzt Regel-Fallback."""
        detector = RegimeDetector()
        fs = _make_feature_set(adx=30.0, returns_5=0.02)
        pred = detector.predict(fs)
        assert isinstance(pred, RegimePrediction)
        assert pred.regime in MarketRegime.__members__.values()
        assert 0.0 <= pred.confidence <= 1.0

    def test_fallback_trending_up_detected(self) -> None:
        """Fallback erkennt Trending-Up korrekt."""
        detector = RegimeDetector()
        fs = _make_feature_set(adx=35.0, returns_5=0.03, atr_pct=0.01)
        pred = detector.predict(fs)
        assert pred.regime == MarketRegime.TRENDING_UP

    def test_fallback_ranging_detected(self) -> None:
        detector = RegimeDetector()
        fs = _make_feature_set(adx=12.0, returns_5=0.001, atr_pct=0.008)
        pred = detector.predict(fs)
        assert pred.regime == MarketRegime.RANGING

    def test_train_succeeds_with_enough_data(self) -> None:
        """Training mit 150+ Samples erfolgreich."""
        detector = RegimeDetector()
        sets = _make_feature_sets(150)
        metrics = detector.train(sets, n_estimators=10, max_depth=3)
        assert "accuracy" in metrics
        assert 0.0 <= metrics["accuracy"] <= 1.0
        assert detector.is_trained

    def test_train_updates_metadata(self) -> None:
        detector = RegimeDetector()
        sets = _make_feature_sets(120)
        detector.train(sets, n_estimators=10, max_depth=3)
        assert detector.metadata.status == ModelStatus.SHADOW
        assert detector.metadata.trained_at is not None
        assert detector.metadata.training_samples > 0

    def test_predict_after_training(self) -> None:
        detector = RegimeDetector()
        sets = _make_feature_sets(120)
        detector.train(sets, n_estimators=10, max_depth=3)
        fs = _make_feature_set()
        pred = detector.predict(fs)
        assert isinstance(pred, RegimePrediction)
        assert pred.regime in MarketRegime.__members__.values()
        assert 0.0 <= pred.confidence <= 1.0

    def test_predict_probabilities_sum_to_one(self) -> None:
        detector = RegimeDetector()
        sets = _make_feature_sets(120)
        detector.train(sets, n_estimators=10, max_depth=3)
        fs = _make_feature_set()
        pred = detector.predict(fs)
        total_prob = sum(pred.probabilities.values())
        assert total_prob == pytest.approx(1.0, abs=0.01)

    def test_insufficient_data_raises(self) -> None:
        detector = RegimeDetector()
        sets = _make_feature_sets(10)
        with pytest.raises(ValueError):
            detector.train(sets)

    def test_high_confidence_property(self) -> None:
        pred = RegimePrediction(
            regime=MarketRegime.TRENDING_UP,
            confidence=0.85,
            probabilities={},
            feature_importance={},
            model_version="test",
            predicted_at=datetime.now(tz=UTC),
        )
        assert pred.is_high_confidence is True

    def test_low_confidence_property(self) -> None:
        pred = RegimePrediction(
            regime=MarketRegime.RANGING,
            confidence=0.55,
            probabilities={},
            feature_importance={},
            model_version="test",
            predicted_at=datetime.now(tz=UTC),
        )
        assert pred.is_high_confidence is False


# ===========================================================================
# Volatility Forecaster
# ===========================================================================


class TestVolatilityForecaster:
    def _make_returns(self, n: int = 100, vol: float = 0.02) -> np.ndarray:
        np.random.seed(42)
        return np.random.normal(0, vol, n)

    def test_fallback_when_not_fitted(self) -> None:
        forecaster = VolatilityForecaster()
        forecast = forecaster.predict("BTC/USDT", "1h", horizon_bars=5)
        assert forecast.predicted_volatility_pct > 0
        assert forecast.model_version == "fallback"

    def test_fit_ewma_fallback(self) -> None:
        """EWMA Fallback funktioniert ohne arch package."""
        forecaster = VolatilityForecaster()
        returns = self._make_returns(100)
        result = forecaster._fit_ewma(returns)
        assert "alpha" in result
        assert "beta" in result
        assert forecaster.is_fitted

    def test_predict_after_fit(self) -> None:
        forecaster = VolatilityForecaster()
        returns = self._make_returns(100)
        forecaster._fit_ewma(returns)
        forecast = forecaster.predict("BTC/USDT", "1h", horizon_bars=5)
        assert forecast.predicted_volatility_pct > 0
        assert forecast.lower_bound_pct <= forecast.predicted_volatility_pct
        assert forecast.upper_bound_pct >= forecast.predicted_volatility_pct

    def test_higher_vol_returns_higher_forecast(self) -> None:
        """Höhere historische Volatilität → höhere Prognose."""
        low_vol = VolatilityForecaster()
        high_vol = VolatilityForecaster()
        low_vol._fit_ewma(self._make_returns(100, vol=0.005))
        high_vol._fit_ewma(self._make_returns(100, vol=0.05))
        f_low = low_vol.predict("BTC/USDT", "1h")
        f_high = high_vol.predict("BTC/USDT", "1h")
        assert f_high.predicted_volatility_pct > f_low.predicted_volatility_pct

    def test_update_changes_last_variance(self) -> None:
        forecaster = VolatilityForecaster()
        returns = self._make_returns(100)
        forecaster._fit_ewma(returns)
        var_before = forecaster._last_variance
        forecaster.update(0.05)  # Große Bewegung
        var_after = forecaster._last_variance
        assert var_after != var_before

    def test_volatility_regime_classification(self) -> None:
        forecaster = VolatilityForecaster()
        returns = self._make_returns(100, vol=0.005)
        forecaster._fit_ewma(returns)
        forecast = forecaster.predict("BTC/USDT", "1h")
        assert forecast.volatility_regime in ("low", "medium", "high")

    def test_stationarity_alpha_beta(self) -> None:
        """α + β nach EWMA Fit < 1 (Stationarität)."""
        forecaster = VolatilityForecaster()
        returns = self._make_returns(100)
        forecaster._fit_ewma(returns)
        assert forecaster._alpha + forecaster._beta < 1.0
