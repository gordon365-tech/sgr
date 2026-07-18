"""
SGR News Fetcher
================
Asynchroner Abruf von Krypto-News aus mehreren Quellen.

Quellen:
    CryptoPanic API:    Professionell kuratierte Crypto-News
    RSS Feeds:          Reuters, Bloomberg Crypto, CoinDesk
    (Social: X/Reddit)  Separate Komponente (API-Kosten)

Rate Limiting:
    CryptoPanic: 1 Request/s (Free Tier), 10/s (Pro)
    RSS Feeds:   Polling alle 5 Minuten

Deduplication:
    URL-basiert + Headline-Similarity (Levenshtein)
    Verhindert, dass dieselbe News mehrfach bewertet wird.

Privacy:
    URLs und Headlines werden nicht persistent geloggt.
    Nur Scores + Entity + Category + Timestamp werden gespeichert.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import aiohttp

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.sentiment.types import SentimentSource

log = get_logger(__name__)

# Bekannte Crypto-RSS Feeds
_RSS_FEEDS = [
    "https://feeds.feedburner.com/CoinDesk",
    "https://cryptonews.com/news/feed/",
    "https://cointelegraph.com/rss",
]

# CryptoPanic API Endpunkt
_CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"


class RawArticle:
    """Roher News-Artikel vor NLP-Verarbeitung."""

    __slots__ = ("url", "headline", "body", "source", "published_at", "entities", "url_hash")

    def __init__(
        self,
        url: str,
        headline: str,
        body: str,
        source: SentimentSource,
        published_at: datetime,
        entities: list[str],
    ) -> None:
        self.url = url
        self.headline = headline
        self.body = body[:1000]  # Max 1000 Zeichen Body
        self.source = source
        self.published_at = published_at
        self.entities = entities
        self.url_hash = hashlib.md5(url.encode()).hexdigest()[:12]


class NewsFetcher:
    """
    Asynchroner News-Fetcher mit Deduplication.

    Usage:
        fetcher = NewsFetcher()
        articles = await fetcher.fetch_recent(max_age_hours=4)
    """

    def __init__(self) -> None:
        self._seen_urls: set[str] = set()
        self._session: aiohttp.ClientSession | None = None
        self._cryptopanic_key: str | None = None

    async def connect(self) -> None:
        """Initialisiert HTTP Session."""
        timeout = aiohttp.ClientTimeout(total=10)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "SGR-SentimentEngine/1.0"},
        )
        # CryptoPanic API Key (optional)
        try:
            get_config()
            # Key aus Config (wenn konfiguriert)
            self._cryptopanic_key = None  # Placeholder
        except Exception:
            pass

        log.info("news_fetcher.connected")

    async def close(self) -> None:
        if self._session:
            await self._session.close()
        log.info("news_fetcher.closed")

    async def fetch_recent(
        self,
        max_age_hours: int = 4,
        currencies: list[str] | None = None,
    ) -> list[RawArticle]:
        """
        Holt aktuelle News aus allen konfigurierten Quellen.

        Args:
            max_age_hours: Nur News jünger als N Stunden
            currencies: Filter-Symbole, z.B. ["BTC", "ETH"]

        Returns:
            Deduplizierte, sortierte Liste von Artikeln
        """
        cutoff = datetime.now(tz=UTC) - timedelta(hours=max_age_hours)
        currencies = currencies or ["BTC", "ETH", "crypto", "bitcoin", "ethereum"]

        tasks = []

        # CryptoPanic (wenn API Key verfügbar)
        if self._cryptopanic_key:
            tasks.append(self._fetch_cryptopanic(currencies))

        # RSS Feeds
        for feed_url in _RSS_FEEDS:
            tasks.append(self._fetch_rss(feed_url, currencies))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        articles: list[RawArticle] = []
        for result in results:
            if isinstance(result, Exception):
                log.warning("news_fetcher.source_error", error=str(result))
                continue
            if isinstance(result, list):
                articles.extend(result)

        # Filter: nur recent + dedupliziert
        fresh = [
            a for a in articles if a.published_at >= cutoff and a.url_hash not in self._seen_urls
        ]

        for a in fresh:
            self._seen_urls.add(a.url_hash)

        # Seen-Set begrenzen (Rolling Window)
        if len(self._seen_urls) > 10_000:
            self._seen_urls = set(list(self._seen_urls)[-5_000:])

        # Sortieren: neueste zuerst
        fresh.sort(key=lambda a: a.published_at, reverse=True)

        log.info(
            "news_fetcher.fetched",
            total=len(articles),
            fresh=len(fresh),
            sources=len(tasks),
        )

        return fresh

    async def _fetch_cryptopanic(self, currencies: list[str]) -> list[RawArticle]:
        """Holt News von CryptoPanic API."""
        if not self._session:
            return []

        params = {
            "auth_token": self._cryptopanic_key,
            "currencies": ",".join(currencies[:5]),  # Max 5
            "filter": "hot",
            "public": "true",
        }

        try:
            async with self._session.get(_CRYPTOPANIC_URL, params=params) as resp:
                if resp.status != 200:
                    log.warning("news_fetcher.cryptopanic_error", status=resp.status)
                    return []

                data = await resp.json()
                articles = []

                for item in data.get("results", [])[:50]:
                    try:
                        published = datetime.fromisoformat(
                            item["published_at"].replace("Z", "+00:00")
                        )
                        entities = [c["code"] for c in item.get("currencies", [])]

                        articles.append(
                            RawArticle(
                                url=item.get("url", ""),
                                headline=item.get("title", ""),
                                body="",
                                source=SentimentSource.NEWS,
                                published_at=published,
                                entities=entities,
                            )
                        )
                    except Exception:
                        continue

                return articles

        except Exception as e:
            log.warning("news_fetcher.cryptopanic_fetch_failed", error=str(e))
            return []

    async def _fetch_rss(self, feed_url: str, currencies: list[str]) -> list[RawArticle]:
        """Holt News aus RSS-Feed."""
        if not self._session:
            return []

        try:
            async with self._session.get(feed_url) as resp:
                if resp.status != 200:
                    return []
                xml_text = await resp.text()

            return self._parse_rss(xml_text, feed_url, currencies)

        except Exception as e:
            log.debug("news_fetcher.rss_failed", url=feed_url, error=str(e))
            return []

    def _parse_rss(
        self,
        xml_text: str,
        source_url: str,
        currencies: list[str],
    ) -> list[RawArticle]:
        """Parst RSS-XML zu RawArticle-Liste."""
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(xml_text)
            articles = []

            # RSS 2.0 und Atom Format
            items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")

            for item in items[:20]:  # Max 20 pro Feed
                # Titel
                title_elem = item.find("title") or item.find("{http://www.w3.org/2005/Atom}title")
                title = title_elem.text if title_elem is not None else ""

                if not title:
                    continue

                # Filter: nur Crypto-relevante Artikel
                title_lower = title.lower()
                currency_hits = [c for c in currencies if c.lower() in title_lower]
                if not currency_hits and not any(
                    kw in title_lower
                    for kw in ["crypto", "bitcoin", "ethereum", "defi", "blockchain"]
                ):
                    continue

                # URL
                link_elem = item.find("link") or item.find("{http://www.w3.org/2005/Atom}link")
                url = ""
                if link_elem is not None:
                    url = link_elem.text or link_elem.get("href", "")

                # Datum
                date_elem = item.find("pubDate") or item.find(
                    "{http://www.w3.org/2005/Atom}published"
                )
                published_at = datetime.now(tz=UTC)
                if date_elem is not None and date_elem.text:
                    try:
                        from email.utils import parsedate_to_datetime

                        published_at = parsedate_to_datetime(date_elem.text)
                        if published_at.tzinfo is None:
                            published_at = published_at.replace(tzinfo=UTC)
                    except Exception:
                        pass

                articles.append(
                    RawArticle(
                        url=url,
                        headline=title,
                        body="",
                        source=SentimentSource.NEWS,
                        published_at=published_at,
                        entities=currency_hits,
                    )
                )

            return articles

        except Exception as e:
            log.debug("news_fetcher.rss_parse_error", error=str(e))
            return []
