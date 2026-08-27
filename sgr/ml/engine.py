"""
SGR ML Engine
=============
Orchestriert alle ML-Komponenten und integriert sie in den Trading-Flow.

Integration in den Data Flow:
    FeatureSet (Market Data Engine)
         ↓
    RegimeDetector.predict() → RegimePrediction
         ↓
    VolatilityForecaster.predict() → VolatilityForecast
         ↓
    StrategySelector.select() → [StrategyScore]
         ↓
    Strategy Registry: aktiviert/deaktiviert Strategien
         ↓
    Strategy Engine: handelt mit bester Strategie

Retraining-Strategie:
    - Wöchentliches Retraining auf rolling 6-Monats-Fenster
    - Shadow Mode: neue Modell-Predictions werden geloggt aber nicht gehandelt
    - A/B Test: alte vs. neue Predictions für 2 Wochen vergleichen
    - Manueller Approve: Mensch bestätigt Rollout

Model Drift Detection:
    - Prediction Accuracy täglich überwacht
    - Wenn Accuracy < 55% für 3 Tage → Alert
    - Kein automatischer Rollback (manueller Eingriff nötig)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sgr.core.logging import get_logger
from sgr.market_data.feature_store import FeatureStore
from sgr.market_data.types import FeatureSet
from sgr.ml.regime_detector import RegimeDetector
from sgr.ml.strategy_selector import StrategySelector
from sgr.ml.types import RegimePrediction, VolatilityForecast
from sgr.ml.volatility_forecaster import VolatilityForecaster
from sgr.strategy.registry import StrategyRegistry

log = get_logger(__name__)


class MLEngine:
    """
    Zentrale ML-Engine. Koordiniert alle ML-Komponenten.

    Lifecycle:
        engine = MLEngine()
        await engine.initialize()     # Lädt oder trainiert Modelle
        result = await engine.run(features)  # Vollständige ML-Pipeline
    """

    def __init__(
        self,
        feature_store: FeatureStore | None = None,
        registry: StrategyRegistry | None = None,
    ) -> None:
        self._feature_store = feature_store
        self._registry = registry or StrategyRegistry.get()
        self._regime_detector = RegimeDetector()
        self._vol_forecaster = VolatilityForecaster()
        self._strategy_selector: StrategySelector | None = None
        self._initialized = False
        self._prediction_log: list[dict[str, Any]] = []

    async def initialize(
        self,
        model_path: str | None = None,
        training_features: list[FeatureSet] | None = None,
    ) -> None:
        """
        Initialisiert ML-Engine.
        Lädt gespeicherte Modelle oder trainiert neu.

        Args:
            model_path: Pfad zu gespeicherten Modellen (None = neu trainieren)
            training_features: Historische Features für Training
        """
        strategy_names = list(self._registry.get_all().keys())

        if model_path:
            from pathlib import Path

            p = Path(model_path)
            try:
                self._regime_detector.load(p / "regime")
                log.info("ml_engine.models_loaded", path=model_path)
            except Exception as e:
                log.warning("ml_engine.load_failed", error=str(e), note="Will train from scratch")

        if not self._regime_detector.is_trained and training_features:
            await self._train_all(training_features, strategy_names)
        elif not self._regime_detector.is_trained:
            log.warning(
                "ml_engine.no_training_data",
                note="Using rule-based fallback for all predictions",
            )

        self._strategy_selector = StrategySelector(strategy_names)
        self._initialized = True

        log.info(
            "ml_engine.initialized",
            regime_detector=self._regime_detector.is_trained,
            vol_forecaster=self._vol_forecaster.is_fitted,
        )

    async def _train_all(
        self,
        features: list[FeatureSet],
        strategy_names: list[str],
    ) -> None:
        """Trainiert alle Modelle auf historischen Daten."""
        log.info("ml_engine.training_started", samples=len(features))

        # 1. Regime Detector
        try:
            metrics = self._regime_detector.train(features)
            log.info("ml_engine.regime_detector_trained", **metrics)
        except Exception as e:
            log.error("ml_engine.regime_detector_train_failed", error=str(e))

        # 2. Volatility Forecaster (auf Close-Returns)
        try:
            import numpy as np

            closes = np.array([float(fs.close) for fs in features])
            if len(closes) > 30:
                log_returns = np.diff(np.log(closes))
                self._vol_forecaster.fit(log_returns)
                log.info("ml_engine.vol_forecaster_trained")
        except Exception as e:
            log.error("ml_engine.vol_forecaster_train_failed", error=str(e))

        log.info("ml_engine.training_complete")

    # ------------------------------------------------------------------
    # Inference Pipeline
    # ------------------------------------------------------------------

    async def run(
        self,
        features: FeatureSet,
        symbol: str | None = None,
        timeframe: str = "1h",
    ) -> dict[str, Any]:
        """
        Vollständige ML-Pipeline für ein FeatureSet.

        Returns:
            dict mit regime_prediction, vol_forecast, strategy_scores
        """
        # 1. Regime Detection
        regime_pred = self._regime_detector.predict(features)

        # 2. Volatility Forecast
        vol_forecast = self._vol_forecaster.predict(
            symbol=symbol or str(features.symbol),
            timeframe=timeframe,
            horizon_bars=5,
        )

        # 3. Strategy Selection (falls verfügbar)
        strategy_scores = []
        if self._strategy_selector and self._strategy_selector.is_fitted:
            strategy_scores = self._strategy_selector.select(features, regime_pred.regime)

        # 4. Registry Update: Strategien basierend auf ML-Empfehlung
        await self._update_registry(regime_pred, strategy_scores)

        # 5. Prediction loggen (für Drift Detection)
        self._log_prediction(features, regime_pred, vol_forecast)

        result = {
            "regime": regime_pred.regime.value,
            "regime_confidence": regime_pred.confidence,
            "regime_probabilities": regime_pred.probabilities,
            "top_regime_features": [
                {"feature": k, "importance": v} for k, v in regime_pred.top_features[:5]
            ],
            "volatility_pct": vol_forecast.predicted_volatility_pct,
            "volatility_regime": vol_forecast.volatility_regime,
            "volatility_ci": {
                "lower": vol_forecast.lower_bound_pct,
                "upper": vol_forecast.upper_bound_pct,
            },
            "strategy_scores": [
                {
                    "strategy": s.strategy_name,
                    "score": s.score,
                    "regime_fit": s.regime_fit,
                    "recommended": s.recommended,
                }
                for s in strategy_scores[:3]
            ],
        }

        log.debug(
            "ml_engine.inference_complete",
            regime=regime_pred.regime.value,
            confidence=f"{regime_pred.confidence:.2%}",
            vol_pct=f"{vol_forecast.predicted_volatility_pct:.2f}%",
        )

        return result

    async def _update_registry(
        self,
        regime_pred: RegimePrediction,
        strategy_scores: list,
    ) -> None:
        """
        Aktualisiert Strategy Registry basierend auf ML-Empfehlung.
        Nur wenn Konfidenz hoch genug.
        """
        if not regime_pred.is_high_confidence:
            return

        for score in strategy_scores:
            entry = self._registry.get_entry(score.strategy_name)
            if entry is None:
                continue

            if score.recommended and not entry.is_active:
                # Nur aktivieren wenn validiert
                if entry.is_validated:
                    await self._registry.activate(score.strategy_name)
                    log.info(
                        "ml_engine.strategy_activated",
                        strategy=score.strategy_name,
                        score=score.score,
                    )
            elif not score.recommended and entry.is_active:
                await self._registry.deactivate(
                    score.strategy_name,
                    reason=f"ML Score {score.score:.2f} below threshold",
                )

    def _log_prediction(
        self,
        features: FeatureSet,
        regime_pred: RegimePrediction,
        vol_forecast: VolatilityForecast,
    ) -> None:
        """Loggt Vorhersage für spätere Accuracy-Auswertung."""
        self._prediction_log.append(
            {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "symbol": str(features.symbol),
                "predicted_regime": regime_pred.regime.value,
                "regime_confidence": regime_pred.confidence,
                "predicted_vol_pct": vol_forecast.predicted_volatility_pct,
                "actual_regime": None,  # Wird später befüllt
                "actual_vol_pct": None,
            }
        )
        # Rolling Window: max 10.000 Logs in Memory
        if len(self._prediction_log) > 10_000:
            self._prediction_log = self._prediction_log[-10_000:]

    def get_prediction_accuracy(self) -> dict[str, float]:
        """
        Berechnet retrospektive Vorhersage-Genauigkeit.
        Nur für Logs mit bekanntem Actual-Outcome.
        """
        labeled = [p for p in self._prediction_log if p["actual_regime"] is not None]
        if not labeled:
            return {"regime_accuracy": 0.0, "sample_size": 0}

        correct = sum(1 for p in labeled if p["predicted_regime"] == p["actual_regime"])
        return {
            "regime_accuracy": correct / len(labeled),
            "sample_size": len(labeled),
        }

    @property
    def is_initialized(self) -> bool:
        return self._initialized
