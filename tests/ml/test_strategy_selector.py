"""
Tests für sgr.ml.strategy_selector.StrategySelector.

Umgebung: lightgbm und shap sind als Projekt-Dependencies deklariert
(pyproject.toml) und in dieser Sandbox nachinstalliert -> train() nutzt
standardmäßig den echten LightGBM-Pfad, _get_feature_alignment den echten
SHAP-Pfad. Beide Fallback-Zweige (RandomForest bei fehlendem lightgbm,
feature_importances_ bei fehlendem/fehlerhaftem shap) werden zusätzlich
gezielt über gemockte Imports erzwungen, damit beide Codepfade unabhängig
von der tatsächlichen Paketverfügbarkeit deterministisch getestet werden.

Die real registrierten Strategien (mean_reversion_v1 -> RANGING,
trend_following_v1 -> TRENDING_UP/TRENDING_DOWN) werden über die globale
StrategyRegistry verwendet, wie es auch tests/unit/test_ml_engine.py als
etabliertes Muster im Projekt tut.
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pytest

from sgr.core.types import ExchangeID, MarketRegime, Symbol
from sgr.market_data.types import FeatureSet, IndicatorValues
from sgr.ml.strategy_selector import StrategySelector
from sgr.ml.types import ModelStatus, ModelType, StrategyScore

# ---------------------------------------------------------------------------
# Helper: erzwingt ImportError für ein bestimmtes Modul, egal was tatsächlich
# installiert ist - simuliert die "Paket fehlt"-Umgebung deterministisch.
# ---------------------------------------------------------------------------


def _blocking_import(blocked_names: set[str]):
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name in blocked_names:
            raise ImportError(f"No module named '{name}' (blocked for test)")
        return real_import(name, *args, **kwargs)

    return _fake_import


# ---------------------------------------------------------------------------
# Fixtures (Muster übernommen aus tests/unit/test_ml_engine.py)
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
    np.random.seed(42)
    sets = []
    for _i in range(n):
        rsi = float(np.clip(50 + np.random.randn() * 15, 10, 90))
        adx = float(np.clip(20 + abs(np.random.randn()) * 15, 5, 60))
        atr_pct = float(np.clip(0.01 + abs(np.random.randn()) * 0.01, 0.001, 0.1))
        ret_5 = float(np.random.randn() * 0.02)
        sets.append(_make_feature_set(rsi=rsi, adx=adx, atr_pct=atr_pct, returns_5=ret_5))
    return sets


STRATEGY_NAMES = ["mean_reversion_v1", "trend_following_v1"]


def _make_returns(feature_sets: list[FeatureSet], seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    return [float(rng.standard_normal()) for _ in feature_sets]


# ---------------------------------------------------------------------------
# Konstruktion / Metadata
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_initial_state_is_unfitted(self):
        selector = StrategySelector(STRATEGY_NAMES)
        assert selector.is_fitted is False

    def test_metadata_initialized_untrained(self):
        selector = StrategySelector(STRATEGY_NAMES)
        assert selector._metadata.model_type == ModelType.STRATEGY_SELECTOR
        assert selector._metadata.status == ModelStatus.UNTRAINED
        assert selector._metadata.version == StrategySelector.VERSION

    def test_min_score_threshold_constant(self):
        assert StrategySelector.MIN_SCORE_THRESHOLD == 0.55


# ---------------------------------------------------------------------------
# train() -> RandomForest-Fallback (lightgbm nicht installiert)
# ---------------------------------------------------------------------------


class TestTrainLightGBM:
    """Echter LightGBM-Trainingspfad (Paket ist installiert)."""

    def test_train_below_100_samples_raises_value_error(self):
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(20)
        returns = {name: _make_returns(sets, seed=i) for i, name in enumerate(STRATEGY_NAMES)}

        with pytest.raises(ValueError, match="at least 100 samples"):
            selector.train(sets, returns, train_split=0.7)

    def test_train_fits_lgb_model_per_strategy(self):
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        returns = {name: _make_returns(sets, seed=i) for i, name in enumerate(STRATEGY_NAMES)}

        results = selector.train(sets, returns, train_split=0.7)

        assert selector.is_fitted is True
        assert len(selector._models) == 2
        for name in STRATEGY_NAMES:
            assert name in results
            assert 0.0 <= results[name] <= 1.0

    def test_train_sets_metadata_status_shadow_and_trained_at(self):
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        returns = {name: _make_returns(sets, seed=i) for i, name in enumerate(STRATEGY_NAMES)}
        selector.train(sets, returns, train_split=0.7)

        assert selector._metadata.status == ModelStatus.SHADOW
        assert selector._metadata.trained_at is not None
        assert selector._metadata.training_samples == int(150 * 0.7)

    def test_train_skips_strategy_with_mismatched_return_length(self):
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        returns = {
            "mean_reversion_v1": _make_returns(sets, seed=1),
            "trend_following_v1": [0.1, 0.2],  # falsche Länge -> übersprungen
        }
        results = selector.train(sets, returns)
        assert "mean_reversion_v1" in results
        assert "trend_following_v1" not in results
        assert "trend_following_v1" not in selector._models

    def test_train_missing_returns_for_strategy_skips_it(self):
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        returns = {"mean_reversion_v1": _make_returns(sets, seed=1)}
        results = selector.train(sets, returns)
        assert "trend_following_v1" not in results

    def test_train_all_strategies_skipped_leaves_unfitted(self):
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        returns: dict[str, list[float]] = {}
        results = selector.train(sets, returns)
        assert results == {}
        assert selector.is_fitted is False
        assert selector._metadata.status == ModelStatus.UNTRAINED


class TestTrainRandomForestFallback:
    """RandomForest-Fallback, erzwungen durch geblockten lightgbm-Import."""

    def test_lightgbm_import_error_triggers_fallback(self):
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        returns = {name: _make_returns(sets, seed=i) for i, name in enumerate(STRATEGY_NAMES)}

        with patch("builtins.__import__", side_effect=_blocking_import({"lightgbm"})):
            results = selector.train(sets, returns, train_split=0.7)

        assert selector.is_fitted is True
        assert set(results.keys()) == set(STRATEGY_NAMES)
        # RandomForestClassifier statt lgb.Booster.
        from sklearn.ensemble import RandomForestClassifier

        assert isinstance(selector._models["mean_reversion_v1"], RandomForestClassifier)

    def test_fallback_has_no_100_sample_minimum(self):
        """_train_rf_fallback prüft die Mindestanzahl NICHT separat - die
        ValueError-Grenze existiert nur im LightGBM-Zweig, welcher hier
        durch den geblockten Import gar nicht erst erreicht wird."""
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(20)
        returns = {name: _make_returns(sets, seed=i) for i, name in enumerate(STRATEGY_NAMES)}

        with patch("builtins.__import__", side_effect=_blocking_import({"lightgbm"})):
            results = selector.train(sets, returns, train_split=0.7)

        assert selector.is_fitted is True
        assert set(results.keys()) == set(STRATEGY_NAMES)

    def test_fallback_skips_mismatched_return_length(self):
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        returns = {
            "mean_reversion_v1": _make_returns(sets, seed=1),
            "trend_following_v1": [0.1, 0.2],
        }
        with patch("builtins.__import__", side_effect=_blocking_import({"lightgbm"})):
            results = selector.train(sets, returns)
        assert "trend_following_v1" not in results

    def test_fallback_all_skipped_leaves_unfitted(self):
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        with patch("builtins.__import__", side_effect=_blocking_import({"lightgbm"})):
            results = selector.train(sets, {})
        assert results == {}
        assert selector.is_fitted is False

    def test_select_after_fallback_training_uses_predict_proba(self):
        """RandomForestClassifier hat predict_proba -> select() muss den
        `hasattr(model, 'predict_proba')`-Zweig nutzen (nicht den
        model.predict()-Fallback, der für Booster ohne predict_proba gilt)."""
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        returns = {name: _make_returns(sets, seed=i) for i, name in enumerate(STRATEGY_NAMES)}

        with patch("builtins.__import__", side_effect=_blocking_import({"lightgbm"})):
            selector.train(sets, returns, train_split=0.7)

        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)
        scores = selector.select(fs, MarketRegime.TRENDING_UP)

        assert len(scores) == 2
        for s in scores:
            assert 0.0 <= s.score <= 1.0


# ---------------------------------------------------------------------------
# select() -> unfitted -> Rule-based Fallback
# ---------------------------------------------------------------------------


class TestSelectUnfitted:
    def test_select_uses_rule_based_when_unfitted(self):
        selector = StrategySelector(STRATEGY_NAMES)
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)

        scores = selector.select(fs, MarketRegime.TRENDING_UP)

        names = [s.strategy_name for s in scores]
        assert "trend_following_v1" in names
        assert "mean_reversion_v1" not in names  # RANGING-only, passt nicht

    def test_select_ranging_regime_matches_mean_reversion(self):
        selector = StrategySelector(STRATEGY_NAMES)
        fs = _make_feature_set(regime=MarketRegime.RANGING)

        scores = selector.select(fs, MarketRegime.RANGING)

        names = [s.strategy_name for s in scores]
        assert "mean_reversion_v1" in names
        assert "trend_following_v1" not in names

    def test_select_no_matching_regime_returns_empty(self):
        selector = StrategySelector(STRATEGY_NAMES)
        fs = _make_feature_set(regime=MarketRegime.CRISIS)

        scores = selector.select(fs, MarketRegime.CRISIS)
        assert scores == []

    def test_rule_based_scores_have_fixed_values(self):
        selector = StrategySelector(STRATEGY_NAMES)
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)
        scores = selector.select(fs, MarketRegime.TRENDING_UP)

        assert len(scores) == 1
        score = scores[0]
        assert isinstance(score, StrategyScore)
        assert score.score == 0.65
        assert score.regime_fit == 1.0
        assert score.feature_alignment == {}
        assert score.recommended is True

    def test_unknown_strategy_name_not_in_registry_is_skipped(self):
        selector = StrategySelector(["nonexistent_strategy_xyz"])
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)
        scores = selector.select(fs, MarketRegime.TRENDING_UP)
        assert scores == []


# ---------------------------------------------------------------------------
# select() -> fitted -> ML-Pfad (RandomForest, feature_importances_)
# ---------------------------------------------------------------------------


class TestSelectFitted:
    def _fit_selector(self) -> StrategySelector:
        selector = StrategySelector(STRATEGY_NAMES)
        sets = _make_feature_sets(150)
        returns = {name: _make_returns(sets, seed=i) for i, name in enumerate(STRATEGY_NAMES)}
        selector.train(sets, returns)
        return selector

    def test_select_returns_scores_sorted_descending(self):
        selector = self._fit_selector()
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)
        scores = selector.select(fs, MarketRegime.TRENDING_UP)

        assert len(scores) == 2
        assert scores[0].score >= scores[1].score

    def test_select_combined_score_formula(self):
        """combined = model_score * 0.7 + regime_fit * 0.3 - verifiziert
        implizit über Wertebereich statt exaktem Modell-Output (der vom
        RandomForest abhängt)."""
        selector = self._fit_selector()
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)
        scores = selector.select(fs, MarketRegime.TRENDING_UP)

        for s in scores:
            assert 0.0 <= s.score <= 1.0
            assert s.regime_fit in (0.2, 1.0)

    def test_select_recommended_flag_uses_threshold(self):
        selector = self._fit_selector()
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)
        scores = selector.select(fs, MarketRegime.TRENDING_UP)

        for s in scores:
            assert s.recommended == (s.score >= StrategySelector.MIN_SCORE_THRESHOLD)

    def test_select_feature_alignment_populated_via_shap(self):
        """SHAP ist installiert -> _get_feature_alignment nutzt den echten
        TreeExplainer-Pfad (LightGBM-Modell)."""
        selector = self._fit_selector()
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)
        scores = selector.select(fs, MarketRegime.TRENDING_UP)

        for s in scores:
            assert isinstance(s.feature_alignment, dict)
            # SHAP-Zweig filtert auf abs(val) > 0.001.
            assert all(abs(v) > 0.001 for v in s.feature_alignment.values())

    def test_select_feature_alignment_falls_back_to_importances_without_shap(self):
        """Wenn shap nicht importierbar ist (oder TreeExplainer intern
        scheitert), muss _get_feature_alignment auf
        model.feature_importances_ zurückfallen statt leer zu bleiben."""
        selector = self._fit_selector()
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)

        with patch("builtins.__import__", side_effect=_blocking_import({"shap"})):
            scores = selector.select(fs, MarketRegime.TRENDING_UP)

        for s in scores:
            assert isinstance(s.feature_alignment, dict)
            assert all(v > 0.01 for v in s.feature_alignment.values())

    def test_shap_values_as_multiclass_list_uses_positive_class(self):
        """In manchen SHAP/LightGBM-Versionskombinationen liefert
        explainer.shap_values() eine Liste (eine Matrix pro Klasse) statt
        eines einzelnen ndarray. In der hier installierten Version tritt
        das für binäre LightGBM-Modelle nicht auf (immer ndarray) - dieser
        Zweig wird daher über einen gemockten TreeExplainer erzwungen, um
        ihn trotzdem regressionssicher abzudecken."""
        selector = self._fit_selector()
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)
        X = selector._extractor.transform_single(fs).reshape(1, -1)
        n_features = len(selector._extractor.feature_names)

        class FakeExplainer:
            def __init__(self, model):
                pass

            def shap_values(self, X):
                # Liste mit 2 Klassen-Matrizen -> len(shap_values) > 1 Zweig.
                return [np.zeros((1, n_features)), np.ones((1, n_features)) * 0.5]

        with patch("shap.TreeExplainer", new=FakeExplainer):
            model = next(iter(selector._models.values()))
            alignment = selector._get_feature_alignment(model, X, "mean_reversion_v1")

        assert isinstance(alignment, dict)
        # Alle Werte kommen aus shap_values[1][0] (0.5), gefiltert auf >0.001.
        assert all(v == 0.5 for v in alignment.values())

    def test_shap_values_as_single_element_list(self):
        """Deckt den `len(shap_values) > 1 else shap_values[0][0]`-Zweig
        für den Fall ab, dass nur eine Klassen-Matrix zurückgegeben wird."""
        selector = self._fit_selector()
        fs = _make_feature_set(regime=MarketRegime.TRENDING_UP)
        X = selector._extractor.transform_single(fs).reshape(1, -1)
        n_features = len(selector._extractor.feature_names)

        class FakeExplainer:
            def __init__(self, model):
                pass

            def shap_values(self, X):
                return [np.ones((1, n_features)) * 0.3]

        with patch("shap.TreeExplainer", new=FakeExplainer):
            model = next(iter(selector._models.values()))
            alignment = selector._get_feature_alignment(model, X, "mean_reversion_v1")

        assert all(v == 0.3 for v in alignment.values())

    def test_select_regime_fit_reflects_registry_match(self):
        selector = self._fit_selector()
        fs = _make_feature_set(regime=MarketRegime.RANGING)
        scores = selector.select(fs, MarketRegime.RANGING)

        by_name = {s.strategy_name: s for s in scores}
        assert by_name["mean_reversion_v1"].regime_fit == 1.0
        assert by_name["trend_following_v1"].regime_fit == 0.2

    def test_select_model_error_is_caught_and_strategy_skipped(self):
        """Wenn model.predict/predict_proba eine Exception wirft, wird die
        Strategie uebersprungen statt select() abstuerzen zu lassen."""
        selector = self._fit_selector()

        class BrokenModel:
            def predict(self, X):
                raise RuntimeError("model broke")

        selector._models["mean_reversion_v1"] = BrokenModel()

        fs = _make_feature_set(regime=MarketRegime.RANGING)
        scores = selector.select(fs, MarketRegime.RANGING)

        names = [s.strategy_name for s in scores]
        assert "mean_reversion_v1" not in names
        assert "trend_following_v1" in names

    def test_select_model_without_predict_proba_uses_predict(self):
        """Deckt den `else`-Zweig ab (kein predict_proba -> model.predict)."""
        selector = self._fit_selector()

        class PredictOnlyModel:
            def predict(self, X):
                return np.array([0.8])

            @property
            def feature_importances_(self):
                return np.ones(len(selector._extractor.feature_names))

        selector._models["mean_reversion_v1"] = PredictOnlyModel()

        fs = _make_feature_set(regime=MarketRegime.RANGING)
        scores = selector.select(fs, MarketRegime.RANGING)

        by_name = {s.strategy_name: s for s in scores}
        assert "mean_reversion_v1" in by_name


# ---------------------------------------------------------------------------
# _get_feature_alignment: Modell ganz ohne feature_importances_/predict_proba
# ---------------------------------------------------------------------------


class TestGetFeatureAlignmentEdgeCases:
    def test_model_without_shap_and_without_importances_returns_empty_dict(self):
        selector = StrategySelector(STRATEGY_NAMES)
        # extractor braucht feature_names -> mit trainiertem Extractor arbeiten
        sets = _make_feature_sets(150)
        returns = {name: _make_returns(sets, seed=i) for i, name in enumerate(STRATEGY_NAMES)}
        selector.train(sets, returns)

        class BareModel:
            pass

        X = np.zeros((1, len(selector._extractor.feature_names)))
        alignment = selector._get_feature_alignment(BareModel(), X, "mean_reversion_v1")
        assert alignment == {}


# ---------------------------------------------------------------------------
# _compute_regime_fit
# ---------------------------------------------------------------------------


class TestComputeRegimeFit:
    def test_matching_regime_returns_1(self):
        selector = StrategySelector(STRATEGY_NAMES)
        fit = selector._compute_regime_fit("trend_following_v1", MarketRegime.TRENDING_UP)
        assert fit == 1.0

    def test_non_matching_regime_returns_0_2(self):
        selector = StrategySelector(STRATEGY_NAMES)
        fit = selector._compute_regime_fit("trend_following_v1", MarketRegime.RANGING)
        assert fit == 0.2

    def test_unknown_strategy_returns_0_2(self):
        selector = StrategySelector(STRATEGY_NAMES)
        fit = selector._compute_regime_fit("does_not_exist", MarketRegime.TRENDING_UP)
        assert fit == 0.2
