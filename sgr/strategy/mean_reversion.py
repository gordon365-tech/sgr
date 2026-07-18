"""
SGR Mean Reversion Strategy
============================
Referenz-Implementierung einer Mean-Reversion-Strategie.

Logik:
    Kauft überverkaufte Zustände, verkauft überkaufte Zustände.
    Funktioniert in Range-Märkten (RANGING Regime).

    Long Entry (Oversold Bounce):
        - Regime: RANGING
        - RSI(14) < 35 (überverkauft)
        - Preis < BB Lower Band (statistisch günstig)
        - BB Squeeze erkannt (Volatilität komprimiert)
        - Orderbook Imbalance > 0 (Bid-Druck vorhanden)
        - MACD Histogram dreht (Momentum-Shift)

    Short Entry (Overbought Reversal):
        - Inverse der Long-Bedingungen

    Exit-Kriterium (als Signal-Metadaten):
        - Target: BB Middle (SMA20) = Mean
        - Stop: 1.5x ATR unter Entry

Bekannte Stärken:
    - Hohe Hit-Rate in Range-Märkten (> 60%)
    - Klar definierte Entry/Exit-Level

Bekannte Schwächen:
    - Katastrophal in Trend-Märkten (Strategie kämpft gegen Trend)
    - Regime-Detection muss zuverlässig sein
    - „Catching falling knife" Risiko ohne Stop
"""

from __future__ import annotations

from dataclasses import dataclass

from sgr.core.types import MarketRegime, SignalDirection
from sgr.market_data.types import MarketContext
from sgr.strategy.base import BaseStrategy, Signal, StrategyParameters
from sgr.strategy.registry import StrategyRegistry


@dataclass
class MeanReversionParams:
    rsi_oversold: float = 35.0  # RSI unter diesem Wert = Oversold
    rsi_overbought: float = 65.0  # RSI über diesem Wert = Overbought
    bb_position_long: float = 0.15  # BB Position unter diesem Level = Long
    bb_position_short: float = 0.85  # BB Position über diesem Level = Short
    min_confidence: float = 0.55
    require_imbalance: bool = True  # Orderbook Imbalance bestätigung
    require_macd_turn: bool = True  # MACD Histogram Richtungswechsel


@StrategyRegistry.register
class MeanReversionStrategy(BaseStrategy):
    """
    Mean Reversion für Range-Märkte.
    Handelt Rückkehr zum Mittelwert (SMA20 / BB Middle).
    """

    name = "mean_reversion_v1"
    version = "1.0.0"
    supported_regimes = [MarketRegime.RANGING]

    def __init__(self, params: MeanReversionParams | None = None) -> None:
        self._params = params or MeanReversionParams()

    def generate_signal(self, context: MarketContext) -> Signal | None:
        if not self.validate_context(context):
            return None

        if context.regime != MarketRegime.RANGING:
            return None

        ind = context.primary.indicators
        close = float(context.primary.close)

        # Long: Oversold Bounce
        long_score, long_max = self._score_long(ind, close, context)
        short_score, short_max = self._score_short(ind, close, context)

        # Wähle die bessere Seite
        long_conf = long_score / long_max if long_max > 0 else 0.0
        short_conf = short_score / short_max if short_max > 0 else 0.0

        if long_conf > short_conf and long_conf >= self._params.min_confidence:
            target_price = float(ind.bb_middle) if ind.bb_middle else None
            stop_distance = float(ind.atr_14) * 1.5 if ind.atr_14 else None
            stop_price = (close - stop_distance) if stop_distance else None

            return self._signal(
                context=context,
                direction=SignalDirection.LONG,
                confidence=min(long_conf, 1.0),
                metadata={
                    "score": round(long_score, 2),
                    "rsi_14": ind.rsi_14,
                    "bb_position": ind.bb_position,
                    "target_price": round(target_price, 2) if target_price else None,
                    "stop_price": round(stop_price, 2) if stop_price else None,
                },
            )

        if short_conf >= self._params.min_confidence and short_conf > long_conf:
            target_price = float(ind.bb_middle) if ind.bb_middle else None
            stop_distance = float(ind.atr_14) * 1.5 if ind.atr_14 else None
            stop_price = (close + stop_distance) if stop_distance else None

            return self._signal(
                context=context,
                direction=SignalDirection.SHORT,
                confidence=min(short_conf, 1.0),
                metadata={
                    "score": round(short_score, 2),
                    "rsi_14": ind.rsi_14,
                    "bb_position": ind.bb_position,
                    "target_price": round(target_price, 2) if target_price else None,
                    "stop_price": round(stop_price, 2) if stop_price else None,
                },
            )

        return None

    def _score_long(
        self,
        ind: object,
        close: float,
        context: MarketContext,
    ) -> tuple[float, float]:
        from sgr.market_data.types import IndicatorValues

        assert isinstance(ind, IndicatorValues)
        score, max_score = 0.0, 0.0

        # 1. RSI Oversold (Gewichtung: 2.5)
        max_score += 2.5
        if ind.rsi_14 is not None:
            if ind.rsi_14 < self._params.rsi_oversold:
                score += 2.5
            elif ind.rsi_14 < 40:
                score += 1.0

        # 2. Bollinger Lower Touch (Gewichtung: 2)
        max_score += 2.0
        if ind.bb_position is not None:
            if ind.bb_position < self._params.bb_position_long:
                score += 2.0
            elif ind.bb_position < 0.25:
                score += 1.0

        # 3. MACD Histogram dreht (negativ → weniger negativ) (Gewichtung: 1.5)
        max_score += 1.5
        if ind.macd_histogram is not None and ind.macd_histogram < 0:
            # Zeigt Momentum-Abschwächung im Downtrend
            score += 1.5

        # 4. Orderbook Imbalance (Gewichtung: 1)
        max_score += 1.0
        ob = context.primary.orderbook
        if ob and ob.order_imbalance_5 > 0.1:
            score += 1.0
        elif not self._params.require_imbalance:
            score += 0.5  # Partial credit wenn nicht erzwungen

        # 5. BB Width (Squeeze erkannt) (Gewichtung: 1)
        max_score += 1.0
        if ind.bb_width is not None and ind.bb_width < 0.04:
            score += 1.0  # Enge Bänder → Breakout wahrscheinlich

        # 6. ADX niedrig (Range bestätigt) (Gewichtung: 0.5)
        max_score += 0.5
        if ind.adx_14 is not None and ind.adx_14 < 20:
            score += 0.5

        return score, max_score

    def _score_short(
        self,
        ind: object,
        close: float,
        context: MarketContext,
    ) -> tuple[float, float]:
        from sgr.market_data.types import IndicatorValues

        assert isinstance(ind, IndicatorValues)
        score, max_score = 0.0, 0.0

        # 1. RSI Overbought
        max_score += 2.5
        if ind.rsi_14 is not None:
            if ind.rsi_14 > self._params.rsi_overbought:
                score += 2.5
            elif ind.rsi_14 > 60:
                score += 1.0

        # 2. Bollinger Upper Touch
        max_score += 2.0
        if ind.bb_position is not None:
            if ind.bb_position > self._params.bb_position_short:
                score += 2.0
            elif ind.bb_position > 0.75:
                score += 1.0

        # 3. MACD Histogram dreht (positiv → weniger positiv)
        max_score += 1.5
        if ind.macd_histogram is not None and ind.macd_histogram > 0:
            score += 1.5

        # 4. Orderbook Ask-Druck
        max_score += 1.0
        ob = context.primary.orderbook
        if ob and ob.order_imbalance_5 < -0.1:
            score += 1.0
        elif not self._params.require_imbalance:
            score += 0.5

        # 5. BB Squeeze
        max_score += 1.0
        if ind.bb_width is not None and ind.bb_width < 0.04:
            score += 1.0

        # 6. ADX niedrig
        max_score += 0.5
        if ind.adx_14 is not None and ind.adx_14 < 20:
            score += 0.5

        return score, max_score

    def validate_context(self, context: MarketContext) -> bool:
        ind = context.primary.indicators
        return (
            ind.rsi_14 is not None
            and ind.bb_position is not None
            and ind.atr_14 is not None
            and context.primary.close > 0
        )

    def get_parameters(self) -> StrategyParameters:
        return StrategyParameters(
            name=self.name,
            version=self.version,
            params={
                "rsi_oversold": self._params.rsi_oversold,
                "rsi_overbought": self._params.rsi_overbought,
                "bb_position_long": self._params.bb_position_long,
                "bb_position_short": self._params.bb_position_short,
                "min_confidence": self._params.min_confidence,
            },
        )
