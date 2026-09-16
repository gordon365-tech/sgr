"""
SGR Volatility-Adjusted Momentum Strategy
===========================================
Fuer das HIGH_VOLATILITY-Regime (siehe sgr/strategy/regime_classifier.py
"regime_detector_v1": atr_pct ueber der HIGH_VOLATILITY-Schwelle).

Task-Vorgabe fuer HIGH_VOLATILITY: "Volatility Adjusted Strategy oder
kein Trade, wenn Risk Limits ueberschritten werden". Umsetzung:
    1. Deutlich straffere Entry-Schwellen als momentum_v1 (RSI/Returns/
       Volumen) - in hoher Volatilitaet erzeugen "normale" Momentum-
       Schwellen zu viele Fehlsignale (Rauschen wird mit echtem
       Momentum verwechselt).
    2. Die resultierende Konfidenz wird explizit durch einen
       Volatilitaets-Faktor geteilt (staerker gedaempft, je weiter
       atr_pct ueber der Regime-Schwelle liegt). PositionSizer (siehe
       sgr/risk/position_sizer.py) skaliert die Positionsgroesse bereits
       proportional zur Signal-Konfidenz - eine niedrigere Konfidenz
       fuehrt dadurch automatisch zu einer kleineren Position, ohne dass
       diese Strategie selbst Positionsgroessen berechnen muss (verboten
       laut TradingStrategy-Protokoll, siehe sgr/strategy/base.py).
       "Kein Trade, wenn Risk Limits ueberschritten werden" bleibt
       Aufgabe der Risk Engine (Hard/Soft-Limits, Position auf 0
       reduziert) - diese Strategie erzwingt das nicht selbst.

Long/Short-Entry-Bedingungen: wie momentum_v1 (returns_5/10, RSI-Fenster,
MACD-Histogramm, Volumen), aber mit hoeheren Mindestschwellen (siehe
VolAdjustedMomentumParams) und der zusaetzlichen Vola-Daempfung oben.
"""

from __future__ import annotations

from dataclasses import dataclass

from sgr.core.types import MarketRegime, SignalDirection
from sgr.market_data.types import MarketContext
from sgr.strategy.base import BaseStrategy, Signal, StrategyParameters
from sgr.strategy.regime_classifier import HIGH_VOLATILITY_ATR_PCT
from sgr.strategy.registry import StrategyRegistry


@dataclass
class VolAdjustedMomentumParams:
    returns_5_min: float = 0.02  # hoehere Schwelle als momentum_v1 (0.01)
    rsi_momentum_low: float = 58.0
    rsi_momentum_high: float = 78.0
    volume_ratio_min: float = 1.3
    min_confidence: float = 0.55
    # Referenz-ATR%, ab der die Vola-Daempfung greift (identisch zur
    # Regime-Schwelle - unterhalb dieser Schwelle waere das Regime gar
    # nicht erst HIGH_VOLATILITY gewesen).
    atr_pct_reference: float = HIGH_VOLATILITY_ATR_PCT


@StrategyRegistry.register
class VolatilityAdjustedMomentumStrategy(BaseStrategy):
    """Momentum-Strategie mit expliziter Volatilitaets-Daempfung der
    Konfidenz fuer das HIGH_VOLATILITY-Regime."""

    name = "volatility_adjusted_momentum_v1"
    version = "1.0.0"
    supported_regimes = [MarketRegime.HIGH_VOLATILITY]

    def __init__(self, params: VolAdjustedMomentumParams | None = None) -> None:
        self._params = params or VolAdjustedMomentumParams()

    def generate_signal(self, context: MarketContext) -> Signal | None:
        if not self.validate_context(context):
            return None
        if context.regime != MarketRegime.HIGH_VOLATILITY:
            return None

        ind = context.primary.indicators
        returns_5 = context.primary.returns_5
        returns_10 = context.primary.returns_10
        atr_pct = ind.atr_pct or self._params.atr_pct_reference

        long_score, long_max = self._score(ind, returns_5, returns_10, long=True)
        short_score, short_max = self._score(ind, returns_5, returns_10, long=False)
        long_conf = self._dampen(long_score / long_max if long_max > 0 else 0.0, atr_pct)
        short_conf = self._dampen(short_score / short_max if short_max > 0 else 0.0, atr_pct)

        if long_conf >= self._params.min_confidence and long_conf > short_conf:
            return self._signal(
                context=context,
                direction=SignalDirection.LONG,
                confidence=min(long_conf, 1.0),
                metadata={
                    "score": round(long_score, 2),
                    "atr_pct": atr_pct,
                    "returns_5": returns_5,
                    "volume_ratio": ind.volume_ratio,
                    "volatility_damped": True,
                },
            )

        if short_conf >= self._params.min_confidence and short_conf > long_conf:
            return self._signal(
                context=context,
                direction=SignalDirection.SHORT,
                confidence=min(short_conf, 1.0),
                metadata={
                    "score": round(short_score, 2),
                    "atr_pct": atr_pct,
                    "returns_5": returns_5,
                    "volume_ratio": ind.volume_ratio,
                    "volatility_damped": True,
                },
            )

        return None

    def _dampen(self, confidence: float, atr_pct: float) -> float:
        """Teilt die rohe Konfidenz durch einen mit der Volatilitaet
        wachsenden Faktor - siehe Modul-Docstring Punkt 2."""
        excess = max(atr_pct - self._params.atr_pct_reference, 0.0)
        damping_factor = 1.0 + (excess / self._params.atr_pct_reference)
        return confidence / damping_factor

    def _score(
        self,
        ind: object,
        returns_5: float | None,
        returns_10: float | None,
        *,
        long: bool,
    ) -> tuple[float, float]:
        from sgr.market_data.types import IndicatorValues

        assert isinstance(ind, IndicatorValues)
        score, max_score = 0.0, 0.0
        sign = 1 if long else -1

        max_score += 2.5
        if returns_5 is not None and sign * returns_5 > self._params.returns_5_min:
            score += 2.5

        max_score += 1.5
        if returns_10 is not None and sign * returns_10 > 0:
            score += 1.5

        max_score += 2.0
        rsi_low = self._params.rsi_momentum_low if long else 100.0 - self._params.rsi_momentum_high
        rsi_high = self._params.rsi_momentum_high if long else 100.0 - self._params.rsi_momentum_low
        if ind.rsi_14 is not None and rsi_low < ind.rsi_14 < rsi_high:
            score += 2.0

        max_score += 2.0
        if ind.macd_histogram is not None and sign * ind.macd_histogram > 0:
            score += 2.0

        max_score += 2.0
        if ind.volume_ratio is not None and ind.volume_ratio >= self._params.volume_ratio_min:
            score += 2.0

        return score, max_score

    def validate_context(self, context: MarketContext) -> bool:
        ind = context.primary.indicators
        return (
            ind.rsi_14 is not None
            and ind.atr_pct is not None
            and context.primary.returns_5 is not None
            and context.primary.close > 0
        )

    def get_parameters(self) -> StrategyParameters:
        return StrategyParameters(
            name=self.name,
            version=self.version,
            params={
                "returns_5_min": self._params.returns_5_min,
                "rsi_momentum_low": self._params.rsi_momentum_low,
                "rsi_momentum_high": self._params.rsi_momentum_high,
                "volume_ratio_min": self._params.volume_ratio_min,
                "min_confidence": self._params.min_confidence,
                "atr_pct_reference": self._params.atr_pct_reference,
            },
        )
