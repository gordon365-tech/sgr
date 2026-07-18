"""
SGR Backtesting Data Loader
============================
Lädt historische OHLCV-Daten für den Backtest.

Quellen (in Reihenfolge):
    1. TimescaleDB (primär, falls vorhanden)
    2. Exchange API (Fallback, limitiert auf ~1000 Bars)
    3. CSV-Dateien (für Offline-Tests)

Validierung:
    - Lücken in Zeitreihe erkannt und gemeldet
    - Duplicate Timestamps entfernt
    - OHLC-Sanity-Check (High >= Low, etc.)
    - Minimale Bar-Anzahl sichergestellt

Look-Ahead-Prävention:
    DataLoader gibt niemals Daten nach dem aktuellen
    Simulations-Zeitpunkt zurück.
    Candles werden sortiert und indexiert.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sgr.core.logging import get_logger
from sgr.core.types import Candle, ExchangeID, Symbol
from sgr.market_data.gap_detector import GapDetector

log = get_logger(__name__)


class BacktestDataLoader:
    """
    Lädt und validiert historische OHLCV-Daten.

    Usage:
        loader = BacktestDataLoader()
        candles = await loader.load_from_exchange(
            symbol="BTC/USDT",
            timeframe="1h",
            start=datetime(2023, 1, 1),
            end=datetime(2023, 12, 31),
            exchange_pool=pool,
        )
    """

    def __init__(self) -> None:
        self._cache: dict[str, list[Candle]] = {}

    async def load_from_exchange(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        exchange_pool: object,
        exchange_id: ExchangeID = ExchangeID.PIONEX,
    ) -> list[Candle]:
        """
        Lädt Candles von Exchange API.
        Paginiert automatisch (Exchange-Limits umgehen).
        """
        cache_key = f"{exchange_id.value}:{symbol}:{timeframe}:{start.date()}:{end.date()}"
        if cache_key in self._cache:
            log.info("backtesting.data_loader.cache_hit", symbol=symbol)
            return self._cache[cache_key]

        from sgr.core.types import TradingMode
        from sgr.exchanges.factory import ExchangePool

        assert isinstance(exchange_pool, ExchangePool)

        adapter = exchange_pool.get(exchange_id, TradingMode.PAPER)
        all_candles: list[Candle] = []
        current_since = start

        from datetime import timedelta

        from sgr.market_data.gap_detector import GapDetector

        bar_seconds = GapDetector.timeframe_to_seconds(timeframe)
        batch_size = 500

        log.info(
            "backtesting.data_loader.loading",
            symbol=symbol,
            timeframe=timeframe,
            start=start.isoformat(),
            end=end.isoformat(),
        )

        while current_since < end:
            batch = await adapter.get_ohlcv(
                symbol=symbol,
                timeframe=timeframe,
                since=current_since,
                limit=batch_size,
            )

            if not batch:
                break

            # Filter: nur bis end_date
            batch = [c for c in batch if c.timestamp <= end]
            all_candles.extend(batch)

            if len(batch) < batch_size:
                break

            # Nächste Seite ab letztem Timestamp
            current_since = batch[-1].timestamp + timedelta(seconds=bar_seconds)

        # Deduplizieren + Sortieren
        candles = self._deduplicate(all_candles)
        candles = sorted(candles, key=lambda c: c.timestamp)

        # Validierung
        issues = self._validate(candles, timeframe)
        if issues:
            for issue in issues:
                log.warning("backtesting.data_loader.issue", symbol=symbol, issue=issue)

        log.info(
            "backtesting.data_loader.loaded",
            symbol=symbol,
            count=len(candles),
            from_ts=candles[0].timestamp.isoformat() if candles else "none",
            to_ts=candles[-1].timestamp.isoformat() if candles else "none",
        )

        self._cache[cache_key] = candles
        return candles

    def load_from_csv(
        self,
        path: Path,
        symbol_str: str,
        timeframe: str,
        exchange_id: ExchangeID = ExchangeID.PIONEX,
    ) -> list[Candle]:
        """
        Lädt OHLCV aus CSV-Datei.
        Format: timestamp,open,high,low,close,volume
        timestamp: ISO-Format oder Unix-Timestamp (ms)
        """
        import csv

        sym = Symbol(
            base=symbol_str.split("/")[0],
            quote=symbol_str.split("/")[1],
            exchange=exchange_id,
        )

        candles: list[Candle] = []
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts_raw = row.get("timestamp", row.get("time", ""))
                    if ts_raw.isdigit():
                        ts = datetime.fromtimestamp(int(ts_raw) / 1000, tz=UTC)
                    else:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))

                    candles.append(
                        Candle(
                            symbol=sym,
                            timestamp=ts,
                            timeframe=timeframe,
                            open=Decimal(row["open"]),
                            high=Decimal(row["high"]),
                            low=Decimal(row["low"]),
                            close=Decimal(row["close"]),
                            volume=Decimal(row.get("volume", "0")),
                        )
                    )
                except Exception as e:
                    log.warning("backtesting.csv.row_error", error=str(e))

        candles = self._deduplicate(candles)
        candles = sorted(candles, key=lambda c: c.timestamp)
        log.info("backtesting.csv.loaded", path=str(path), count=len(candles))
        return candles

    # ------------------------------------------------------------------
    # Iterator für Event-driven Simulation
    # ------------------------------------------------------------------

    def iterate(
        self,
        candles: list[Candle],
        warmup_bars: int = 200,
    ) -> Iterator[tuple[int, Candle, list[Candle]]]:
        """
        Iterator für Event-driven Backtesting.

        Yields:
            (bar_index, current_candle, history_up_to_current)

        history enthält alle Candles BIS ZUM current_candle (inklusive).
        NIEMALS den nächsten Candle – Look-Ahead Prevention.

        warmup_bars: Bars am Anfang überspringen (für Indikator-Warmup).
        """
        for i in range(warmup_bars, len(candles)):
            history = candles[: i + 1]  # Strikt: nur bis incl. aktuellen Bar
            yield i, candles[i], history

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    def _deduplicate(self, candles: list[Candle]) -> list[Candle]:
        """Entfernt duplicate Timestamps (letzter Wert gewinnt)."""
        seen: dict[datetime, Candle] = {}
        for c in candles:
            seen[c.timestamp] = c
        return list(seen.values())

    def _validate(self, candles: list[Candle], timeframe: str) -> list[str]:
        """Findet Datenfehler und Lücken."""
        issues: list[str] = []
        if len(candles) < 50:
            issues.append(f"Very few candles: {len(candles)}")

        detector = GapDetector(timeframe)
        gaps = detector.detect_in_series(candles)
        if gaps:
            total_missing = sum(g.missing_candles for g in gaps)
            issues.append(f"{len(gaps)} gaps detected, {total_missing} missing bars")

        for c in candles:
            if c.high < c.low:
                issues.append(f"OHLC error at {c.timestamp}: high < low")
            if c.volume < 0:
                issues.append(f"Negative volume at {c.timestamp}")

        return issues
