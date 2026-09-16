"""
SGR Breakout Strategy
======================
Regelbasierte Breakout-Strategie fuer das BREAKOUT-Regime (siehe
sgr/strategy/regime_classifier.py "regime_detector_v1": Baender
expandiert + Preis am/ueber dem Rand).

Logik:
    Long Entry (Ausbruch nach oben):
        - Regime: BREAKOUT
        - Preis am/ueber oberem Bollinger-Band (bb_position hoch)
        - Bollinger-Baender expandieren (Gegenteil der "Squeeze"-
          Schwelle, die mean_reversion_v1 fuer RANGING nutzt)
        - Preis ueber dem oberen Keltner-Channel (klassisches
          "Squeeze-Breakout"-Signal - Baender waren vorher enger als
          Keltner, jetzt durchbrochen)
        - Ueberdurchschnittliches Volumen (bestaetigt echten Ausbruch,
          nicht nur Rauschen)
        - RSI momentum-bestaetigend, aber noch nicht extrem
          ueberkauft/-verkauft (>75/<25 gilt als Erschoepfungssignal,
          nicht als Fortsetzung)

    Short Entry: Inverse der obigen Bedingungen (Ausbruch nach unten).

Bekannte Schwaechen (wie jede Breakout-Strategie):
    - Anfaellig fuer False Breakouts (Preis kehrt sofort in die
      vorherige Range zurueck) - deshalb straffer Stop in den
      Signal-Metadaten (1.0x ATR statt der 1.5x bei mean_reversion_v1).
    - Funktioniert schlecht bei duennem Orderbuch/niedrigem Volumen -
      deshalb Volumen-Bestaetigung als eigenes, nicht optionales
      Gewichtungskriterium.
"""

from __future__ import annotations

from dataclasses import dataclass

from sgr.core.types import MarketRegime, SignalDirection
from sgr.market_data.types import MarketContext
from sgr.strategy.base import BaseStrategy, Signal, StrategyParameters
from sgr.strategy.registry import StrategyRegistry


@dataclass
class BreakoutParams:
    bb_position_breakout_high: float = 0.90
    bb_position_breakout_low: float = 0.10
    bb_width_expansion_min: float = 0.045  # identisch zur Regime-Klassifikation
    volume_ratio_min: float = 1.2
    rsi_exhaustion_high: float = 75.0
    rsi_exhaustion_low: float = 25.0
    min_confidence: float = 0.55


@StrategyRegistry.register
class BreakoutStrategy(BaseStrategy):
    """Breakout-Strategie fuer expandierende Baender / Range-Ausbrueche."""

    name = "breakout_v1"
    version = "1.0.0"
    supported_regimes = [MarketRegime.BREAKOUT]

    def __init__(self, params: BreakoutParams | None = None) -> None:
        self._params = params or BreakoutParams()

    def generate_signal(self, context: MarketContext) -> Signal | None:
        if not self.validate_context(context):
            return None
        if context.regime != MarketRegime.BREAKOUT:
            return None

        ind = context.primary.indicators
        close = float(context.primary.close)

        long_score, long_max = self._score_long(ind)
        short_score, short_max = self._score_short(ind)
        long_conf = long_score / long_max if long_max > 0 else 0.0
        short_conf = short_score / short_max if short_max > 0 else 0.0

        if long_conf >= self._params.min_confidence and long_conf > short_conf:
            stop_distance = float(ind.atr_14) if ind.atr_14 else None
            stop_price = (close - stop_distance) if stop_distance else None
            target_price = (close + stop_distance * 2) if stop_distance else None
            return self._signal(
                context=context,
                direction=SignalDirection.LONG,
                confidence=min(long_conf, 1.0),
                metadata={
                    "score": round(long_score, 2),
                    "bb_position": ind.bb_position,
                    "bb_width": ind.bb_width,
                    "volume_ratio": ind.volume_ratio,
                    "stop_price": round(stop_price, 2) if stop_price else None,
                    "target_price": round(target_price, 2) if target_price else None,
                },
            )

        if short_conf >= self._params.min_confidence and short_conf > long_conf:
            stop_distance = float(ind.atr_14) if ind.atr_14 else None
            stop_price = (close + stop_distance) if stop_distance else None
            target_price = (close - stop_distance * 2) if stop_distance else None
            return self._signal(
                context=context,
                direction=SignalDirection.SHORT,
                confidence=min(short_conf, 1.0),
                metadata={
                    "score": round(short_score, 2),
                    "bb_position": ind.bb_position,
                    "bb_width": ind.bb_width,
                    "volume_ratio": ind.volume_ratio,
                    "stop_price": round(stop_price, 2) if stop_price else None,
                    "target_price": round(target_price, 2) if target_price else None,
                },
            )

        return None

    def _score_long(self, ind: object) -> tuple[float, float]:
        from sgr.market_data.types import IndicatorValues

        assert isinstance(ind, IndicatorValues)
        score, max_score = 0.0, 0.0

        # 1. Preis am/ueber oberem Band (Gewichtung: 2.5)
        max_score += 2.5
        bb_high = self._params.bb_position_breakout_high
        if ind.bb_position is not None and ind.bb_position >= bb_high:
            score += 2.5

        # 2. Baender expandiert (Gewichtung: 2.0)
        max_score += 2.0
        if ind.bb_width is not None and ind.bb_width >= self._params.bb_width_expansion_min:
            score += 2.0

        # 3. Ueber Keltner-Oberband (klassisches Squeeze-Breakout) (1.5)
        max_score += 1.5
        if ind.kc_upper is not None and ind.bb_upper is not None:
            if float(ind.bb_upper) > float(ind.kc_upper):
                score += 1.5

        # 4. Volumen bestaetigt (Gewichtung: 1.5)
        max_score += 1.5
        if ind.volume_ratio is not None and ind.volume_ratio >= self._params.volume_ratio_min:
            score += 1.5

        # 5. RSI bestaetigt Fortsetzung, nicht Erschoepfung (Gewichtung: 1.0)
        max_score += 1.0
        if ind.rsi_14 is not None and 55.0 < ind.rsi_14 < self._params.rsi_exhaustion_high:
            score += 1.0

        # 6. ADX steigend/vorhanden (fruehe Trendbestaetigung) (0.5)
        max_score += 0.5
        if ind.adx_14 is not None and ind.adx_14 > 18:
            score += 0.5

        return score, max_score

    def _score_short(self, ind: object) -> tuple[float, float]:
        from sgr.market_data.types import IndicatorValues

        assert isinstance(ind, IndicatorValues)
        score, max_score = 0.0, 0.0

        max_score += 2.5
        if ind.bb_position is not None and ind.bb_position <= self._params.bb_position_breakout_low:
            score += 2.5

        max_score += 2.0
        if ind.bb_width is not None and ind.bb_width >= self._params.bb_width_expansion_min:
            score += 2.0

        max_score += 1.5
        if ind.kc_lower is not None and ind.bb_lower is not None:
            if float(ind.bb_lower) < float(ind.kc_lower):
                score += 1.5

        max_score += 1.5
        if ind.volume_ratio is not None and ind.volume_ratio >= self._params.volume_ratio_min:
            score += 1.5

        max_score += 1.0
        if ind.rsi_14 is not None and self._params.rsi_exhaustion_low < ind.rsi_14 < 45.0:
            score += 1.0

        max_score += 0.5
        if ind.adx_14 is not None and ind.adx_14 > 18:
            score += 0.5

        return score, max_score

    def validate_context(self, context: MarketContext) -> bool:
        ind = context.primary.indicators
        return (
            ind.bb_position is not None
            and ind.bb_width is not None
            and ind.atr_14 is not None
            and context.primary.close > 0
        )

    def get_parameters(self) -> StrategyParameters:
        return StrategyParameters(
            name=self.name,
            version=self.version,
            params={
                "bb_position_breakout_high": self._params.bb_position_breakout_high,
                "bb_position_breakout_low": self._params.bb_position_breakout_low,
                "bb_width_expansion_min": self._params.bb_width_expansion_min,
                "volume_ratio_min": self._params.volume_ratio_min,
                "min_confidence": self._params.min_confidence,
            },
        )
