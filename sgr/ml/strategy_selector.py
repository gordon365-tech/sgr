"""
SGR Strategy Selector
======================
Wählt optimale Strategie basierend auf aktuellem Regime + Features.

Modell: Gradient Boosting (LightGBM)
    - Schnell für tabellarische Daten
    - Native SHAP-Unterstützung
    - Kein Overfitting durch regularisierten Boosting

Training:
    Für jede (Regime, Strategy)-Kombination: hat Strategie profitiert?
    Label: 1 wenn Strategie in diesem Regime positiven Expected Value hatte
    Features: aktuelles Feature-Set + Regime-Label + Volatilität

Output:
    Score pro Strategie → Registry aktiviert/deaktiviert entsprechend
    Empfehlung: "In TRENDING_UP-Regime: trend_following_v1 (Score 0.84)"

SHAP Integration:
    Erklärt warum eine Strategie empfohlen wird:
    "ADX=35 und Volume Ratio=1.8 sprechen für TrendFollowing (+0.23 SHAP)"
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import numpy as np

from sgr.core.logging import get_logger
from sgr.core.types import MarketRegime
from sgr.market_data.types import FeatureSet
from sgr.ml.features import REGIME_FEATURES, FeatureExtractor
from sgr.ml.types import ModelMetadata, ModelStatus, ModelType, StrategyScore

log = get_logger(__name__)


class StrategySelector:
    """
    ML-basierter Strategy Selector.

    Gibt für jeden Strategie-Kandidaten einen Score zurück.
    Registry-Aktivierung basiert auf Score-Schwellwert.
    """

    VERSION = "1.0.0"
    MIN_SCORE_THRESHOLD = 0.55

    def __init__(self, strategy_names: list[str]) -> None:
        self._strategy_names = strategy_names
        self._models: dict[str, Any] = {}  # strategy_name → LGB model
        self._extractor = FeatureExtractor(REGIME_FEATURES)
        self._fitted = False
        self._metadata = ModelMetadata(
            model_id=str(uuid4()),
            model_type=ModelType.STRATEGY_SELECTOR,
            version=self.VERSION,
            status=ModelStatus.UNTRAINED,
            feature_names=REGIME_FEATURES,
        )

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    def train(
        self,
        feature_sets: list[FeatureSet],
        strategy_returns: dict[str, list[float]],
        train_split: float = 0.70,
    ) -> dict[str, float]:
        """
        Trainiert Selector für jede Strategie.

        Args:
            feature_sets: Zeitlich sortierte FeatureSets
            strategy_returns: {strategy_name: [return_per_bar, ...]}
                              Positiv = Strategie hätte Gewinn gemacht
            train_split: Temporal Split-Ratio

        Returns:
            Validation-Accuracy pro Strategie
        """
        try:
            import lightgbm as lgb
        except ImportError:
            log.warning("strategy_selector.lgb_not_available", note="Using RandomForest fallback")
            return self._train_rf_fallback(feature_sets, strategy_returns, train_split)

        if len(feature_sets) < 100:
            raise ValueError(f"Need at least 100 samples, got {len(feature_sets)}")

        split_idx = int(len(feature_sets) * train_split)
        train_features = feature_sets[:split_idx]
        test_features = feature_sets[split_idx:]

        train_matrix = self._extractor.fit_transform(train_features)
        test_matrix = self._extractor.transform(test_features)

        validation_results: dict[str, float] = {}

        for strategy_name in self._strategy_names:
            returns = strategy_returns.get(strategy_name, [])
            if len(returns) != len(feature_sets):
                log.warning(
                    "strategy_selector.return_length_mismatch",
                    strategy=strategy_name,
                    expected=len(feature_sets),
                    got=len(returns),
                )
                continue

            # Label: 1 wenn Return > 0 (Strategie war profitabel)
            y = np.array([1 if r > 0 else 0 for r in returns])
            y_train = y[:split_idx]
            y_test = y[split_idx:]

            # LightGBM Training
            dtrain = lgb.Dataset(train_matrix.X, label=y_train)

            params = {
                "objective": "binary",
                "metric": "binary_logloss",
                "num_leaves": 31,
                "learning_rate": 0.05,
                "feature_fraction": 0.8,
                "bagging_fraction": 0.8,
                "bagging_freq": 5,
                "verbose": -1,
                "random_state": 42,
            }

            model = lgb.train(
                params,
                dtrain,
                num_boost_round=100,
                valid_sets=[lgb.Dataset(test_matrix.X, label=y_test)],
                callbacks=[lgb.early_stopping(10, verbose=False)],
            )

            self._models[strategy_name] = model

            # Validation Accuracy
            y_pred = (model.predict(test_matrix.X) > 0.5).astype(int)
            accuracy = float(np.mean(y_pred == y_test))
            validation_results[strategy_name] = accuracy

            log.info(
                "strategy_selector.strategy_trained",
                strategy=strategy_name,
                accuracy=f"{accuracy:.3f}",
            )

        self._fitted = bool(self._models)
        if self._fitted:
            self._metadata.status = ModelStatus.SHADOW
            self._metadata.trained_at = datetime.now(tz=UTC)
            self._metadata.training_samples = split_idx

        return validation_results

    def _train_rf_fallback(
        self,
        feature_sets: list[FeatureSet],
        strategy_returns: dict[str, list[float]],
        train_split: float,
    ) -> dict[str, float]:
        """RandomForest Fallback wenn LightGBM nicht verfügbar."""
        from sklearn.ensemble import RandomForestClassifier

        split_idx = int(len(feature_sets) * train_split)
        train_features = feature_sets[:split_idx]
        test_features = feature_sets[split_idx:]
        train_matrix = self._extractor.fit_transform(train_features)
        test_matrix = self._extractor.transform(test_features)

        results: dict[str, float] = {}
        for strategy_name in self._strategy_names:
            returns = strategy_returns.get(strategy_name, [])
            if len(returns) != len(feature_sets):
                continue

            y = np.array([1 if r > 0 else 0 for r in returns])
            y_train = y[:split_idx]
            y_test = y[split_idx:]

            model = RandomForestClassifier(n_estimators=100, random_state=42, n_jobs=-1)
            model.fit(train_matrix.X, y_train)
            self._models[strategy_name] = model

            y_pred = model.predict(test_matrix.X)
            results[strategy_name] = float(np.mean(y_pred == y_test))

        self._fitted = bool(self._models)
        if self._fitted:
            self._metadata.status = ModelStatus.SHADOW
        return results

    def select(
        self,
        features: FeatureSet,
        regime: MarketRegime,
    ) -> list[StrategyScore]:
        """
        Bewertet alle Strategien für aktuelle Marktbedingungen.

        Returns:
            Liste von StrategyScores (sortiert nach Score descending).
        """
        if not self._fitted:
            return self._rule_based_selection(regime)

        X = self._extractor.transform_single(features).reshape(1, -1)
        scores: list[StrategyScore] = []

        for strategy_name, model in self._models.items():
            try:
                # Probability für "Strategy works here"
                if hasattr(model, "predict_proba"):
                    score = float(model.predict_proba(X)[0][1])
                else:
                    score = float(model.predict(X)[0])

                # Regime-Fitness (regelbasiert)
                regime_fit = self._compute_regime_fit(strategy_name, regime)

                # Combined Score
                combined = score * 0.7 + regime_fit * 0.3

                # Feature Alignment via SHAP oder Feature Importance
                alignment = self._get_feature_alignment(model, X, strategy_name)

                scores.append(
                    StrategyScore(
                        strategy_name=strategy_name,
                        score=round(combined, 4),
                        expected_sharpe=0.0,  # Aus Performance-History befüllt
                        regime_fit=round(regime_fit, 4),
                        feature_alignment=alignment,
                        recommended=combined >= self.MIN_SCORE_THRESHOLD,
                    )
                )

            except Exception as e:
                log.warning(
                    "strategy_selector.score_error",
                    strategy=strategy_name,
                    error=str(e),
                )

        scores.sort(key=lambda s: s.score, reverse=True)
        return scores

    def _compute_regime_fit(self, strategy_name: str, regime: MarketRegime) -> float:
        """Regelbasierte Regime-Fitness (Fallback + Boost für bekannte Matches)."""
        from sgr.strategy.registry import StrategyRegistry

        registry = StrategyRegistry.get()
        entry = registry.get_entry(strategy_name)
        if entry and regime in entry.strategy.supported_regimes:
            return 1.0
        return 0.2  # Geringe Fitness für nicht-unterstütztes Regime

    def _get_feature_alignment(
        self,
        model: Any,
        X: np.ndarray,
        strategy_name: str,
    ) -> dict[str, float]:
        """SHAP oder Feature Importance für Erklärbarkeit."""
        try:
            import shap

            explainer = shap.TreeExplainer(model)
            shap_values = explainer.shap_values(X)
            if isinstance(shap_values, list):
                vals = shap_values[1][0] if len(shap_values) > 1 else shap_values[0][0]
            else:
                vals = shap_values[0]
            return {
                name: round(float(val), 6)
                for name, val in zip(self._extractor.feature_names, vals, strict=False)
                if abs(val) > 0.001
            }
        except Exception:
            if hasattr(model, "feature_importances_"):
                return {
                    name: round(float(imp), 6)
                    for name, imp in zip(
                        self._extractor.feature_names,
                        model.feature_importances_,
                        strict=False,
                    )
                    if imp > 0.01
                }
            return {}

    def _rule_based_selection(self, regime: MarketRegime) -> list[StrategyScore]:
        """Fallback wenn ML nicht verfügbar."""
        from sgr.strategy.registry import StrategyRegistry

        registry = StrategyRegistry.get()
        scores = []
        for name in self._strategy_names:
            entry = registry.get_entry(name)
            if entry and regime in entry.strategy.supported_regimes:
                scores.append(
                    StrategyScore(
                        strategy_name=name,
                        score=0.65,
                        expected_sharpe=0.0,
                        regime_fit=1.0,
                        feature_alignment={},
                        recommended=True,
                    )
                )
        return scores
