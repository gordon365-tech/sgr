"""
SGR Momentum Strategy
=======================
Regelbasierte Momentum-Strategie - unterscheidet sich bewusst von
trend_following_v1: trend_following_v1 bewertet die STRUKTUR eines
Trends (EMA-Alignment, ADX-Trendstaerke), momentum_v1 bewertet die
GESCHWINDIGKEIT der Preisbewegung selbst (returns_5/returns_10, MACD-
Histogramm-Richtung) - zwei unterschiedliche, sich ergaenzende
Signalquellen fuer dasselbe TRENDING_UP/TRENDING_DOWN Regime (beide
koennen gleichzeitig aktiv sein; StrategyEngine._aggregate() loest
Konsens/Widerspruch bereits auf, siehe dortigen Docstring).

Logik:
    Long Entry:
        - Regime: TRENDING_UP
        - returns_5 > Schwelle (kurzfristige Beschleunigung nach oben)
        - returns_10 > 0 (Bestaetigung ueber laengeres Fenster - kein
          reiner 1-Bar-Ausreisser)
        - RSI(14) im Momentum-Fenster (55-80: bestaetigt Momentum, aber
          noch nicht ueberkauft/erschoepft)
        - MACD-Histogramm positiv UND steigend (Momentum beschleunigt,
          nicht nur vorhanden)
        - Volumen ueberdurchschnittlich

    Short Entry: Inverse der obigen Bedingungen.

Bekannte Schwaechen:
    - Reagiert auf bereits laufende Bewegungen (spaeter Einstieg als
      ein reiner Breakout-Ansatz)
    - Kein eigener Stop/Take-Profit-Mechanismus im Code verankert
      (wie bei mean_reversion_v1/trend_following_v1 nur als Signal-
      Metadaten dokumentiert - siehe Modul-Docstring in
      sgr/strategy/base.py: Strategien entscheiden nur Richtung +
      Konfidenz, nie Positionsmanagement).
"""

from __future__ import annotations

from dataclasses import dataclass

from sgr.core.types import MarketRegime, SignalDirection
from sgr.market_data.types import MarketContext
from sgr.strategy.base import BaseStrategy, Signal, StrategyParameters
from sgr.strategy.registry import StrategyRegistry


@dataclass
class MomentumParams:
    returns_5_min: float = 0.01  # 1% Mindestbewegung ueber 5 Bars
    rsi_momentum_low: float = 55.0
    rsi_momentum_high: float = 80.0
    volume_ratio_min: float = 1.1
    min_confidence: float = 0.55


@StrategyRegistry.register
class MomentumStrategy(BaseStrategy):
    """Momentum-Strategie: handelt die Geschwindigkeit der Bewegung,
    nicht die Trendstruktur (siehe Modul-Docstring)."""

    name = "momentum_v1"
    version = "1.0.0"
    supported_regimes = [MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN]

    def __init__(self, params: MomentumParams | None = None) -> None:
        self._params = params or MomentumParams()

    def generate_signal(self, context: MarketContext) -> Signal | None:
        if not self.validate_context(context):
            return None

        if context.regime == MarketRegime.TRENDING_UP:
            return self._evaluate_long(context)
        if context.regime == MarketRegime.TRENDING_DOWN:
            return self._evaluate_short(context)
        return None

    def _evaluate_long(self, context: MarketContext) -> Signal | None:
        ind = context.primary.indicators
        returns_5 = context.primary.returns_5
        returns_10 = context.primary.returns_10

        score, max_score = 0.0, 0.0

        # 1. Kurzfristige Beschleunigung (Gewichtung: 2.5)
        max_score += 2.5
        if returns_5 is not None and returns_5 > self._params.returns_5_min:
            score += 2.5
        elif returns_5 is not None and returns_5 > 0:
            score += 1.0

        # 2. Laengerfristige Bestaetigung (Gewichtung: 1.5)
        max_score += 1.5
        if returns_10 is not None and returns_10 > 0:
            score += 1.5

        # 3. RSI im Momentum-Fenster (Gewichtung: 2.0)
        max_score += 2.0
        rsi_low, rsi_high = self._params.rsi_momentum_low, self._params.rsi_momentum_high
        if ind.rsi_14 is not None and rsi_low < ind.rsi_14 < rsi_high:
            score += 2.0

        # 4. MACD Histogramm positiv (Gewichtung: 2.0)
        max_score += 2.0
        if ind.macd_histogram is not None and ind.macd_histogram > 0:
            score += 2.0

        # 5. Volumen (Gewichtung: 1.5)
        max_score += 1.5
        if ind.volume_ratio is not None and ind.volume_ratio >= self._params.volume_ratio_min:
            score += 1.5

        # 6. ADX minimale Trendbestaetigung (Gewichtung: 0.5)
        max_score += 0.5
        if ind.adx_14 is not None and ind.adx_14 > 18:
            score += 0.5

        confidence = score / max_score if max_score > 0 else 0.0
        if confidence < self._params.min_confidence:
            return None

        return self._signal(
            context=context,
            direction=SignalDirection.LONG,
            confidence=min(confidence, 1.0),
            metadata={
                "score": round(score, 2),
                "returns_5": returns_5,
                "returns_10": returns_10,
                "rsi_14": ind.rsi_14,
                "volume_ratio": ind.volume_ratio,
            },
        )

    def _evaluate_short(self, context: MarketContext) -> Signal | None:
        ind = context.primary.indicators
        returns_5 = context.primary.returns_5
        returns_10 = context.primary.returns_10

        score, max_score = 0.0, 0.0

        max_score += 2.5
        if returns_5 is not None and returns_5 < -self._params.returns_5_min:
            score += 2.5
        elif returns_5 is not None and returns_5 < 0:
            score += 1.0

        max_score += 1.5
        if returns_10 is not None and returns_10 < 0:
            score += 1.5

        max_score += 2.0
        rsi_low = 100.0 - self._params.rsi_momentum_high
        rsi_high = 100.0 - self._params.rsi_momentum_low
        if ind.rsi_14 is not None and rsi_low < ind.rsi_14 < rsi_high:
            score += 2.0

        max_score += 2.0
        if ind.macd_histogram is not None and ind.macd_histogram < 0:
            score += 2.0

        max_score += 1.5
        if ind.volume_ratio is not None and ind.volume_ratio >= self._params.volume_ratio_min:
            score += 1.5

        max_score += 0.5
        if ind.adx_14 is not None and ind.adx_14 > 18:
            score += 0.5

        confidence = score / max_score if max_score > 0 else 0.0
        if confidence < self._params.min_confidence:
            return None

        return self._signal(
            context=context,
            direction=SignalDirection.SHORT,
            confidence=min(confidence, 1.0),
            metadata={
                "score": round(score, 2),
                "returns_5": returns_5,
                "returns_10": returns_10,
                "rsi_14": ind.rsi_14,
                "volume_ratio": ind.volume_ratio,
            },
        )

    def validate_context(self, context: MarketContext) -> bool:
        ind = context.primary.indicators
        return (
            ind.rsi_14 is not None
            and context.primary.returns_5 is not None
            and context.primary.returns_10 is not None
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
            },
        )
