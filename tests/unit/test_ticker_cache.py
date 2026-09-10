"""
Tests für sgr/market_data/ticker_cache.py - Redis-backed Cache für rohe
Ticker-Snapshots (sgr-api Read-Only-Zugriff, kein eigener ExchangePool
mehr in der API seit der sgr-api/sgr-worker-Trennung).

Teststrategie: analog zu tests/unit/test_risk_metrics_cache.py - selbes
Fail-Safe-Prinzip (kein Redis-Client -> no-op, Redis-/Exchange-Fehler ->
geloggt, nie geworfen; fehlender/abgelaufener Wert -> None = "noch kein
Ticker verfügbar"). Kein Tenant-Scoping: Ticker sind marktweit gültig,
nicht tenant-spezifisch (anders als RiskMetrics/Kill Switch).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from sgr.exchanges.base import TickerData
from sgr.market_data.ticker_cache import publish_ticker, read_ticker_from_redis


@pytest.fixture
def sample_ticker() -> TickerData:
    return TickerData(
        symbol="BTC/USDT",
        bid=Decimal("50000.00"),
        ask=Decimal("50010.00"),
        last=Decimal("50005.00"),
        volume_24h=Decimal("1234.5"),
        change_24h_pct=1.2,
        timestamp=datetime(2026, 9, 10, tzinfo=UTC),
    )


@pytest.fixture
def fake_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


class TestPublishTickerWithoutRedis:
    """Regressionsschutz: ohne injizierten Redis-Client darf
    publish_ticker() ein reines no-op sein."""

    async def test_publish_without_redis_does_not_raise(
        self, sample_ticker: TickerData
    ) -> None:
        await publish_ticker(None, sample_ticker)


class TestPublishTicker:
    async def test_publish_writes_serialized_ticker_with_ttl(
        self, fake_redis: AsyncMock, sample_ticker: TickerData
    ) -> None:
        await publish_ticker(fake_redis, sample_ticker)

        fake_redis.set.assert_awaited_once()
        args, kwargs = fake_redis.set.call_args
        key, payload = args
        assert key == "sgr:market:ticker:BTC/USDT"
        assert kwargs.get("ex") == 60

        data = json.loads(payload)
        assert data["symbol"] == "BTC/USDT"
        assert data["bid"] == "50000.00"
        assert data["ask"] == "50010.00"
        assert data["last"] == "50005.00"
        assert data["volume_24h"] == "1234.5"
        assert data["change_24h_pct"] == 1.2
        assert data["timestamp"] == sample_ticker.timestamp.isoformat()

    async def test_publish_uses_correct_key_per_symbol(
        self, fake_redis: AsyncMock
    ) -> None:
        """Zwei verschiedene Symbole duerfen sich niemals denselben
        Redis-Key teilen."""
        eth_ticker = TickerData(
            symbol="ETH/USDT",
            bid=Decimal("3000"),
            ask=Decimal("3001"),
            last=Decimal("3000.5"),
            volume_24h=Decimal("500"),
            change_24h_pct=-0.5,
            timestamp=datetime(2026, 9, 10, tzinfo=UTC),
        )

        await publish_ticker(fake_redis, eth_ticker)

        key, _payload = fake_redis.set.call_args.args
        assert key == "sgr:market:ticker:ETH/USDT"


class TestPublishTickerFailSafe:
    async def test_publish_swallows_redis_errors(
        self, sample_ticker: TickerData
    ) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(side_effect=ConnectionError("redis down"))

        # Darf NICHT raisen - ein Redis-Fehler darf den Market-Data-
        # Poll-Loop niemals unterbrechen.
        await publish_ticker(redis, sample_ticker)


class TestReadTickerFromRedis:
    async def test_returns_parsed_ticker_when_present(
        self, fake_redis: AsyncMock
    ) -> None:
        fake_redis.get = AsyncMock(
            return_value=json.dumps({"symbol": "BTC/USDT", "bid": "50000.00"})
        )

        result = await read_ticker_from_redis(fake_redis, "BTC/USDT")

        assert result == {"symbol": "BTC/USDT", "bid": "50000.00"}

    async def test_returns_none_when_no_ticker_written_yet(
        self, fake_redis: AsyncMock
    ) -> None:
        fake_redis.get = AsyncMock(return_value=None)

        result = await read_ticker_from_redis(fake_redis, "BTC/USDT")

        assert result is None

    async def test_returns_none_on_redis_error_fail_safe(
        self, fake_redis: AsyncMock
    ) -> None:
        """Fail-safe: Redis-Fehler -> None ('noch kein Ticker
        verfügbar'), kein Absturz."""
        fake_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))

        result = await read_ticker_from_redis(fake_redis, "BTC/USDT")

        assert result is None

    async def test_uses_correct_key_per_symbol(self, fake_redis: AsyncMock) -> None:
        await read_ticker_from_redis(fake_redis, "ETH/USDT")

        fake_redis.get.assert_awaited_once_with("sgr:market:ticker:ETH/USDT")
