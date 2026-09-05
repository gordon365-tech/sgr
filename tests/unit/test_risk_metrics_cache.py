"""
Tests für sgr/risk/metrics_cache.py - Redis-backed Cache der zuletzt
berechneten RiskMetrics (sgr-api Read-Only-Zugriff, kein eigener
RiskEngine mehr in der API seit der sgr-api/sgr-worker-Trennung).

Teststrategie: analog zu tests/unit/test_kill_switch_redis_sync.py -
selbes Fail-Safe-Prinzip (kein Redis-Client -> no-op, Redis-Fehler ->
geloggt, nie geworfen; fehlender/abgelaufener Wert -> None = "unbekannt").
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from sgr.core.types import RiskMetrics, TradingMode
from sgr.risk.metrics_cache import publish_risk_metrics, read_risk_metrics_from_redis


@pytest.fixture
def sample_metrics() -> RiskMetrics:
    return RiskMetrics(
        timestamp=datetime(2026, 9, 5, tzinfo=UTC),
        portfolio_value=Decimal("10000.00"),
        daily_pnl=Decimal("150.00"),
        daily_pnl_pct=0.015,
        drawdown_from_peak=0.02,
        var_95=0.03,
        expected_shortfall=0.04,
        portfolio_heat=0.25,
        active_positions=3,
        correlation_exposure=0.1,
        gross_leverage=1.5,
    )


@pytest.fixture
def fake_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


class TestPublishRiskMetricsWithoutRedis:
    """Regressionsschutz: ohne injizierten Redis-Client (Default in
    RiskEngine) darf publish_risk_metrics() ein reines no-op sein."""

    async def test_publish_without_redis_does_not_raise(
        self, sample_metrics: RiskMetrics
    ) -> None:
        await publish_risk_metrics(None, TradingMode.PAPER, sample_metrics)


class TestPublishRiskMetrics:
    async def test_publish_writes_serialized_metrics_with_ttl(
        self, fake_redis: AsyncMock, sample_metrics: RiskMetrics
    ) -> None:
        await publish_risk_metrics(fake_redis, TradingMode.PAPER, sample_metrics)

        fake_redis.set.assert_awaited_once()
        args, kwargs = fake_redis.set.call_args
        key, payload = args
        assert key == "sgr:risk:metrics:paper"
        assert kwargs.get("ex") == 120

        data = json.loads(payload)
        assert data["portfolio_value"] == "10000.00"
        assert data["var_95"] == 0.03
        assert data["active_positions"] == 3

    async def test_publish_uses_correct_key_per_trading_mode(
        self, fake_redis: AsyncMock, sample_metrics: RiskMetrics
    ) -> None:
        """Paper und Live duerfen sich niemals denselben Redis-Key teilen -
        dieselbe Anforderung wie beim Kill Switch."""
        await publish_risk_metrics(fake_redis, TradingMode.LIVE, sample_metrics)

        key, _payload = fake_redis.set.call_args.args
        assert key == "sgr:risk:metrics:live"


class TestPublishRiskMetricsFailSafe:
    async def test_publish_swallows_redis_errors(
        self, sample_metrics: RiskMetrics
    ) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(side_effect=ConnectionError("redis down"))

        # Darf NICHT raisen - ein Redis-Fehler darf die Risk-Bewertung
        # (evaluate()) niemals unterbrechen.
        await publish_risk_metrics(redis, TradingMode.PAPER, sample_metrics)


class TestReadRiskMetricsFromRedis:
    async def test_returns_parsed_metrics_when_present(
        self, fake_redis: AsyncMock
    ) -> None:
        fake_redis.get = AsyncMock(
            return_value=json.dumps({"portfolio_value": "10000.00", "var_95": 0.03})
        )

        result = await read_risk_metrics_from_redis(fake_redis, TradingMode.PAPER)

        assert result == {"portfolio_value": "10000.00", "var_95": 0.03}

    async def test_returns_none_when_no_metrics_written_yet(
        self, fake_redis: AsyncMock
    ) -> None:
        fake_redis.get = AsyncMock(return_value=None)

        result = await read_risk_metrics_from_redis(fake_redis, TradingMode.PAPER)

        assert result is None

    async def test_returns_none_on_redis_error_fail_safe(
        self, fake_redis: AsyncMock
    ) -> None:
        """Fail-safe: Redis-Fehler -> None ('unbekannt'), kein Absturz.
        Der Aufrufer (Risk-Router) muss dies als 'Status unbekannt'
        behandeln, nicht als 'kein Risiko'."""
        fake_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))

        result = await read_risk_metrics_from_redis(fake_redis, TradingMode.PAPER)

        assert result is None

    async def test_uses_correct_key_per_trading_mode(self, fake_redis: AsyncMock) -> None:
        await read_risk_metrics_from_redis(fake_redis, TradingMode.LIVE)

        fake_redis.get.assert_awaited_once_with("sgr:risk:metrics:live")
