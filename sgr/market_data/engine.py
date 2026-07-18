"""
SGR Market Data Engine
======================
Orchestriert den gesamten Market-Data-Flow:

    Exchange WebSocket/REST
         ↓
    Normalisierung + Gap-Detection
         ↓
    Feature Engineering
         ↓
    Feature Store (Redis)  +  DB (TimescaleDB)
         ↓
    Event Bus (CandleEvent für Strategy Engine)

Design-Entscheidungen:
- Ein Engine-Objekt pro Symbol+Timeframe-Kombination.
  Keine God-Class die alles verwaltet.
- Zwei Modi:
    REST-Polling: für historische Daten + Backtesting
    WebSocket: für Live-Trading (sub-Sekunden Latenz)
- Gap-Detection: fehlende Candles werden erkannt und gefüllt
  (durch Nachfragen bei Exchange, nicht durch Interpolation)
- Graceful Degradation: Exchange-Ausfall → weiter mit gecachten Features

Symbole werden zur Laufzeit registriert:
    engine.subscribe("BTC/USDT", ["1h", "4h"])
    engine.subscribe("ETH/USDT", ["1h"])
    await engine.start()
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sgr.core.event_bus import get_event_bus
from sgr.core.logging import get_logger
from sgr.core.types import (
    CandleEvent,
    ExchangeID,
    TradingMode,
)
from sgr.exchanges.base import ExchangeError
from sgr.exchanges.factory import ExchangePool
from sgr.market_data.feature_engineering import FeatureEngineer
from sgr.market_data.feature_store import FeatureStore
from sgr.market_data.gap_detector import GapDetector
from sgr.market_data.types import DataGap

log = get_logger(__name__)

# Polling interval per timeframe (etwas kürzer als Bar-Länge)
_POLL_INTERVALS: dict[str, float] = {
    "1m": 55.0,
    "3m": 170.0,
    "5m": 290.0,
    "15m": 890.0,
    "30m": 1790.0,
    "1h": 3590.0,
    "4h": 14390.0,
    "1d": 86390.0,
}

# Candle-History pro Timeframe für Feature-Berechnung
_HISTORY_REQUIRED: dict[str, int] = {
    "1m": 250,
    "5m": 250,
    "15m": 250,
    "1h": 250,
    "4h": 200,
    "1d": 200,
}


class SymbolFeed:
    """
    Daten-Feed für ein Symbol + Timeframe.
    Hält den Candle-Buffer und triggert Feature-Berechnung.
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        exchange_id: ExchangeID,
        trading_mode: TradingMode,
        feature_engineer: FeatureEngineer,
        feature_store: FeatureStore,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange_id = exchange_id
        self.trading_mode = trading_mode

        self._engineer = feature_engineer
        self._store = feature_store
        self._gap_detector = GapDetector(timeframe)
        self._candle_buffer: list[Any] = []  # list[Candle]
        self._max_buffer = _HISTORY_REQUIRED.get(timeframe, 250)
        self._last_processed_ts: datetime | None = None
        self._initialized = False

    async def initialize(self, pool: ExchangePool) -> None:
        """
        Lädt historische Candles beim Start.
        Füllt den Buffer für sofortige Feature-Berechnung.
        """
        try:
            adapter = pool.get(self.exchange_id, self.trading_mode)
            candles = await adapter.get_ohlcv(
                self.symbol,
                self.timeframe,
                limit=self._max_buffer,
            )

            if not candles:
                log.warning(
                    "market_data.feed.no_history",
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                )
                return

            self._candle_buffer = candles[-self._max_buffer :]
            self._last_processed_ts = candles[-1].timestamp
            self._initialized = True

            log.info(
                "market_data.feed.initialized",
                symbol=self.symbol,
                timeframe=self.timeframe,
                candles=len(self._candle_buffer),
                latest=self._last_processed_ts.isoformat(),
            )

        except ExchangeError as e:
            log.error(
                "market_data.feed.init_failed",
                symbol=self.symbol,
                timeframe=self.timeframe,
                error=str(e),
            )

    async def update(self, pool: ExchangePool) -> bool:
        """
        Holt neue Candles und berechnet Features.
        Returns True wenn neue Features berechnet wurden.
        """
        if not self._initialized:
            await self.initialize(pool)
            if not self._initialized:
                return False

        try:
            adapter = pool.get(self.exchange_id, self.trading_mode)

            # Nur neue Candles abrufen (seit letztem bekannten Timestamp)
            since = self._last_processed_ts
            new_candles = await adapter.get_ohlcv(
                self.symbol,
                self.timeframe,
                since=since,
                limit=10,  # Nur wenige neue Candles erwartet
            )

            if not new_candles:
                return False

            # Filter: nur wirklich neue Candles
            if self._last_processed_ts:
                new_candles = [c for c in new_candles if c.timestamp > self._last_processed_ts]

            if not new_candles:
                return False

            # Gap Detection
            gaps = self._gap_detector.detect(
                self._candle_buffer[-5:] if self._candle_buffer else [],
                new_candles,
            )
            if gaps:
                await self._handle_gaps(gaps, pool)

            # Buffer aktualisieren
            self._candle_buffer.extend(new_candles)
            self._candle_buffer = self._candle_buffer[-self._max_buffer :]
            self._last_processed_ts = new_candles[-1].timestamp

            # Features berechnen
            if len(self._candle_buffer) >= 2:
                features = self._engineer.compute(self._candle_buffer)
                await self._store.save(features)

                log.debug(
                    "market_data.feed.updated",
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    new_candles=len(new_candles),
                    buffer_size=len(self._candle_buffer),
                )
                return True

        except ExchangeError as e:
            if e.retryable:
                log.warning(
                    "market_data.feed.transient_error",
                    symbol=self.symbol,
                    error=str(e),
                )
            else:
                log.error(
                    "market_data.feed.permanent_error",
                    symbol=self.symbol,
                    error=str(e),
                )

        return False

    async def _handle_gaps(self, gaps: list[DataGap], pool: ExchangePool) -> None:
        """
        Füllt Datenlücken durch Nachfragen bei Exchange.
        Interpoliert NICHT – echte Daten oder nichts.
        """
        for gap in gaps:
            log.warning(
                "market_data.gap_detected",
                symbol=self.symbol,
                timeframe=self.timeframe,
                gap_start=gap.gap_start.isoformat(),
                gap_end=gap.gap_end.isoformat(),
                missing=gap.missing_candles,
            )
            try:
                adapter = pool.get(self.exchange_id, self.trading_mode)
                fill_candles = await adapter.get_ohlcv(
                    self.symbol,
                    self.timeframe,
                    since=gap.gap_start,
                    limit=gap.missing_candles + 5,
                )
                if fill_candles:
                    # Insert in buffer (sorted)
                    self._candle_buffer.extend(fill_candles)
                    self._candle_buffer.sort(key=lambda c: c.timestamp)
                    self._candle_buffer = list(
                        {c.timestamp: c for c in self._candle_buffer}.values()
                    )
                    log.info(
                        "market_data.gap_filled",
                        symbol=self.symbol,
                        filled=len(fill_candles),
                    )
            except ExchangeError as e:
                log.error("market_data.gap_fill_failed", error=str(e))


class MarketDataEngine:
    """
    Zentrale Market Data Engine.
    Verwaltet SymbolFeeds für alle subscribed Symbole.
    Koordiniert Polling-Loop und Event-Publishing.

    Usage:
        engine = MarketDataEngine(pool, TradingMode.PAPER)
        engine.subscribe("BTC/USDT", ExchangeID.PIONEX, ["1h", "4h"])
        engine.subscribe("ETH/USDT", ExchangeID.PIONEX, ["1h"])
        await engine.start()
        # ... runs until stopped
        await engine.stop()
    """

    def __init__(
        self,
        pool: ExchangePool,
        trading_mode: TradingMode,
        feature_store: FeatureStore | None = None,
    ) -> None:
        self._pool = pool
        self._trading_mode = trading_mode
        self._engineer = FeatureEngineer()
        self._store = feature_store or FeatureStore()
        self._feeds: dict[str, SymbolFeed] = {}  # key: "{symbol}:{timeframe}"
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    def subscribe(
        self,
        symbol: str,
        exchange_id: ExchangeID,
        timeframes: list[str],
    ) -> None:
        """
        Registriert Symbol + Timeframes für Daten-Ingestion.
        Muss vor start() aufgerufen werden.
        """
        for tf in timeframes:
            key = f"{symbol}:{tf}"
            if key not in self._feeds:
                self._feeds[key] = SymbolFeed(
                    symbol=symbol,
                    timeframe=tf,
                    exchange_id=exchange_id,
                    trading_mode=self._trading_mode,
                    feature_engineer=self._engineer,
                    feature_store=self._store,
                )
                log.info(
                    "market_data.subscribed",
                    symbol=symbol,
                    timeframe=tf,
                    exchange=exchange_id.value,
                )

    async def start(self) -> None:
        """
        Startet alle Polling-Loops.
        Blockiert nicht – Tasks laufen im Hintergrund.
        """
        if not self._store._redis:
            await self._store.connect()

        self._running = True

        # Initialize all feeds concurrently
        init_tasks = [feed.initialize(self._pool) for feed in self._feeds.values()]
        if init_tasks:
            await asyncio.gather(*init_tasks, return_exceptions=True)

        # Start polling loops
        for key, feed in self._feeds.items():
            interval = _POLL_INTERVALS.get(feed.timeframe, 60.0)
            task = asyncio.create_task(
                self._poll_loop(feed, interval),
                name=f"market_data:{key}",
            )
            self._tasks.append(task)

        log.info(
            "market_data.engine.started",
            feeds=len(self._feeds),
            trading_mode=self._trading_mode.value,
        )

    async def stop(self) -> None:
        """Stoppt alle Polling-Loops gracefully."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._store.close()
        log.info("market_data.engine.stopped")

    async def _poll_loop(self, feed: SymbolFeed, interval: float) -> None:
        """
        Polling-Loop für einen Feed.
        Wartet `interval` Sekunden zwischen Updates.
        Fehler werden geloggt und Loop weitergeführt (resilient).
        """
        while self._running:
            try:
                updated = await feed.update(self._pool)

                if updated and feed._candle_buffer:
                    await self._publish_candle_event(feed)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(
                    "market_data.poll_loop.unexpected_error",
                    symbol=feed.symbol,
                    timeframe=feed.timeframe,
                    error=str(e),
                    exc_info=True,
                )

            await asyncio.sleep(interval)

    async def _publish_candle_event(self, feed: SymbolFeed) -> None:
        """Publiziert CandleEvent auf Event Bus für Strategy Engine."""
        if not feed._candle_buffer:
            return

        try:
            last_candle = feed._candle_buffer[-1]
            event = CandleEvent(
                timestamp=datetime.now(tz=UTC),
                candle=last_candle,
            )
            bus = get_event_bus()
            await bus.publish(event)
        except Exception as e:
            log.warning("market_data.publish_failed", error=str(e))

    def get_subscribed_symbols(self) -> list[tuple[str, str]]:
        """Returns list of (symbol, timeframe) tuples."""
        return [(f.symbol, f.timeframe) for f in self._feeds.values()]

    @property
    def is_running(self) -> bool:
        return self._running
