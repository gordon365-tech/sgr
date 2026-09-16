"""
SGR Backtest Data Quality Gate
================================
Entscheidet VOR jeder Strategie-Bewertung, ob die historischen Daten
eines Symbols ueberhaupt belastbar genug sind, um eine Strategie darauf
zu testen (Autonomous-Strategy-Universe-Rollout, Phase 3).

Ein Symbol mit unzureichender Historie darf NIE als "schlechte
Strategie" gewertet werden - es ist ein separater, ehrlicher Zustand
(INSUFFICIENT_DATA / INVALID_DATA), keine Sharpe-0-Bewertung.

Wiederverwendet bewusst vorhandene Bausteine statt einer Parallel-
Implementierung:
    - GapDetector (sgr/market_data/gap_detector.py) fuer Luecken.
    - Dieselben OHLC-Sanity-Checks wie BacktestDataLoader._validate()
      (High>=Low, keine negativen Werte) - hier als eigenstaendiges,
      strukturiertes Ergebnis statt nur Log-Zeilen, weil die
      Batch-Validierung eine maschinenlesbare Entscheidung braucht
      (persistiert in StrategySymbolValidationModel.data_quality).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sgr.core.types import Candle
from sgr.market_data.gap_detector import GapDetector

# Warmup (BacktestSimulator.WARMUP_BARS) + genug Bars fuer eine
# Haupt-Simulation und mindestens einen Walk-Forward-Split
# (WalkForwardAnalyzer verlangt >= warmup*2 = 400 Bars pro IS/OOS-
# Fenster). 1000 Bars (~41 Tage bei 1h) ist der Kompromiss zwischen
# "genug fuer mindestens einen echten Split" und "auch neuere Listings
# noch bewertbar, nicht nur Symbole mit voller 180-Tage-Historie".
MIN_CANDLES = 1000

# Anteil fehlender Bars (aus GapDetector), ab dem die Zeitreihe als zu
# lueckenhaft fuer eine belastbare Validierung gilt.
MAX_MISSING_RATIO = 0.05

# Mindestanzahl unterschiedlicher Marktregime (siehe
# sgr.strategy.regime_classifier), damit eine Validierung ueberhaupt
# mehrere Marktphasen sieht statt nur eine einzige - sonst waere ein
# "Walk-Forward konsistent"-Ergebnis nur ein Artefakt einer einzigen,
# zufaelligen Marktphase.
MIN_REGIME_DIVERSITY = 2


class DataQualityStatus(StrEnum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    INVALID_DATA = "invalid_data"


@dataclass
class DataQualityResult:
    status: DataQualityStatus
    n_candles: int
    first_timestamp: str | None
    last_timestamp: str | None
    gap_count: int
    missing_bars: int
    missing_ratio: float
    issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == DataQualityStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "n_candles": self.n_candles,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "gap_count": self.gap_count,
            "missing_bars": self.missing_bars,
            "missing_ratio": round(self.missing_ratio, 4),
            "issues": self.issues,
        }


def assess_data_quality(candles: list[Candle], timeframe: str) -> DataQualityResult:
    """
    Reine Funktion (kein I/O) - prueft eine bereits geladene Candle-
    Liste (siehe BacktestDataLoader.load_public_history(), die bereits
    dedupliziert und zeitlich sortiert liefert).

    Reihenfolge der Pruefungen (erste zutreffende gewinnt):
        1. Zu wenige Candles -> INSUFFICIENT_DATA
        2. Ungueltige OHLCV-Werte (High<Low, negatives Volumen,
           Duplikate, falsche Reihenfolge) -> INVALID_DATA
        3. Zu viele fehlende Bars (Gaps) -> INSUFFICIENT_DATA
           (eine luecken­hafte Zeitreihe ist kein "falscher" Wert,
           sondern fehlende Daten - andere Kategorie als #2)
        4. Sonst -> OK
    """
    n = len(candles)
    first_ts = candles[0].timestamp.isoformat() if candles else None
    last_ts = candles[-1].timestamp.isoformat() if candles else None

    if n < MIN_CANDLES:
        return DataQualityResult(
            status=DataQualityStatus.INSUFFICIENT_DATA,
            n_candles=n,
            first_timestamp=first_ts,
            last_timestamp=last_ts,
            gap_count=0,
            missing_bars=0,
            missing_ratio=0.0,
            issues=[f"Only {n} candles available, minimum {MIN_CANDLES} required"],
        )

    issues: list[str] = []

    # Reihenfolge + Duplikate (BacktestDataLoader liefert bereits
    # sortierte/deduplizierte Daten - dies ist eine unabhaengige
    # Gegenpruefung, kein Vertrauen auf den Aufrufer).
    timestamps = [c.timestamp for c in candles]
    if timestamps != sorted(timestamps):
        issues.append("Candles are not in strictly ascending timestamp order")
    if len(set(timestamps)) != len(timestamps):
        issues.append("Duplicate candle timestamps detected")

    # high>=low ist bereits durch Candle.high_gte_low() (Pydantic-
    # Validator, sgr/core/types.py) bei der Konstruktion erzwungen - ein
    # Candle-Objekt mit high<low kann in dieser Liste gar nicht
    # existieren. Hier nur die Pruefungen, die das Modell NICHT bereits
    # abdeckt.
    for c in candles:
        if c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0:
            issues.append(f"Non-positive price at {c.timestamp.isoformat()}")
        if c.volume < 0:
            issues.append(f"Negative volume at {c.timestamp.isoformat()}")
        if c.high < c.open or c.high < c.close or c.low > c.open or c.low > c.close:
            issues.append(
                f"OHLC inconsistency at {c.timestamp.isoformat()}: "
                "open/close outside high/low range"
            )

    if issues:
        # Deckeln, damit ein systematisch kaputtes Symbol nicht
        # tausende identische Zeilen im DB-Feld data_quality erzeugt.
        return DataQualityResult(
            status=DataQualityStatus.INVALID_DATA,
            n_candles=n,
            first_timestamp=first_ts,
            last_timestamp=last_ts,
            gap_count=0,
            missing_bars=0,
            missing_ratio=0.0,
            issues=issues[:20],
        )

    detector = GapDetector(timeframe)
    gaps = detector.detect_in_series(candles)
    missing_bars = sum(g.missing_candles for g in gaps)
    missing_ratio = missing_bars / n if n > 0 else 0.0

    if missing_ratio > MAX_MISSING_RATIO:
        return DataQualityResult(
            status=DataQualityStatus.INSUFFICIENT_DATA,
            n_candles=n,
            first_timestamp=first_ts,
            last_timestamp=last_ts,
            gap_count=len(gaps),
            missing_bars=missing_bars,
            missing_ratio=missing_ratio,
            issues=[
                f"{len(gaps)} gaps, {missing_bars} missing bars "
                f"({missing_ratio:.1%} > {MAX_MISSING_RATIO:.0%} threshold)"
            ],
        )

    return DataQualityResult(
        status=DataQualityStatus.OK,
        n_candles=n,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        gap_count=len(gaps),
        missing_bars=missing_bars,
        missing_ratio=missing_ratio,
        issues=[],
    )
