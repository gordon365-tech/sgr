"""
SGR Trend Following Strategy
=============================
Referenz-Implementierung einer Trend-Following-Strategie.

Logik (regelbasiert, vollständig transparent):
    Long Entry wenn:
        - Regime: TRENDING_UP
        - RSI(14) > 50 (Momentum bestätigt Trend)
        - EMA9 > EMA21 > EMA50 (Alignment – alle EMAs bullish)
        - Price > VWAP (Preis über Tages-VWAP)
        - ADX(14) > 25 (Trendstärke ausreichend)
        - Volume Ratio > 1.0 (überdurchschnittliches Volumen)

    Short Entry wenn:
        - Regime: TRENDING_DOWN
        - Inverse der obigen Bedingungen

    Konfidenz-Berechnung:
        Jede erfüllte Bedingung addiert Punkte.
        Konfidenz = Summe / Max_Punkte (0.0 – 1.0)

Warum dieses Design?
    - Vollständig interpretierbar: jede Entscheidung ist nachvollziehbar
    - Regime-aware: handelt nur bei klarem Trend (ADX > 25)
    - Multi-Confirmation: kein einzelner Indikator entscheidet
    - Kein Look-Ahead: alle Features berechnet auf geschlossenen Bars

Bekannte Schwächen:
    - Schlechte Performance in Range-Märkten (dafür MeanReversion nutzen)
    - Trend-Ende oft zu spät erkannt
    - Hoher Drawdown bei falschen Trend-Breakouts
"""

from __future__ import annotations

from dataclasses import dataclass

from sgr.core.types import MarketRegime, SignalDirection
from sgr.market_data.types import MarketContext
from sgr.strategy.base import BaseStrategy, Signal, StrategyParameters
from sgr.strategy.registry import StrategyRegistry


@dataclass
class TrendFollowingParams:
    rsi_min_long: float = 50.0  # RSI Minimum für Long-Entry
    rsi_max_short: float = 50.0  # RSI Maximum für Short-Entry
    adx_min: float = 25.0  # Mindest-ADX (Trendstärke)
    volume_ratio_min: float = 0.8  # Mindest-Volumen-Ratio
    min_confidence: float = 0.55  # Mindest-Konfidenz für Signal-Output
    ema_alignment_required: bool = True  # EMA9 > EMA21 erzwingen


@StrategyRegistry.register
class TrendFollowingStrategy(BaseStrategy):
    """
    Trend-Following Strategie. Nur für Trending-Regime.

    Unterstützte Regime:
        TRENDING_UP:   Long Signals
        TRENDING_DOWN: Short Signals
    """

    name = "trend_following_v1"
    version = "1.0.0"
    supported_regimes = [MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN]

    def __init__(self, params: TrendFollowingParams | None = None) -> None:
        self._params = params or TrendFollowingParams()

    def generate_signal(self, context: MarketContext) -> Signal | None:
        if not self.validate_context(context):
            return None

        regime = context.regime
        ind = context.primary.indicators
        close = float(context.primary.close)

        if regime == MarketRegime.TRENDING_UP:
            return self._evaluate_long(context, ind, close)
        elif regime == MarketRegime.TRENDING_DOWN:
            return self._evaluate_short(context, ind, close)

        return None

    def _evaluate_long(self, context: MarketContext, ind: object, close: float) -> Signal | None:
        """Bewertet Long-Entry Bedingungen. Gibt Konfidenz-Score zurück."""
        from sgr.market_data.types import IndicatorValues

        assert isinstance(ind, IndicatorValues)

        score = 0.0
        max_score = 0.0

        # 1. RSI Momentum (Gewichtung: 2)
        max_score += 2.0
        if ind.rsi_14 is not None and ind.rsi_14 > self._params.rsi_min_long:
            score += 2.0
            if ind.rsi_14 > 60:
                score += 0.5  # Bonus für starkes Momentum

        # 2. EMA Alignment: EMA9 > EMA21 (Gewichtung: 2)
        max_score += 2.0
        if ind.ema_9 and ind.ema_21 and float(ind.ema_9) > float(ind.ema_21):
            score += 2.0
            # Bonus: EMA21 > EMA50
            if ind.ema_50 and float(ind.ema_21) > float(ind.ema_50):
                score += 0.5

        # 3. ADX Trendstärke (Gewichtung: 1.5)
        max_score += 1.5
        if ind.adx_14 is not None:
            if ind.adx_14 >= self._params.adx_min:
                score += 1.5
            elif ind.adx_14 >= 20:
                score += 0.75  # Partial credit

        # 4. Preis über VWAP (Gewichtung: 1)
        max_score += 1.0
        if ind.vwap and close > float(ind.vwap):
            score += 1.0

        # 5. Volumen (Gewichtung: 1)
        max_score += 1.0
        if ind.volume_ratio is not None and ind.volume_ratio >= self._params.volume_ratio_min:
            score += 1.0

        # 6. Bollinger Position (optional, Gewichtung: 0.5)
        max_score += 0.5
        if ind.bb_position is not None and 0.4 < ind.bb_position < 0.85:
            score += 0.5  # Preis im gesunden Bereich (nicht überkauft)

        # 7. DI+ > DI- (Gewichtung: 1)
        max_score += 1.0
        if ind.di_plus is not None and ind.di_minus is not None:
            if ind.di_plus > ind.di_minus:
                score += 1.0

        confidence = score / max_score if max_score > 0 else 0.0

        if confidence < self._params.min_confidence:
            return None

        return self._signal(
            context=context,
            direction=SignalDirection.LONG,
            confidence=min(confidence, 1.0),
            metadata={
                "score": round(score, 2),
                "max_score": round(max_score, 2),
                "rsi_14": ind.rsi_14,
                "adx_14": ind.adx_14,
                "ema_alignment": ind.ema_9 and ind.ema_21 and float(ind.ema_9) > float(ind.ema_21),
                "volume_ratio": ind.volume_ratio,
            },
        )

    def _evaluate_short(self, context: MarketContext, ind: object, close: float) -> Signal | None:
        """Bewertet Short-Entry Bedingungen. Spiegelbild von Long."""
        from sgr.market_data.types import IndicatorValues

        assert isinstance(ind, IndicatorValues)

        score = 0.0
        max_score = 0.0

        # 1. RSI Momentum (unter 50)
        max_score += 2.0
        if ind.rsi_14 is not None and ind.rsi_14 < self._params.rsi_max_short:
            score += 2.0
            if ind.rsi_14 < 40:
                score += 0.5

        # 2. EMA Alignment: EMA9 < EMA21
        max_score += 2.0
        if ind.ema_9 and ind.ema_21 and float(ind.ema_9) < float(ind.ema_21):
            score += 2.0
            if ind.ema_50 and float(ind.ema_21) < float(ind.ema_50):
                score += 0.5

        # 3. ADX Trendstärke
        max_score += 1.5
        if ind.adx_14 is not None:
            if ind.adx_14 >= self._params.adx_min:
                score += 1.5
            elif ind.adx_14 >= 20:
                score += 0.75

        # 4. Preis unter VWAP
        max_score += 1.0
        if ind.vwap and close < float(ind.vwap):
            score += 1.0

        # 5. Volumen
        max_score += 1.0
        if ind.volume_ratio is not None and ind.volume_ratio >= self._params.volume_ratio_min:
            score += 1.0

        # 6. BB Position (nicht überverkauft)
        max_score += 0.5
        if ind.bb_position is not None and 0.15 < ind.bb_position < 0.6:
            score += 0.5

        # 7. DI- > DI+
        max_score += 1.0
        if ind.di_plus is not None and ind.di_minus is not None:
            if ind.di_minus > ind.di_plus:
                score += 1.0

        confidence = score / max_score if max_score > 0 else 0.0

        if confidence < self._params.min_confidence:
            return None

        return self._signal(
            context=context,
            direction=SignalDirection.SHORT,
            confidence=min(confidence, 1.0),
            metadata={
                "score": round(score, 2),
                "rsi_14": ind.rsi_14,
                "adx_14": ind.adx_14,
            },
        )

    def validate_context(self, context: MarketContext) -> bool:
        ind = context.primary.indicators
        return (
            ind.rsi_14 is not None
            and ind.adx_14 is not None
            and ind.ema_9 is not None
            and ind.ema_21 is not None
            and context.primary.close > 0
        )

    def get_parameters(self) -> StrategyParameters:
        return StrategyParameters(
            name=self.name,
            version=self.version,
            params={
                "rsi_min_long": self._params.rsi_min_long,
                "rsi_max_short": self._params.rsi_max_short,
                "adx_min": self._params.adx_min,
                "volume_ratio_min": self._params.volume_ratio_min,
                "min_confidence": self._params.min_confidence,
            },
        )
