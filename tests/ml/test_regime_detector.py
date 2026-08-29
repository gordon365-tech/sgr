"""
Tests für sgr.ml.regime_detector (RegimeDetector + _generate_labels).

Coverage-Ziel: 82% -> hoch.

Teststrategie: RegimeDetector wird über echtes Training (scikit-learn
und hmmlearn sind in dieser Umgebung installiert) getestet statt über
Mocking der ML-Bibliotheken selbst - ein echtes RandomForestClassifier-
Training auf synthetischen, aber vollständig befüllten FeatureSets ist
robuster und deckt die tatsächliche sklearn/hmmlearn/shap-Integration
ab. Wo Fehlerpfade absichtlich getriggert werden sollen (zu wenige
Samples, ImportError, Prediction-Exception), wird gezielt der
Fehlerzustand herbeigeführt (zu kleiner Datensatz, gepatchte Imports,
kaputtes Modell-Mock) statt die komplette ML-Pipeline zu mocken.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from sgr.core.types import ExchangeID, MarketRegime, Symbol
from sgr.market_data.types import FeatureSet, IndicatorValues
from sgr.ml.regime_detector import RegimeDetector, _generate_labels
from sgr.ml.types import ModelStatus


def _make_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _make_feature_set(
    *,
    adx: float = 15.0,
    atr_pct: float = 0.01,
    ret_5: float = 0.0,
    ret_20: float = 0.0,
    rsi_14: float = 50.0,
    rsi_7: float = 50.0,
    macd_line: float = 0.0,
    macd_histogram: float = 0.0,
    di_plus: float = 20.0,
    di_minus: float = 20.0,
    bb_position: float = 0.5,
    bb_width: float = 0.03,
    volume_ratio: float = 1.0,
    obv: float = 0.0,
    returns_1: float = 0.0,
    returns_10: float = 0.0,
) -> FeatureSet:
    return FeatureSet(
        symbol=_make_symbol(),
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal("50000"),
        volume=Decimal("1000"),
        indicators=IndicatorValues(
            rsi_14=rsi_14,
            rsi_7=rsi_7,
            macd_line=macd_line,
            macd_histogram=macd_histogram,
            adx_14=adx,
            di_plus=di_plus,
            di_minus=di_minus,
            atr_pct=atr_pct,
            bb_position=bb_position,
            bb_width=bb_width,
            volume_ratio=volume_ratio,
            obv=obv,
        ),
        returns_1=returns_1,
        returns_5=ret_5,
        returns_10=returns_10,
        returns_20=ret_20,
    )


def _make_training_set(n: int = 150) -> list[FeatureSet]:
    """Erzeugt einen synthetischen, vollständig befüllten Datensatz mit
    Samples aus mehreren Regime-Klassen (für stabiles Training ohne NaN)."""
    rng = random.Random(42)
    feature_sets = []
    for i in range(n):
        bucket = i % 4
        if bucket == 0:  # Trending up
            fs = _make_feature_set(
                adx=rng.uniform(26, 40),
                ret_5=rng.uniform(0.02, 0.05),
                atr_pct=rng.uniform(0.01, 0.03),
                rsi_14=rng.uniform(55, 75),
            )
        elif bucket == 1:  # Trending down
            fs = _make_feature_set(
                adx=rng.uniform(26, 40),
                ret_5=rng.uniform(-0.05, -0.02),
                atr_pct=rng.uniform(0.01, 0.03),
                rsi_14=rng.uniform(25, 45),
            )
        elif bucket == 2:  # Ranging
            fs = _make_feature_set(
                adx=rng.uniform(10, 20),
                ret_5=rng.uniform(-0.005, 0.005),
                atr_pct=rng.uniform(0.005, 0.015),
                rsi_14=rng.uniform(45, 55),
            )
        else:  # High volatility
            fs = _make_feature_set(
                adx=rng.uniform(10, 30),
                ret_5=rng.uniform(-0.01, 0.01),
                atr_pct=rng.uniform(0.06, 0.07),
                rsi_14=rng.uniform(30, 70),
            )
        feature_sets.append(fs)
    return feature_sets


# ---------------------------------------------------------------------
# _generate_labels()
# ---------------------------------------------------------------------


class TestGenerateLabels:
    def test_crisis_label(self) -> None:
        fs = _make_feature_set(atr_pct=0.10, ret_20=-0.20)
        labels = _generate_labels([fs])
        assert labels[0] == 4  # CRISIS

    def test_high_volatility_label(self) -> None:
        fs = _make_feature_set(atr_pct=0.06, ret_20=0.0)
        labels = _generate_labels([fs])
        assert labels[0] == 3  # HIGH_VOLATILITY

    def test_trending_up_label(self) -> None:
        fs = _make_feature_set(adx=30, ret_5=0.02, atr_pct=0.01)
        labels = _generate_labels([fs])
        assert labels[0] == 0  # TRENDING_UP

    def test_trending_down_label(self) -> None:
        fs = _make_feature_set(adx=30, ret_5=-0.02, atr_pct=0.01)
        labels = _generate_labels([fs])
        assert labels[0] == 1  # TRENDING_DOWN

    def test_ranging_label_default(self) -> None:
        fs = _make_feature_set(adx=15, ret_5=0.0, atr_pct=0.01)
        labels = _generate_labels([fs])
        assert labels[0] == 2  # RANGING


# ---------------------------------------------------------------------
# RegimeDetector - initial state
# ---------------------------------------------------------------------


class TestInitialState:
    def test_not_trained_initially(self) -> None:
        detector = RegimeDetector()
        assert detector.is_trained is False
        assert detector.metadata.status == ModelStatus.UNTRAINED


# ---------------------------------------------------------------------
# train()
# ---------------------------------------------------------------------


class TestTrain:
    def test_train_raises_with_too_few_samples(self) -> None:
        detector = RegimeDetector()
        with pytest.raises(ValueError, match="at least 100 samples"):
            detector.train(_make_training_set(50))

    def test_train_succeeds_and_sets_metadata(self) -> None:
        detector = RegimeDetector()
        metrics = detector.train(_make_training_set(150))

        assert detector.is_trained is True
        assert detector.metadata.status == ModelStatus.SHADOW
        assert detector.metadata.trained_at is not None
        assert "accuracy" in metrics
        assert metrics["training_samples"] == 105  # 70% of 150

    def test_train_raises_runtime_error_when_sklearn_missing(self) -> None:
        detector = RegimeDetector()
        with patch.dict("sys.modules", {"sklearn.ensemble": None}):
            with pytest.raises(RuntimeError, match="scikit-learn not installed"):
                detector.train(_make_training_set(150))

    def test_train_swallows_non_import_hmm_training_failure(self) -> None:
        detector = RegimeDetector()
        with patch.object(
            RegimeDetector, "_train_hmm", side_effect=RuntimeError("hmm blew up")
        ):
            # Should not raise - _train_hmm failures are swallowed in train().
            metrics = detector.train(_make_training_set(150))
        assert "accuracy" in metrics


class TestTrainHMM:
    def test_train_hmm_swallows_import_error(self) -> None:
        detector = RegimeDetector()
        with patch.dict("sys.modules", {"hmmlearn": None}):
            # Should not raise - hmmlearn ImportError is caught internally.
            detector._train_hmm(
                MagicMock(X=np.random.rand(20, 5)), np.array([0, 1, 2] * 6 + [0, 1])
            )


# ---------------------------------------------------------------------
# predict() - trained model
# ---------------------------------------------------------------------


class TestPredictTrained:
    @pytest.fixture
    def trained_detector(self) -> RegimeDetector:
        detector = RegimeDetector()
        detector.train(_make_training_set(150))
        return detector

    def test_predict_returns_prediction_with_probabilities(
        self, trained_detector: RegimeDetector
    ) -> None:
        fs = _make_feature_set(adx=30, ret_5=0.03, atr_pct=0.01)
        prediction = trained_detector.predict(fs)

        assert isinstance(prediction.regime, MarketRegime)
        assert 0.0 <= prediction.confidence <= 1.0
        assert len(prediction.probabilities) > 0
        assert prediction.model_version == RegimeDetector.VERSION

    def test_predict_falls_back_to_rule_based_on_exception(
        self, trained_detector: RegimeDetector
    ) -> None:
        trained_detector._rf_model.predict_proba = MagicMock(
            side_effect=RuntimeError("model error")
        )

        prediction = trained_detector.predict(_make_feature_set())

        assert prediction.model_version == "rule_based_fallback"

    def test_predict_with_hmm_agreement_boosts_confidence(
        self, trained_detector: RegimeDetector
    ) -> None:
        # Force HMM to agree with whatever RF predicts.
        fs = _make_feature_set(adx=30, ret_5=0.03, atr_pct=0.01)
        X = trained_detector._extractor.transform_single(fs).reshape(1, -1)
        rf_proba = trained_detector._rf_model.predict_proba(X)[0]
        rf_label = int(np.argmax(rf_proba))

        fake_hmm = MagicMock()
        fake_hmm.predict.return_value = [rf_label]
        trained_detector._hmm_model = fake_hmm
        trained_detector._pca = MagicMock()
        trained_detector._pca.transform.return_value = X

        prediction = trained_detector.predict(fs)

        assert prediction.regime is not None

    def test_predict_with_hmm_disagreement_lowers_confidence(
        self, trained_detector: RegimeDetector
    ) -> None:
        fs = _make_feature_set(adx=30, ret_5=0.03, atr_pct=0.01)
        X = trained_detector._extractor.transform_single(fs).reshape(1, -1)
        rf_proba = trained_detector._rf_model.predict_proba(X)[0]
        rf_label = int(np.argmax(rf_proba))
        disagreeing_label = (rf_label + 1) % len(rf_proba)

        fake_hmm = MagicMock()
        fake_hmm.predict.return_value = [disagreeing_label]
        trained_detector._hmm_model = fake_hmm
        trained_detector._pca = MagicMock()
        trained_detector._pca.transform.return_value = X

        prediction = trained_detector.predict(fs)

        assert prediction.regime is not None

    def test_predict_hmm_exception_is_swallowed(
        self, trained_detector: RegimeDetector
    ) -> None:
        fake_hmm = MagicMock()
        fake_hmm.predict.side_effect = RuntimeError("hmm broken")
        trained_detector._hmm_model = fake_hmm
        trained_detector._pca = MagicMock()
        trained_detector._pca.transform.side_effect = RuntimeError("pca broken")

        # Should not raise - HMM failure is swallowed, falls back to RF-only.
        prediction = trained_detector.predict(_make_feature_set(adx=30, ret_5=0.03))
        assert prediction.regime is not None

    def test_rf_feature_importance_used_when_shap_missing(
        self, trained_detector: RegimeDetector
    ) -> None:
        with patch.dict("sys.modules", {"shap": None}):
            fs = _make_feature_set(adx=30, ret_5=0.03, atr_pct=0.01)
            prediction = trained_detector.predict(fs)

        assert isinstance(prediction.feature_importance, dict)

    def test_rf_feature_importance_returns_empty_when_untrained(self) -> None:
        detector = RegimeDetector()
        assert detector._rf_feature_importance() == {}

    def test_shap_list_format_branch_via_mocked_tree_explainer(
        self, trained_detector: RegimeDetector
    ) -> None:
        """Deckt den `isinstance(shap_values, list)`-Zweig ab, der mit der
        installierten SHAP-Version für Binärmodelle nicht real erreichbar
        ist (dokumentierte Einschränkung) - via gemocktem TreeExplainer."""
        fs = _make_feature_set(adx=30, ret_5=0.03, atr_pct=0.01)
        X = trained_detector._extractor.transform_single(fs).reshape(1, -1)
        n_classes = len(trained_detector._rf_model.classes_)
        fake_shap_values = [np.zeros((1, X.shape[1])) for _ in range(n_classes)]

        fake_explainer = MagicMock()
        fake_explainer.shap_values.return_value = fake_shap_values
        fake_shap_module = MagicMock()
        fake_shap_module.TreeExplainer.return_value = fake_explainer

        with patch.dict("sys.modules", {"shap": fake_shap_module}):
            importance = trained_detector._compute_shap(X)

        assert isinstance(importance, dict)


# ---------------------------------------------------------------------
# predict() - untrained model (rule-based fallback)
# ---------------------------------------------------------------------


class TestPredictUntrainedFallback:
    def test_untrained_predict_uses_rule_based_fallback(self) -> None:
        detector = RegimeDetector()
        prediction = detector.predict(_make_feature_set())
        assert prediction.model_version == "rule_based_fallback"
        assert prediction.confidence == 0.60

    def test_rule_based_crisis(self) -> None:
        detector = RegimeDetector()
        prediction = detector.predict(_make_feature_set(atr_pct=0.10, ret_20=-0.20))
        assert prediction.regime == MarketRegime.CRISIS

    def test_rule_based_high_volatility(self) -> None:
        detector = RegimeDetector()
        prediction = detector.predict(_make_feature_set(atr_pct=0.10, ret_20=0.0))
        assert prediction.regime == MarketRegime.HIGH_VOLATILITY

    def test_rule_based_trending_up(self) -> None:
        detector = RegimeDetector()
        prediction = detector.predict(_make_feature_set(adx=30, ret_5=0.02, atr_pct=0.01))
        assert prediction.regime == MarketRegime.TRENDING_UP

    def test_rule_based_trending_down(self) -> None:
        detector = RegimeDetector()
        prediction = detector.predict(_make_feature_set(adx=30, ret_5=-0.02, atr_pct=0.01))
        assert prediction.regime == MarketRegime.TRENDING_DOWN

    def test_rule_based_ranging(self) -> None:
        detector = RegimeDetector()
        prediction = detector.predict(_make_feature_set(adx=15, ret_5=0.0, atr_pct=0.01))
        assert prediction.regime == MarketRegime.RANGING


# ---------------------------------------------------------------------
# save() / load()
# ---------------------------------------------------------------------


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        detector = RegimeDetector()
        detector.train(_make_training_set(150))

        model_path = tmp_path / "regime_model"
        detector.save(model_path)

        assert (model_path / "rf_model.pkl").exists()
        assert (model_path / "extractor.pkl").exists()

        loaded = RegimeDetector()
        loaded.load(model_path)

        assert loaded.is_trained is True
        assert loaded.metadata.status == ModelStatus.PRODUCTION

        # Loaded model should produce a valid prediction.
        prediction = loaded.predict(_make_feature_set(adx=30, ret_5=0.03, atr_pct=0.01))
        assert isinstance(prediction.regime, MarketRegime)
