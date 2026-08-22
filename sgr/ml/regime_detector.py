"""
SGR Regime Detector
===================
Erkennt aktuelles Marktregime aus technischen Features.

Architektur: Ensemble aus zwei Modellen
    1. Hidden Markov Model (HMM): für zeitliche Persistenz von Regimen
       (Regime wechselt nicht jeden Bar – HMM smootht Vorhersagen)
    2. Random Forest Classifier: für aktuelle Feature-basierte Klassifikation
       (Schnelle Anpassung an neue Bedingungen)

    Final Prediction:
        Wenn HMM und RF übereinstimmen → hohes Confidence
        Wenn beide widersprechen → niedrigeres Confidence, beide Probabilities anzeigen

Labels (unsupervised via Heuristik):
    Kein manuelles Labeling nötig. Labels werden aus historischen
    Returns + Volatilität + ADX generiert:
        ADX > 25, positive Returns → TRENDING_UP
        ADX > 25, negative Returns → TRENDING_DOWN
        ADX < 20 → RANGING
        ATR > 2x Durchschnitt → HIGH_VOLATILITY
        Extreme Verluste → CRISIS

SHAP Explainability:
    Für jede Vorhersage werden SHAP-Werte berechnet.
    Zeigt welche Features die Regime-Entscheidung getrieben haben.
    Audit-Trail: "RSI=72 und ADX=35 → TRENDING_UP mit 84% Konfidenz"

Temporal Split für Training:
    70% Training, 30% Out-of-Sample Validation.
    KEIN zufälliger Split (Look-Ahead Bias).
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from sgr.core.logging import get_logger
from sgr.core.types import MarketRegime
from sgr.market_data.types import FeatureSet
from sgr.ml.features import REGIME_FEATURES, FeatureExtractor, FeatureMatrix
from sgr.ml.types import ModelMetadata, ModelStatus, ModelType, RegimePrediction

log = get_logger(__name__)

# Regime → integer label mapping
_REGIME_TO_LABEL: dict[MarketRegime, int] = {
    MarketRegime.TRENDING_UP: 0,
    MarketRegime.TRENDING_DOWN: 1,
    MarketRegime.RANGING: 2,
    MarketRegime.HIGH_VOLATILITY: 3,
    MarketRegime.CRISIS: 4,
    MarketRegime.UNKNOWN: 5,  # Unique label for unknown regime
}
_LABEL_TO_REGIME: dict[int, MarketRegime] = {v: k for k, v in _REGIME_TO_LABEL.items()}


def _generate_labels(feature_sets: list[FeatureSet]) -> np.ndarray:
    """
    Generiert Regime-Labels aus technischen Indikatoren (heuristisch).
    Kein manuelles Labeling – vollständig automatisch.
    """
    labels = []
    for fs in feature_sets:
        ind = fs.indicators
        adx = ind.adx_14 or 0.0
        atr_pct = ind.atr_pct or 0.01
        ret_5 = fs.returns_5 or 0.0
        ret_20 = fs.returns_20 or 0.0

        # Crisis: sehr hohe Volatilität + starke negative Returns
        if atr_pct > 0.08 and ret_20 < -0.15:
            labels.append(_REGIME_TO_LABEL[MarketRegime.CRISIS])
        # High Volatility: hohe ATR
        elif atr_pct > 0.05:
            labels.append(_REGIME_TO_LABEL[MarketRegime.HIGH_VOLATILITY])
        # Trending Up: starker Aufwärtstrend
        elif adx > 25 and ret_5 > 0.01:
            labels.append(_REGIME_TO_LABEL[MarketRegime.TRENDING_UP])
        # Trending Down: starker Abwärtstrend
        elif adx > 25 and ret_5 < -0.01:
            labels.append(_REGIME_TO_LABEL[MarketRegime.TRENDING_DOWN])
        # Ranging: schwacher Trend
        else:
            labels.append(_REGIME_TO_LABEL[MarketRegime.RANGING])

    return np.array(labels)


class RegimeDetector:
    """
    Regime Detector mit Random Forest + optionalem HMM.
    Stateful: trainiertes Modell wird in-memory gehalten.

    Usage:
        detector = RegimeDetector()
        detector.train(historical_features)
        prediction = detector.predict(current_features)
    """

    VERSION = "1.0.0"

    def __init__(self) -> None:
        self._rf_model: Any = None  # sklearn RandomForestClassifier
        self._hmm_model: Any = None  # hmmlearn GaussianHMM
        self._extractor = FeatureExtractor(REGIME_FEATURES)
        self._metadata = ModelMetadata(
            model_id=str(uuid4()),
            model_type=ModelType.REGIME_DETECTOR,
            version=self.VERSION,
            status=ModelStatus.UNTRAINED,
            feature_names=REGIME_FEATURES,
        )

    @property
    def is_trained(self) -> bool:
        return self._rf_model is not None

    @property
    def metadata(self) -> ModelMetadata:
        return self._metadata

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(
        self,
        feature_sets: list[FeatureSet],
        n_estimators: int = 200,
        max_depth: int = 8,
        train_split: float = 0.70,
    ) -> dict[str, float]:
        """
        Trainiert Regime Detector auf historischen Features.

        Args:
            feature_sets: Zeitlich sortierte FeatureSets (älteste zuerst)
            n_estimators: Anzahl Trees im Random Forest
            max_depth: Max Tiefe der Trees (Overfitting-Kontrolle)
            train_split: Anteil Trainings-Daten (temporal split)

        Returns:
            Validation-Metriken (accuracy, per-class F1)
        """
        try:
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.metrics import accuracy_score, classification_report
        except ImportError:
            raise RuntimeError(
                "scikit-learn not installed. Run: pip install scikit-learn"
            ) from None

        if len(feature_sets) < 100:
            raise ValueError(f"Need at least 100 samples, got {len(feature_sets)}")

        self._metadata.status = ModelStatus.TRAINING

        # Labels generieren
        y = _generate_labels(feature_sets)

        # Temporal Split (kein Random Split!)
        split_idx = int(len(feature_sets) * train_split)
        train_features = feature_sets[:split_idx]
        test_features = feature_sets[split_idx:]
        y_train = y[:split_idx]
        y_test = y[split_idx:]

        # Feature Matrix
        train_matrix = self._extractor.fit_transform(train_features)
        test_matrix = self._extractor.transform(test_features)

        # Random Forest Training
        self._rf_model = RandomForestClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            class_weight="balanced",  # Unbalancierte Labels ausgleichen
            random_state=42,
            n_jobs=-1,
        )
        self._rf_model.fit(train_matrix.X, y_train)

        # Validation
        y_pred = self._rf_model.predict(test_matrix.X)
        accuracy = float(accuracy_score(y_test, y_pred))

        # Per-Class Metriken. _generate_labels() erzeugt nur die tatsaechlich
        # heuristisch erreichbaren Regimes; nicht jedes Label aus
        # _LABEL_TO_REGIME muss im Test-Split vorkommen (z.B. UNKNOWN nie).
        # labels=... explizit setzen, damit target_names und die tatsaechlich
        # ausgewerteten Klassen immer uebereinstimmen (sonst ValueError bei
        # Groessen-Mismatch).
        present_labels = sorted(set(int(v) for v in y_train) | set(int(v) for v in y_test))
        class_names = [_LABEL_TO_REGIME[label].value for label in present_labels]
        report = classification_report(
            y_test,
            y_pred,
            labels=present_labels,
            target_names=class_names,
            output_dict=True,
            zero_division=0,
        )

        # Optionales HMM für Temporal Smoothing
        try:
            self._train_hmm(train_matrix, y_train)
        except Exception as e:
            log.warning("regime_detector.hmm_training_failed", error=str(e))

        self._metadata.status = ModelStatus.SHADOW
        self._metadata.trained_at = datetime.now(tz=UTC)
        self._metadata.training_samples = len(train_features)
        self._metadata.validation_accuracy = accuracy
        self._metadata.validation_details = {"report": report, "accuracy": accuracy}
        self._metadata.hyperparameters = {
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "train_split": train_split,
        }

        log.info(
            "regime_detector.trained",
            samples=len(feature_sets),
            train_samples=len(train_features),
            val_samples=len(test_features),
            accuracy=f"{accuracy:.3f}",
        )

        return {"accuracy": accuracy, "training_samples": len(train_features)}

    def _train_hmm(self, train_matrix: FeatureMatrix, y_train: np.ndarray) -> None:
        """Trainiert HMM für temporale Glättung der Regime-Vorhersagen."""
        try:
            from hmmlearn import hmm

            n_components = len(set(y_train))
            self._hmm_model = hmm.GaussianHMM(
                n_components=n_components,
                covariance_type="diag",
                n_iter=100,
                random_state=42,
            )
            # HMM auf PCA-reduzierten Features (Stabilität)
            from sklearn.decomposition import PCA

            n_components_pca = min(5, train_matrix.X.shape[1])
            pca = PCA(n_components=n_components_pca)
            X_pca = pca.fit_transform(train_matrix.X)
            self._hmm_model.fit(X_pca)
            self._pca = pca
            log.info("regime_detector.hmm_trained", n_states=n_components)
        except ImportError:
            log.warning("regime_detector.hmmlearn_not_available")

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def predict(self, features: FeatureSet) -> RegimePrediction:
        """
        Sagt Marktregime voraus für aktuelles FeatureSet.
        Gibt Regime + Konfidenz + SHAP Feature Importance zurück.

        Falls nicht trainiert: regelbasierter Fallback.
        """
        if not self.is_trained:
            return self._rule_based_fallback(features)

        try:
            X = self._extractor.transform_single(features).reshape(1, -1)

            # Random Forest Prediction
            rf_proba = self._rf_model.predict_proba(X)[0]
            rf_label = int(np.argmax(rf_proba))
            rf_regime = _LABEL_TO_REGIME.get(rf_label, MarketRegime.UNKNOWN)
            rf_confidence = float(rf_proba[rf_label])

            # HMM Smoothing (falls verfügbar)
            final_regime = rf_regime
            final_confidence = rf_confidence

            if self._hmm_model is not None:
                try:
                    X_pca = self._pca.transform(X)
                    hmm_label = int(self._hmm_model.predict(X_pca)[0])
                    hmm_regime = _LABEL_TO_REGIME.get(hmm_label, MarketRegime.UNKNOWN)

                    if hmm_regime == rf_regime:
                        # Konsensus: höhere Konfidenz
                        final_confidence = min(rf_confidence * 1.1, 1.0)
                    else:
                        # Widerspruch: niedrigere Konfidenz
                        final_confidence = rf_confidence * 0.8
                except Exception:
                    pass

            # Probabilities für alle Regime
            probabilities = {
                _LABEL_TO_REGIME.get(i, MarketRegime.UNKNOWN).value: float(p)
                for i, p in enumerate(rf_proba)
            }

            # SHAP Feature Importance
            feature_importance = self._compute_shap(X)

            return RegimePrediction(
                regime=final_regime,
                confidence=final_confidence,
                probabilities=probabilities,
                feature_importance=feature_importance,
                model_version=self.VERSION,
                predicted_at=datetime.now(tz=UTC),
            )

        except Exception as e:
            log.error("regime_detector.predict_error", error=str(e))
            return self._rule_based_fallback(features)

    def _compute_shap(self, X: np.ndarray) -> dict[str, float]:
        """
        Berechnet SHAP-Werte für eine Vorhersage.
        Erklärt welche Features die Entscheidung getrieben haben.
        """
        try:
            import shap

            explainer = shap.TreeExplainer(self._rf_model)
            shap_values = explainer.shap_values(X)

            # shap_values ist List[array] (eine pro Klasse)
            # Wir nehmen die der predicted Klasse
            pred_class = int(self._rf_model.predict(X)[0])
            if isinstance(shap_values, list) and pred_class < len(shap_values):
                class_shap = shap_values[pred_class][0]
            else:
                class_shap = (
                    shap_values[0]
                    if len(shap_values) > 0
                    else np.zeros(len(self._extractor.feature_names))
                )

            return {
                name: round(float(val), 6)
                for name, val in zip(self._extractor.feature_names, class_shap, strict=False)
            }
        except ImportError:
            # SHAP nicht installiert: Feature Importance aus RF
            return self._rf_feature_importance()
        except Exception as e:
            log.debug("regime_detector.shap_error", error=str(e))
            return self._rf_feature_importance()

    def _rf_feature_importance(self) -> dict[str, float]:
        """Fallback: RF Feature Importance (Gini, nicht SHAP)."""
        if self._rf_model is None:
            return {}
        importances = self._rf_model.feature_importances_
        return {
            name: round(float(imp), 6)
            for name, imp in zip(self._extractor.feature_names, importances, strict=False)
        }

    def _rule_based_fallback(self, features: FeatureSet) -> RegimePrediction:
        """
        Regelbasierter Fallback wenn Modell nicht verfügbar.
        Identisch zur Heuristik in BacktestSimulator.
        """
        ind = features.indicators
        adx = ind.adx_14 or 0.0
        atr_pct = ind.atr_pct or 0.01
        ret_5 = features.returns_5 or 0.0

        if atr_pct > 0.08:
            regime = (
                MarketRegime.CRISIS
                if (features.returns_20 or 0) < -0.15
                else MarketRegime.HIGH_VOLATILITY
            )
        elif adx > 25 and ret_5 > 0.01:
            regime = MarketRegime.TRENDING_UP
        elif adx > 25 and ret_5 < -0.01:
            regime = MarketRegime.TRENDING_DOWN
        else:
            regime = MarketRegime.RANGING

        return RegimePrediction(
            regime=regime,
            confidence=0.60,  # Konservative Konfidenz für Regelbasiert
            probabilities={regime.value: 0.60},
            feature_importance={},
            model_version="rule_based_fallback",
            predicted_at=datetime.now(tz=UTC),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Speichert trainiertes Modell auf Disk."""
        path.mkdir(parents=True, exist_ok=True)
        with open(path / "rf_model.pkl", "wb") as f:
            pickle.dump(self._rf_model, f)
        with open(path / "extractor.pkl", "wb") as f:
            pickle.dump(self._extractor.get_params(), f)
        log.info("regime_detector.saved", path=str(path))

    def load(self, path: Path) -> None:
        """Lädt trainiertes Modell von Disk."""
        with open(path / "rf_model.pkl", "rb") as f:
            self._rf_model = pickle.load(f)
        with open(path / "extractor.pkl", "rb") as f:
            params = pickle.load(f)
            self._extractor = FeatureExtractor.from_params(params)
        self._metadata.status = ModelStatus.PRODUCTION
        log.info("regime_detector.loaded", path=str(path))
