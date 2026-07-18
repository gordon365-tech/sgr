"""
SGR Gap Detector
================
Erkennt fehlende Candles in OHLCV-Zeitreihen.

Wichtig: Keine Interpolation. Echte Daten oder Lücke melden.
Interpolierte Preise würden Indikatoren verfälschen und sind
in einem Trading-System gefährlich (Look-Ahead Bias im Backtest).

Timeframe → Sekunden Mapping muss exakt sein,
da Candle-Timestamps von Exchanges nicht immer auf die Minute fallen.
Toleranz: ±5 Sekunden für Timestamp-Vergleiche.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sgr.core.logging import get_logger
from sgr.market_data.types import DataGap

log = get_logger(__name__)

# Timeframe → Dauer in Sekunden
_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}

_TIMESTAMP_TOLERANCE_SECONDS = 5


class GapDetector:
    """
    Prüft ob zwischen bekannten und neuen Candles Lücken existieren.
    """

    def __init__(self, timeframe: str) -> None:
        self.timeframe = timeframe
        self._bar_seconds = _TIMEFRAME_SECONDS.get(timeframe, 3600)

    def detect(
        self,
        existing: list[Any],  # list[Candle]
        incoming: list[Any],  # list[Candle]
    ) -> list[DataGap]:
        """
        Vergleicht letzten bekannten Candle mit ersten neuen Candles.
        Gibt Liste von DataGap-Objekten zurück (leer wenn keine Lücken).
        """
        if not existing or not incoming:
            return []

        last_known = existing[-1]
        first_new = incoming[0]

        gaps: list[DataGap] = []

        expected_next = last_known.timestamp + timedelta(seconds=self._bar_seconds)
        actual_next = first_new.timestamp

        diff_seconds = abs((actual_next - expected_next).total_seconds())

        # Innerhalb Toleranz: kein Gap
        if diff_seconds <= _TIMESTAMP_TOLERANCE_SECONDS:
            return []

        # Größer als eine Bar: Gap
        if actual_next > expected_next + timedelta(seconds=_TIMESTAMP_TOLERANCE_SECONDS):
            missing = int((actual_next - expected_next).total_seconds() / self._bar_seconds)
            if missing > 0:
                gaps.append(
                    DataGap(
                        symbol=last_known.symbol,
                        timeframe=self.timeframe,
                        gap_start=expected_next,
                        gap_end=actual_next,
                        missing_candles=missing,
                    )
                )
                log.warning(
                    "gap_detector.gap_found",
                    symbol=str(last_known.symbol),
                    timeframe=self.timeframe,
                    missing_candles=missing,
                    gap_start=expected_next.isoformat(),
                    gap_end=actual_next.isoformat(),
                )

        return gaps

    def detect_in_series(self, candles: list[Any]) -> list[DataGap]:
        """
        Prüft eine komplette Candle-Serie auf interne Lücken.
        Verwendet für Backtest-Daten-Validierung.
        """
        if len(candles) < 2:
            return []

        gaps: list[DataGap] = []
        for i in range(1, len(candles)):
            prev = candles[i - 1]
            curr = candles[i]
            result = self.detect([prev], [curr])
            gaps.extend(result)

        return gaps

    @staticmethod
    def timeframe_to_seconds(timeframe: str) -> int:
        return _TIMEFRAME_SECONDS.get(timeframe, 3600)
