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
from typing import Any

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
        Lädt Candles über einen bereits verbundenen Pool-Adapter
        (Testnet im PAPER-Modus, echte Exchange im LIVE-Modus).
        Paginiert automatisch (Exchange-Limits umgehen).

        Für lange historische Backtests siehe load_public_history() -
        Testnets haben typischerweise nur wenige Tage Historie (siehe
        dortiger Docstring), diese Methode ist dafür ungeeignet.
        """
        cache_key = f"{exchange_id.value}:{symbol}:{timeframe}:{start.date()}:{end.date()}"
        if cache_key in self._cache:
            log.info("backtesting.data_loader.cache_hit", symbol=symbol)
            return self._cache[cache_key]

        from sgr.core.types import TradingMode
        from sgr.exchanges.ccxt_base import CCXTBaseAdapter
        from sgr.exchanges.factory import ExchangePool

        assert isinstance(exchange_pool, ExchangePool)

        adapter: CCXTBaseAdapter = exchange_pool.get(exchange_id, TradingMode.PAPER)

        async def fetch_batch(since: datetime, limit: int) -> list[Candle]:
            # get_ohlcv() ist mit @_retryable_exchange_call dekoriert, dessen
            # Decorator-Factory als "-> Any" typisiert ist (siehe
            # sgr/exchanges/ccxt_base.py) - daher der explizite cast hier,
            # statt den bestehenden, an vielen Stellen genutzten Decorator
            # anzufassen (separates Thema, nicht Teil dieses Fixes).
            result: list[Candle] = await adapter.get_ohlcv(
                symbol=symbol, timeframe=timeframe, since=since, limit=limit
            )
            return result

        candles = await self._paginate_and_load(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            fetch_batch=fetch_batch,
        )
        self._cache[cache_key] = candles
        return candles

    async def load_public_history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        exchange_id: ExchangeID = ExchangeID.PIONEX,
    ) -> list[Candle]:
        """
        Lädt lange historische OHLCV-Daten direkt von der oeffentlichen
        Mainnet-API, unabhaengig vom Trading-Modus (kein API-Key, keine
        Order-faehigen Endpoints - reine Marktdaten-Lesezugriffe).

        Grund: Binance Testnet (und Testnets allgemein) haben nur eine
        sehr kurze Datenretention - beobachtet auf dem Server: eine
        Anfrage nach 180 Tagen 1h-Candles lieferte nur 72 Bars (3 Tage)
        zurueck, weit unter dem fuer Walk-Forward-Validierung noetigen
        Minimum von 200 Bars (siehe validation.py). Fuer eine belastbare
        Strategie-Validierung wird echte, lange Marktdaten-Historie
        benoetigt, die nur Mainnet bereithaelt.

        Architektonisch sauber, weil rein lesend: keine Order-Fähigkeit,
        keine Credentials, kein Bezug zu Trading Mode oder dem
        verbundenen ExchangePool - diese Methode oeffnet einen eigenen,
        temporaeren ccxt-Client nur fuer die Dauer des Ladevorgangs und
        schliesst ihn danach wieder. Verwendet ausschliesslich fuer
        Backtest-Validierung (StrategyValidationRunner /
        BacktestingEngine.run_full_validation), niemals fuer Trading-
        Entscheidungen oder Order-Ausfuehrung - das bleibt ausschliesslich
        Aufgabe des ExchangePool/Adapter-Pfads.
        """
        cache_key = f"public:{exchange_id.value}:{symbol}:{timeframe}:{start.date()}:{end.date()}"
        if cache_key in self._cache:
            log.info("backtesting.data_loader.cache_hit", symbol=symbol)
            return self._cache[cache_key]

        candles = await self._load_public_history_with_ccxt_id(
            exchange_id.value, symbol, timeframe, start, end, exchange_id=exchange_id
        )

        self._cache[cache_key] = candles
        return candles

    async def _load_public_history_with_ccxt_id(
        self,
        ccxt_id: str,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        exchange_id: ExchangeID = ExchangeID.PIONEX,
    ) -> list[Candle]:
        """
        Eigentliche Ladelogik hinter load_public_history(), mit dem
        ccxt-Attributnamen als expliziten Parameter statt ihn aus
        exchange_id abzuleiten - erlaubt gezieltes Testen des
        "ccxt hat diese Exchange nicht" Fehlerfalls unabhängig vom
        SGR-eigenen ExchangeID-Enum (das per Definition nur Werte
        enthält, die auch eine ccxt-Entsprechung haben).
        """
        import ccxt.async_support as ccxt_async

        try:
            exchange_class = getattr(ccxt_async, ccxt_id)
        except AttributeError:
            raise RuntimeError(
                f"ccxt has no exchange class '{ccxt_id}' for public history loading"
            ) from None

        public_client = exchange_class({"enableRateLimit": True, "timeout": 30_000})
        try:
            await public_client.load_markets()

            async def fetch_batch(since: datetime, limit: int) -> list[Candle]:
                since_ms = int(since.timestamp() * 1000)
                raw = await public_client.fetch_ohlcv(
                    symbol, timeframe=timeframe, since=since_ms, limit=limit
                )
                sym = self._parse_symbol_str(symbol, exchange_id)
                return sorted(
                    (
                        Candle(
                            symbol=sym,
                            timestamp=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                            timeframe=timeframe,
                            open=Decimal(str(row[1])),
                            high=Decimal(str(row[2])),
                            low=Decimal(str(row[3])),
                            close=Decimal(str(row[4])),
                            volume=Decimal(str(row[5])),
                        )
                        for row in raw
                    ),
                    key=lambda c: c.timestamp,
                )

            candles = await self._paginate_and_load(
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                fetch_batch=fetch_batch,
                log_prefix="public_",
            )
        finally:
            await public_client.close()

        return candles

    def _parse_symbol_str(self, symbol: str, exchange_id: ExchangeID) -> Symbol:
        """Minimaler Symbol-Parser fuer den public-history Pfad, analog
        CCXTBaseAdapter._parse_symbol() - hier ohne Adapter-Instanz.
        Futures-Settle-Suffix (":USDT") wird nicht unterstuetzt, da
        load_public_history() ausschliesslich fuer Backtest-Validierung
        auf Spot-Symbolen verwendet wird (siehe main.py DEFAULT_SYMBOLS)."""
        parts = symbol.split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid symbol format for public history: '{symbol}'")
        return Symbol(base=parts[0], quote=parts[1], exchange=exchange_id)

    async def _paginate_and_load(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        fetch_batch: Any,
        log_prefix: str = "",
    ) -> list[Candle]:
        """
        Gemeinsame Pagination-, Dedup-, Sortier- und Validierungslogik
        fuer load_from_exchange() und load_public_history(). fetch_batch
        ist ein async Callable(since, limit) -> list[Candle], das die
        eigentliche API-spezifische Abfrage kapselt.
        """
        from datetime import timedelta

        from sgr.market_data.gap_detector import GapDetector

        bar_seconds = GapDetector.timeframe_to_seconds(timeframe)
        batch_size = 500
        all_candles: list[Candle] = []
        current_since = start

        log.info(
            f"backtesting.data_loader.{log_prefix}loading",
            symbol=symbol,
            timeframe=timeframe,
            start=start.isoformat(),
            end=end.isoformat(),
        )

        while current_since < end:
            batch = await fetch_batch(current_since, batch_size)

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
                log.warning(
                    f"backtesting.data_loader.{log_prefix}issue", symbol=symbol, issue=issue
                )

        log.info(
            f"backtesting.data_loader.{log_prefix}loaded",
            symbol=symbol,
            count=len(candles),
            from_ts=candles[0].timestamp.isoformat() if candles else "none",
            to_ts=candles[-1].timestamp.isoformat() if candles else "none",
        )

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
