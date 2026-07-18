"""
SGR Sentiment Engine
====================
Orchestriert alle Sentiment-Quellen und produziert einen aggregierten Score.

Pipeline:
    News Fetcher → NLP Scorer → SentimentSignal
    Macro Monitor → MacroEvent
    [Social Media] → NLP Scorer → SentimentSignal
         ↓
    SentimentAggregator → SentimentAggregate
         ↓
    Redis Cache (für Strategy Engine + ML Engine)

Update-Frequenz:
    News: alle 5 Minuten
    Makro: alle 60 Minuten
    Social: alle 5 Minuten (wenn verfügbar)

Integration in Trading:
    MarketContext.sentiment_score wird von SentimentAggregate.primary_score befüllt.
    Strategy Engine kann damit kontext-sensitiver sein:
        Starkes bearish Sentiment → kein Long-Entry auch wenn technisch bullish
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import orjson

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.sentiment.macro_monitor import MacroEventMonitor
from sgr.sentiment.news_fetcher import NewsFetcher, RawArticle
from sgr.sentiment.nlp_scorer import SentimentScorer
from sgr.sentiment.types import (
    SentimentAggregate,
    SentimentSignal,
    SentimentSource,
)

log = get_logger(__name__)

# Rollende Fenster (Sekunden)
_WINDOW_1H = 3600
_WINDOW_4H = 14400
_WINDOW_24H = 86400

# Minimale Konfidenz für Signal-Inclusion
_MIN_SIGNAL_CONFIDENCE = 0.30


class SentimentAggregator:
    """
    Aggregiert Sentiment-Signale aus mehreren Quellen über Zeitfenster.
    """

    def __init__(self) -> None:
        self._signals: list[SentimentSignal] = []

    def add_signal(self, signal: SentimentSignal) -> None:
        """Fügt neues Signal hinzu. Alte Signale werden automatisch entfernt."""
        if signal.confidence >= _MIN_SIGNAL_CONFIDENCE:
            self._signals.append(signal)

        # Rolling Window: max 24h + Größen-Limit
        now = datetime.now(tz=UTC)
        cutoff = now.timestamp() - _WINDOW_24H
        self._signals = [s for s in self._signals if s.timestamp.timestamp() > cutoff][
            -5000:
        ]  # Max 5000 Signals in Memory

    def aggregate(self, symbol: str, macro_bias: float = 0.0) -> SentimentAggregate:
        """Berechnet aggregierten Score für ein Symbol."""
        now = datetime.now(tz=UTC)
        now_ts = now.timestamp()

        def _window_score(window_seconds: int) -> tuple[float, float, int]:
            """(score, confidence, count) für ein Zeitfenster."""
            cutoff = now_ts - window_seconds
            window_signals = [
                s
                for s in self._signals
                if s.timestamp.timestamp() > cutoff
                and (
                    symbol.upper() in s.entity.upper()
                    or "market" in s.entity.lower()
                    or s.entity == ""
                )
            ]

            if not window_signals:
                return 0.0, 0.0, 0

            # Konfidenz-gewichteter Durchschnitt
            total_weight = sum(s.confidence for s in window_signals)
            if total_weight == 0:
                return 0.0, 0.0, len(window_signals)

            weighted_score = sum(s.raw_score * s.confidence for s in window_signals)
            score = weighted_score / total_weight
            avg_confidence = total_weight / len(window_signals)

            return float(score), float(min(avg_confidence, 1.0)), len(window_signals)

        score_1h, conf_1h, count_1h = _window_score(_WINDOW_1H)
        score_4h, conf_4h, count_4h = _window_score(_WINDOW_4H)
        score_24h, conf_24h, count_24h = _window_score(_WINDOW_24H)

        # Source-Breakdown (24h)
        source_scores: dict[str, float] = {}
        for source in SentimentSource:
            source_sigs = [
                s
                for s in self._signals
                if s.source == source and s.timestamp.timestamp() > now_ts - _WINDOW_24H
            ]
            if source_sigs:
                total_w = sum(s.confidence for s in source_sigs)
                if total_w > 0:
                    source_scores[source.value] = float(
                        sum(s.raw_score * s.confidence for s in source_sigs) / total_w
                    )

        return SentimentAggregate(
            symbol=symbol,
            timestamp=now,
            score_1h=round(score_1h, 4),
            score_4h=round(score_4h, 4),
            score_24h=round(score_24h, 4),
            confidence_1h=round(conf_1h, 4),
            confidence_4h=round(conf_4h, 4),
            confidence_24h=round(conf_24h, 4),
            signals_1h=count_1h,
            signals_4h=count_4h,
            signals_24h=count_24h,
            source_scores=source_scores,
            macro_bias=round(macro_bias, 4),
        )


class SentimentEngine:
    """
    Haupt-Sentiment-Engine. Orchestriert alle Quellen.

    Lifecycle:
        engine = SentimentEngine()
        await engine.start()
        aggregate = await engine.get_sentiment("BTC")
        await engine.stop()
    """

    def __init__(self) -> None:
        self._scorer = SentimentScorer()
        self._news_fetcher = NewsFetcher()
        self._macro_monitor = MacroEventMonitor()
        self._aggregator = SentimentAggregator()
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._redis: Any = None
        self._macro_bias: float = 0.0

    async def start(self) -> None:
        """Startet alle Collection-Loops."""
        await self._news_fetcher.connect()

        config = get_config()
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(config.redis.url, decode_responses=False)
            await self._redis.ping()
        except Exception as e:
            log.warning("sentiment_engine.redis_unavailable", error=str(e))

        self._running = True

        # News Collection Loop (alle 5 Minuten)
        self._tasks.append(asyncio.create_task(self._news_loop(), name="sentiment:news"))

        # Macro Monitor Loop (alle 60 Minuten)
        self._tasks.append(asyncio.create_task(self._macro_loop(), name="sentiment:macro"))

        log.info(
            "sentiment_engine.started",
            scorer_mode=self._scorer.mode,
            production_ready=self._scorer.is_production_ready,
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._news_fetcher.close()
        if self._redis:
            await self._redis.aclose()
        log.info("sentiment_engine.stopped")

    async def get_sentiment(
        self,
        symbol: str = "BTC",
        cache_ttl_seconds: int = 60,
    ) -> SentimentAggregate:
        """
        Gibt aktuellen Sentiment-Score für ein Symbol zurück.
        Cached in Redis für schnellen Abruf.
        """
        cache_key = f"sentiment:{symbol.upper()}"

        # Redis Cache Check
        if self._redis:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    data = orjson.loads(cached)
                    return SentimentAggregate(**data)
            except Exception:
                pass

        # Neu berechnen
        aggregate = self._aggregator.aggregate(symbol, self._macro_bias)

        # In Redis cachen
        if self._redis:
            try:
                payload = orjson.dumps(
                    {
                        "symbol": aggregate.symbol,
                        "timestamp": aggregate.timestamp.isoformat(),
                        "score_1h": aggregate.score_1h,
                        "score_4h": aggregate.score_4h,
                        "score_24h": aggregate.score_24h,
                        "confidence_1h": aggregate.confidence_1h,
                        "confidence_4h": aggregate.confidence_4h,
                        "confidence_24h": aggregate.confidence_24h,
                        "signals_1h": aggregate.signals_1h,
                        "signals_4h": aggregate.signals_4h,
                        "signals_24h": aggregate.signals_24h,
                        "source_scores": aggregate.source_scores,
                        "macro_bias": aggregate.macro_bias,
                    }
                )
                await self._redis.set(cache_key, payload, ex=cache_ttl_seconds)
            except Exception:
                pass

        return aggregate

    # ------------------------------------------------------------------
    # Collection Loops
    # ------------------------------------------------------------------

    async def _news_loop(self) -> None:
        """Holt News alle 5 Minuten und bewertet sie."""
        while self._running:
            try:
                articles = await self._news_fetcher.fetch_recent(max_age_hours=4)

                for article in articles:
                    await self._process_article(article)

                log.debug(
                    "sentiment_engine.news_cycle",
                    articles_processed=len(articles),
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("sentiment_engine.news_loop_error", error=str(e))

            await asyncio.sleep(300)  # 5 Minuten

    async def _macro_loop(self) -> None:
        """Aktualisiert Makro-Daten stündlich."""
        while self._running:
            try:
                events = await self._macro_monitor.fetch_recent_events(max_age_days=7)
                self._macro_bias = self._macro_monitor.get_macro_bias(events)

                log.info(
                    "sentiment_engine.macro_updated",
                    events=len(events),
                    macro_bias=f"{self._macro_bias:.3f}",
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("sentiment_engine.macro_loop_error", error=str(e))

            await asyncio.sleep(3600)  # 60 Minuten

    async def _process_article(self, article: RawArticle) -> None:
        """Bewertet einen Artikel und fügt Signal zum Aggregator hinzu."""
        text = f"{article.headline} {article.body}".strip()
        if not text:
            return

        score, confidence = self._scorer.score(text)
        event_category = self._scorer.classify_event(text, score)

        # Entity-Mapping: welches Symbol ist betroffen?
        entity = article.entities[0] if article.entities else "market"

        signal = SentimentSignal(
            source=article.source,
            timestamp=article.published_at,
            raw_score=score,
            confidence=confidence,
            event_category=event_category,
            entity=entity,
            headline="",  # Privacy: Headline nicht in Signal gespeichert
            url="",
        )

        self._aggregator.add_signal(signal)
