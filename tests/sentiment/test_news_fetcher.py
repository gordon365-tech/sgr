"""
Tests für sgr.sentiment.news_fetcher.

Strategie: aiohttp.ClientSession wird durch eine minimale Fake-Session mit
async-Context-Manager-Response ersetzt (Standardmuster für aiohttp-Mocks,
kein externes Mocking-Framework benötigt). Netzwerk wird nie tatsächlich
angesprochen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from sgr.sentiment.news_fetcher import (
    _CRYPTOPANIC_URL,
    _RSS_FEEDS,
    NewsFetcher,
    RawArticle,
)
from sgr.sentiment.types import SentimentSource

# ---------------------------------------------------------------------------
# Fake aiohttp primitives
# ---------------------------------------------------------------------------


class FakeResponse:
    """Fake aiohttp response usable as an async context manager."""

    def __init__(self, status: int = 200, json_data: dict | None = None, text_data: str = ""):
        self.status = status
        self._json_data = json_data or {}
        self._text_data = text_data

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Fake aiohttp.ClientSession.get() returning a queued FakeResponse."""

    def __init__(self, response: FakeResponse | Exception):
        self._response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params or {}))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# ---------------------------------------------------------------------------
# Sample RSS payloads
# ---------------------------------------------------------------------------

RSS2_XML = """<?xml version="1.0"?>
<rss version="2.0">
<channel>
  <title>Crypto News</title>
  <item>
    <title>Bitcoin surges past new milestone</title>
    <link>https://example.com/btc-surge</link>
    <pubDate>Mon, 01 Jan 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Local weather report</title>
    <link>https://example.com/weather</link>
    <pubDate>Mon, 01 Jan 2026 13:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Ethereum DeFi protocol launches</title>
    <link>https://example.com/eth-defi</link>
  </item>
</channel>
</rss>
"""

ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Blockchain Feed</title>
  <entry>
    <title>Blockchain adoption grows</title>
    <link href="https://example.com/blockchain-growth"/>
    <published>2026-01-01T12:00:00Z</published>
  </entry>
</feed>
"""

EMPTY_TITLE_XML = """<?xml version="1.0"?>
<rss version="2.0">
<channel>
  <item>
    <link>https://example.com/no-title</link>
  </item>
</channel>
</rss>
"""

MALFORMED_XML = "<rss><channel><item><title>Unterminated"


# ---------------------------------------------------------------------------
# RawArticle
# ---------------------------------------------------------------------------


class TestRawArticle:
    def test_basic_fields(self):
        now = datetime.now(tz=UTC)
        article = RawArticle(
            url="https://example.com/a",
            headline="Headline",
            body="Body text",
            source=SentimentSource.NEWS,
            published_at=now,
            entities=["BTC"],
        )
        assert article.url == "https://example.com/a"
        assert article.headline == "Headline"
        assert article.body == "Body text"
        assert article.source == SentimentSource.NEWS
        assert article.published_at == now
        assert article.entities == ["BTC"]

    def test_body_truncated_to_1000_chars(self):
        long_body = "x" * 2000
        article = RawArticle(
            url="https://example.com/a",
            headline="H",
            body=long_body,
            source=SentimentSource.NEWS,
            published_at=datetime.now(tz=UTC),
            entities=[],
        )
        assert len(article.body) == 1000

    def test_url_hash_deterministic_and_short(self):
        a1 = RawArticle(
            url="https://example.com/x",
            headline="H",
            body="",
            source=SentimentSource.NEWS,
            published_at=datetime.now(tz=UTC),
            entities=[],
        )
        a2 = RawArticle(
            url="https://example.com/x",
            headline="Different headline",
            body="",
            source=SentimentSource.NEWS,
            published_at=datetime.now(tz=UTC),
            entities=[],
        )
        assert a1.url_hash == a2.url_hash
        assert len(a1.url_hash) == 12

    def test_different_urls_produce_different_hashes(self):
        a1 = RawArticle(
            "https://example.com/1", "H", "", SentimentSource.NEWS, datetime.now(tz=UTC), []
        )
        a2 = RawArticle(
            "https://example.com/2", "H", "", SentimentSource.NEWS, datetime.now(tz=UTC), []
        )
        assert a1.url_hash != a2.url_hash


# ---------------------------------------------------------------------------
# connect / close
# ---------------------------------------------------------------------------


class TestConnectClose:
    async def test_connect_creates_session(self):
        fetcher = NewsFetcher()
        assert fetcher._session is None

        await fetcher.connect()
        try:
            assert fetcher._session is not None
            assert fetcher._cryptopanic_key is None
        finally:
            await fetcher.close()

    async def test_connect_survives_config_error(self):
        fetcher = NewsFetcher()
        with patch("sgr.sentiment.news_fetcher.get_config", side_effect=RuntimeError("no config")):
            await fetcher.connect()
        try:
            assert fetcher._session is not None
        finally:
            await fetcher.close()

    async def test_close_without_connect_does_not_raise(self):
        fetcher = NewsFetcher()
        await fetcher.close()  # no session created; should be a no-op

    async def test_close_closes_session(self):
        fetcher = NewsFetcher()
        await fetcher.connect()
        session = fetcher._session
        assert session is not None
        assert session.closed is False
        await fetcher.close()
        assert session.closed is True


# ---------------------------------------------------------------------------
# _parse_rss
# ---------------------------------------------------------------------------


class TestParseRss:
    """
    HINWEIS zu einem Bestandsbefund (nicht behoben, siehe Session-Notiz):

    `item.find("title") or item.find("{atom}title")` (und die analogen
    Konstrukte für link/pubDate) nutzen Python's `or`-Kurzschluss. Ein
    gefundenes `xml.etree.ElementTree.Element` ohne Kind-Elemente ist
    jedoch falsy (`bool(elem) == False`, auch wenn `elem.text` gesetzt
    ist) - das trifft auf praktisch jedes reale `<title>Text</title>`-Item
    zu. Dadurch fällt der Ausdruck IMMER auf den Atom-Fallback durch, der
    für Standard-RSS2-Items `None` liefert. Effekt: In der aktuellen
    Implementierung werden reale RSS2-Feeds mit einfachen Text-Kindelementen
    faktisch nie korrekt geparst (title_elem/link_elem/date_elem bleiben
    None). Diese Tests dokumentieren das TATSÄCHLICHE Verhalten, nicht das
    beabsichtigte - keine Korrektur ohne explizite Freigabe.
    """

    def test_title_element_lookup_is_shadowed_by_falsy_element_bug(self):
        """`item.find("title") or ...` ist immer falsy für ein gefundenes,
        kindloses Element -> title_elem wird nie aus dem RSS-Zweig gesetzt."""
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(RSS2_XML, "https://feed.example.com", ["BTC", "ETH"])
        # Aufgrund des Bugs bleibt title="" für alle Items -> alle werden
        # durch `if not title: continue` verworfen.
        assert articles == []

    def test_atom_feed_unaffected_because_atom_lookup_is_the_fallback_branch(self):
        """Atom-Feeds sind vom Bug nicht betroffen, weil `item.find("title")`
        (ohne Namespace) dort nichts findet (None ist falsy wie erwartet)
        und der Fallback auf den Atom-Namespace korrekt greift."""
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(ATOM_XML, "https://feed.example.com", ["BTC"])
        assert len(articles) == 1
        assert articles[0].headline == "Blockchain adoption grows"
        assert articles[0].url == "https://example.com/blockchain-growth"

    def test_item_without_any_title_is_skipped(self):
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(EMPTY_TITLE_XML, "https://feed.example.com", ["BTC"])
        assert articles == []

    def test_malformed_xml_returns_empty_list(self):
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(MALFORMED_XML, "https://feed.example.com", ["BTC"])
        assert articles == []

    def test_currency_match_populates_entities(self):
        """Nutzt Atom-Format, da RSS2-Items wegen des title-Bugs oben nie
        den title-Guard passieren."""
        xml = """<?xml version="1.0" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>BTC rallies hard today</title>
<link href="https://example.com/btc-rally"/>
<published>2026-01-01T12:00:00Z</published></entry>
</feed>"""
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(xml, "https://feed.example.com", ["BTC", "ETH"])
        assert len(articles) == 1
        assert "BTC" in articles[0].entities

    def test_keyword_match_without_currency_still_included(self):
        xml = """<?xml version="1.0" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Blockchain technology explained</title>
<link href="https://example.com/blockchain-explainer"/></entry>
</feed>"""
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(xml, "https://feed.example.com", ["BTC", "ETH"])
        assert len(articles) == 1
        assert articles[0].entities == []

    def test_no_currency_and_no_keyword_match_is_filtered_out(self):
        xml = """<?xml version="1.0" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Local weather report today</title>
<link href="https://example.com/weather"/></entry>
</feed>"""
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(xml, "https://feed.example.com", ["BTC", "ETH"])
        assert articles == []

    def test_bad_pubdate_falls_back_to_now(self):
        xml = """<?xml version="1.0" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Bitcoin news item</title>
<link href="https://example.com/x"/>
<published>not-a-real-date</published></entry>
</feed>"""
        fetcher = NewsFetcher()
        before = datetime.now(tz=UTC)
        articles = fetcher._parse_rss(xml, "https://feed.example.com", ["BTC"])
        after = datetime.now(tz=UTC)
        assert before <= articles[0].published_at <= after

    def test_pubdate_without_tzinfo_gets_utc_attached(self):
        xml = """<?xml version="1.0" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Bitcoin naive date item</title>
<link href="https://example.com/naive-date"/>
<published>Mon, 01 Jan 2026 12:00:00</published></entry>
</feed>"""
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(xml, "https://feed.example.com", ["BTC"])
        assert articles[0].published_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert articles[0].published_at.tzinfo is not None

    def test_item_without_pubdate_uses_now(self):
        xml = """<?xml version="1.0" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Bitcoin news without date</title>
<link href="https://example.com/nodate"/></entry>
</feed>"""
        fetcher = NewsFetcher()
        before = datetime.now(tz=UTC)
        articles = fetcher._parse_rss(xml, "https://feed.example.com", ["BTC"])
        after = datetime.now(tz=UTC)
        assert before <= articles[0].published_at <= after

    def test_link_via_href_attribute_used_when_no_text(self):
        xml = """<?xml version="1.0" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Bitcoin atom entry</title>
<link href="https://example.com/atom-link"/></entry>
</feed>"""
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(xml, "https://feed.example.com", ["BTC"])
        assert articles[0].url == "https://example.com/atom-link"

    def test_max_20_items_per_feed(self):
        entries = "".join(
            f'<entry><title>Bitcoin item {i}</title><link href="https://example.com/{i}"/></entry>'
            for i in range(30)
        )
        xml = f'<?xml version="1.0" ?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
        fetcher = NewsFetcher()
        articles = fetcher._parse_rss(xml, "https://feed.example.com", ["BTC"])
        assert len(articles) == 20


# ---------------------------------------------------------------------------
# _fetch_rss
# ---------------------------------------------------------------------------


class TestFetchRss:
    async def test_no_session_returns_empty(self):
        fetcher = NewsFetcher()
        result = await fetcher._fetch_rss("https://feed.example.com", ["BTC"])
        assert result == []

    async def test_non_200_status_returns_empty(self):
        fetcher = NewsFetcher()
        fetcher._session = FakeSession(FakeResponse(status=503))
        result = await fetcher._fetch_rss("https://feed.example.com", ["BTC"])
        assert result == []

    async def test_success_parses_articles(self):
        fetcher = NewsFetcher()
        fetcher._session = FakeSession(FakeResponse(status=200, text_data=ATOM_XML))
        result = await fetcher._fetch_rss("https://feed.example.com", ["BTC", "ETH"])
        assert len(result) == 1
        assert result[0].headline == "Blockchain adoption grows"

    async def test_request_exception_returns_empty(self):
        fetcher = NewsFetcher()
        fetcher._session = FakeSession(ConnectionError("network down"))
        result = await fetcher._fetch_rss("https://feed.example.com", ["BTC"])
        assert result == []


# ---------------------------------------------------------------------------
# _fetch_cryptopanic
# ---------------------------------------------------------------------------


class TestFetchCryptopanic:
    async def test_no_session_returns_empty(self):
        fetcher = NewsFetcher()
        result = await fetcher._fetch_cryptopanic(["BTC"])
        assert result == []

    async def test_non_200_status_returns_empty(self):
        fetcher = NewsFetcher()
        fetcher._cryptopanic_key = "test-key"
        fetcher._session = FakeSession(FakeResponse(status=429))
        result = await fetcher._fetch_cryptopanic(["BTC"])
        assert result == []

    async def test_success_parses_articles(self):
        fetcher = NewsFetcher()
        fetcher._cryptopanic_key = "test-key"
        payload = {
            "results": [
                {
                    "url": "https://cryptopanic.com/a1",
                    "title": "BTC breaks resistance",
                    "published_at": "2026-01-01T12:00:00Z",
                    "currencies": [{"code": "BTC"}, {"code": "ETH"}],
                },
                {
                    "url": "https://cryptopanic.com/a2",
                    "title": "Market update",
                    "published_at": "2026-01-01T13:00:00+00:00",
                    "currencies": [],
                },
            ]
        }
        fetcher._session = FakeSession(FakeResponse(status=200, json_data=payload))
        result = await fetcher._fetch_cryptopanic(["BTC", "ETH"])

        assert len(result) == 2
        assert result[0].url == "https://cryptopanic.com/a1"
        assert result[0].entities == ["BTC", "ETH"]
        assert result[0].source == SentimentSource.NEWS

    async def test_malformed_item_is_skipped(self):
        fetcher = NewsFetcher()
        fetcher._cryptopanic_key = "test-key"
        payload = {
            "results": [
                {"url": "https://x.com/1"},  # missing published_at -> raises, skipped
                {
                    "url": "https://x.com/2",
                    "title": "Valid item",
                    "published_at": "2026-01-01T12:00:00Z",
                    "currencies": [],
                },
            ]
        }
        fetcher._session = FakeSession(FakeResponse(status=200, json_data=payload))
        result = await fetcher._fetch_cryptopanic(["BTC"])
        assert len(result) == 1
        assert result[0].headline == "Valid item"

    async def test_limits_to_50_results(self):
        fetcher = NewsFetcher()
        fetcher._cryptopanic_key = "test-key"
        payload = {
            "results": [
                {
                    "url": f"https://x.com/{i}",
                    "title": f"Item {i}",
                    "published_at": "2026-01-01T12:00:00Z",
                    "currencies": [],
                }
                for i in range(80)
            ]
        }
        fetcher._session = FakeSession(FakeResponse(status=200, json_data=payload))
        result = await fetcher._fetch_cryptopanic(["BTC"])
        assert len(result) == 50

    async def test_request_exception_returns_empty(self):
        fetcher = NewsFetcher()
        fetcher._cryptopanic_key = "test-key"
        fetcher._session = FakeSession(TimeoutError("timed out"))
        result = await fetcher._fetch_cryptopanic(["BTC"])
        assert result == []

    async def test_currencies_truncated_to_5(self):
        fetcher = NewsFetcher()
        fetcher._cryptopanic_key = "test-key"
        session = FakeSession(FakeResponse(status=200, json_data={"results": []}))
        fetcher._session = session
        await fetcher._fetch_cryptopanic(["A", "B", "C", "D", "E", "F", "G"])
        _url, params = session.calls[0]
        assert params["currencies"] == "A,B,C,D,E"


# ---------------------------------------------------------------------------
# fetch_recent (integration of gather across sources)
# ---------------------------------------------------------------------------


class TestFetchRecent:
    async def test_dedup_prevents_reseeing_urls_across_calls(self):
        """Innerhalb eines einzelnen fetch_recent-Batches erfolgt KEINE
        Deduplizierung zwischen gleichzeitig gelieferten Artikeln mit
        identischer URL - der Dedup-Filter prüft nur gegen bereits in
        früheren Aufrufen gesehene url_hashes (self._seen_urls). Dieser
        Test dokumentiert das tatsächliche, batch-übergreifende Verhalten."""
        fetcher = NewsFetcher()

        article = RawArticle(
            "https://example.com/1",
            "First",
            "",
            SentimentSource.NEWS,
            datetime.now(tz=UTC),
            [],
        )

        with patch.object(fetcher, "_fetch_rss", new=AsyncMock(side_effect=[[article], [], []])):
            first_result = await fetcher.fetch_recent(max_age_hours=4)
        assert len(first_result) == 1

        # Second call with the same article: now filtered because its
        # url_hash was recorded in _seen_urls during the first call.
        with patch.object(fetcher, "_fetch_rss", new=AsyncMock(side_effect=[[article], [], []])):
            second_result = await fetcher.fetch_recent(max_age_hours=4)
        assert second_result == []

    async def test_multiple_articles_with_distinct_urls_all_kept(self):
        fetcher = NewsFetcher()

        articles = [
            RawArticle(
                "https://example.com/1",
                "First",
                "",
                SentimentSource.NEWS,
                datetime.now(tz=UTC),
                [],
            ),
            RawArticle(
                "https://example.com/2",
                "Second",
                "",
                SentimentSource.NEWS,
                datetime.now(tz=UTC),
                [],
            ),
        ]

        with patch.object(fetcher, "_fetch_rss", new=AsyncMock(side_effect=[articles, [], []])):
            result = await fetcher.fetch_recent(max_age_hours=4)

        url_hashes = {a.url_hash for a in result}
        assert len(url_hashes) == len(result) == 2
        assert len(result) == 2  # "1" appears once, "2" once (duplicate collapsed)

    async def test_stale_articles_filtered_by_cutoff(self):
        fetcher = NewsFetcher()
        old_article = RawArticle(
            "https://example.com/old",
            "Old news",
            "",
            SentimentSource.NEWS,
            datetime.now(tz=UTC) - timedelta(hours=10),
            [],
        )
        fresh_article = RawArticle(
            "https://example.com/fresh",
            "Fresh news",
            "",
            SentimentSource.NEWS,
            datetime.now(tz=UTC),
            [],
        )

        with patch.object(
            fetcher, "_fetch_rss", new=AsyncMock(return_value=[old_article, fresh_article])
        ):
            result = await fetcher.fetch_recent(max_age_hours=4)

        headlines = [a.headline for a in result]
        assert "Fresh news" in headlines
        assert "Old news" not in headlines

    async def test_source_exception_is_logged_and_ignored(self):
        fetcher = NewsFetcher()

        async def failing(*args, **kwargs):
            raise RuntimeError("source down")

        with patch.object(fetcher, "_fetch_rss", new=failing):
            result = await fetcher.fetch_recent(max_age_hours=4)

        assert result == []

    async def test_cryptopanic_included_when_key_present(self):
        fetcher = NewsFetcher()
        fetcher._cryptopanic_key = "test-key"

        cp_article = RawArticle(
            "https://cp.example.com/1",
            "CP article",
            "",
            SentimentSource.NEWS,
            datetime.now(tz=UTC),
            [],
        )

        with (
            patch.object(fetcher, "_fetch_cryptopanic", new=AsyncMock(return_value=[cp_article])),
            patch.object(fetcher, "_fetch_rss", new=AsyncMock(return_value=[])),
        ):
            result = await fetcher.fetch_recent(max_age_hours=4)

        assert len(result) == 1
        assert result[0].headline == "CP article"

    async def test_seen_urls_window_trimmed_when_exceeding_10000(self):
        fetcher = NewsFetcher()
        fetcher._seen_urls = {f"hash{i}" for i in range(10_001)}

        with patch.object(fetcher, "_fetch_rss", new=AsyncMock(return_value=[])):
            await fetcher.fetch_recent(max_age_hours=4)

        assert len(fetcher._seen_urls) == 5_000

    async def test_results_sorted_newest_first(self):
        fetcher = NewsFetcher()
        now = datetime.now(tz=UTC)
        older = RawArticle(
            "https://example.com/older",
            "Older",
            "",
            SentimentSource.NEWS,
            now - timedelta(minutes=30),
            [],
        )
        newer = RawArticle("https://example.com/newer", "Newer", "", SentimentSource.NEWS, now, [])

        with patch.object(
            fetcher, "_fetch_rss", new=AsyncMock(side_effect=[[older, newer], [], []])
        ):
            result = await fetcher.fetch_recent(max_age_hours=4)

        assert [a.headline for a in result] == ["Newer", "Older"]

    async def test_default_currencies_used_when_none_provided(self):
        fetcher = NewsFetcher()
        captured = {}

        async def capture_currencies(feed_url, currencies):
            captured["currencies"] = currencies
            return []

        with patch.object(fetcher, "_fetch_rss", new=capture_currencies):
            await fetcher.fetch_recent(max_age_hours=4, currencies=None)

        assert captured["currencies"] == ["BTC", "ETH", "crypto", "bitcoin", "ethereum"]

    async def test_all_configured_rss_feeds_are_queried(self):
        fetcher = NewsFetcher()
        called_urls = []

        async def capture_url(feed_url, currencies):
            called_urls.append(feed_url)
            return []

        with patch.object(fetcher, "_fetch_rss", new=capture_url):
            await fetcher.fetch_recent(max_age_hours=4)

        assert called_urls == _RSS_FEEDS


# ---------------------------------------------------------------------------
# Module-level constants sanity
# ---------------------------------------------------------------------------


def test_cryptopanic_url_is_https():
    assert _CRYPTOPANIC_URL.startswith("https://")


def test_rss_feeds_non_empty():
    assert len(_RSS_FEEDS) >= 1
