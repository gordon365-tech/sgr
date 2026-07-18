"""
SGR ML Engine – Domain Types
=============================
Alle Types für ML-Komponenten.

ML-Anwendungen in SGR (nur explainierbare Modelle):
    1. Regime Detection     → HMM + Random Forest + SHAP
    2. Volatility Forecast  → GARCH + LSTM + Confidence Intervals
    3. Strategy Selection   → Gradient Boosting + SHAP
    4. Position Sizing      → Kelly Criterion (regelbasiert, kein Black-Box)

Verboten: End-to-End Blackbox ohne Explainability.
Erlaubt:  Jede Vorhersage muss mit Feature Importance begründbar sein.

Model Lifecycle:
    TRAINING → VALIDATION → SHADOW (Paper Trading) → PRODUCTION
    Kein automatischer Rollout – manueller Approve-Step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from sgr.core.types import MarketRegime


class ModelStatus(StrEnum):
    UNTRAINED = "untrained"
    TRAINING = "training"
    VALIDATING = "validating"
    SHADOW = "shadow"  # Paper Trading: Predictions geloggt aber nicht gehandelt
    PRODUCTION = "production"
    DEPRECATED = "deprecated"
    FAILED = "failed"


class ModelType(StrEnum):
    REGIME_DETECTOR = "regime_detector"
    VOLATILITY_FORECASTER = "volatility_forecaster"
    STRATEGY_SELECTOR = "strategy_selector"


@dataclass(frozen=True)
class RegimePrediction:
    """
    Ergebnis der Regime Detection.
    Enthält Regime + Konfidenz + Feature-Beitrag (Explainability).
    """

    regime: MarketRegime
    confidence: float  # 0.0–1.0
    probabilities: dict[str, float]  # P(regime) für jedes Regime
    feature_importance: dict[str, float]  # Welche Features am stärksten?
    model_version: str
    predicted_at: datetime

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.70

    @property
    def top_features(self) -> list[tuple[str, float]]:
        """Top-5 Features nach Wichtigkeit."""
        sorted_features = sorted(
            self.feature_importance.items(),
            key=lambda x: abs(x[1]),
            reverse=True,
        )
        return sorted_features[:5]


@dataclass(frozen=True)
class VolatilityForecast:
    """
    Volatilitätsprognose für die nächsten N Bars.
    GARCH-Modell mit Konfidenz-Intervallen.
    """

    symbol: str
    timeframe: str
    horizon_bars: int
    predicted_volatility_pct: float  # Erwartete Volatilität (als %)
    lower_bound_pct: float  # 95% CI untere Grenze
    upper_bound_pct: float  # 95% CI obere Grenze
    garch_alpha: float  # GARCH(1,1) Parameter α
    garch_beta: float  # GARCH(1,1) Parameter β
    model_version: str
    predicted_at: datetime

    @property
    def is_high_volatility(self) -> bool:
        return self.predicted_volatility_pct > 3.0  # > 3% als hoch

    @property
    def volatility_regime(self) -> str:
        if self.predicted_volatility_pct < 1.0:
            return "low"
        elif self.predicted_volatility_pct < 3.0:
            return "medium"
        else:
            return "high"


@dataclass(frozen=True)
class StrategyScore:
    """Score einer Strategie für aktuelles Regime + Features."""

    strategy_name: str
    score: float  # 0.0–1.0
    expected_sharpe: float  # Erwarteter Sharpe basierend auf historischer Performance
    regime_fit: float  # Wie gut passt Strategie zu aktuellem Regime?
    feature_alignment: dict[str, float]  # Feature → Contribution zum Score
    recommended: bool  # Vom ML empfohlen?


@dataclass
class ModelMetadata:
    """Metadata eines trainierten ML-Modells."""

    model_id: str
    model_type: ModelType
    version: str
    status: ModelStatus
    trained_at: datetime | None = None
    training_samples: int = 0
    validation_accuracy: float = 0.0
    validation_details: dict[str, Any] = field(default_factory=dict)
    feature_names: list[str] = field(default_factory=list)
    hyperparameters: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def is_ready(self) -> bool:
        return self.status in (ModelStatus.SHADOW, ModelStatus.PRODUCTION)


class MLPredictionLog(BaseModel):
    """Log-Entry für jede ML-Vorhersage (für Monitoring + Drift Detection)."""

    model_id: str
    model_type: str
    prediction: dict[str, Any]
    features_used: dict[str, float]
    actual_outcome: dict[str, Any] | None = None  # Wird später befüllt
    predicted_at: datetime
    symbol: str
    timeframe: str
