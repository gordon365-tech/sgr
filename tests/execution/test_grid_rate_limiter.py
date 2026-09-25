"""
Tests für sgr.execution.grid_rate_limiter.GridRateLimiter (Phase J,
2026-09-23).

Deckt: Burst-Begrenzung, mehrere Grids/Tenants (getrennte Budgets),
Fail-Open bei Redis-Fehler, kein Effekt ohne injizierten Redis-Client,
Rate-Limit-Event-Metrik.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sgr.execution.grid_rate_limiter import GridRateLimiter

pytestmark = pytest.mark.asyncio


class FakeRedis:
    """Minimaler In-Memory-Double fuer INCR/EXPIRE - genug fuer die
    Sliding-Window-Logik von GridRateLimiter, ohne einen echten Redis-
    Server zu brauchen."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def incr(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expire_calls.append((key, seconds))


class TestGridRateLimiterBasics:
    async def test_no_redis_client_always_allows(self) -> None:
        limiter = GridRateLimiter(redis_client=None, max_calls_per_window=1)

        for _ in range(10):
            assert await limiter.acquire("binance", "gordon", "order_submit") is True

    async def test_within_budget_is_allowed(self) -> None:
        redis = FakeRedis()
        limiter = GridRateLimiter(redis, max_calls_per_window=5, window_seconds=10)

        for _ in range(5):
            assert await limiter.acquire("binance", "gordon", "order_submit") is True

    async def test_burst_beyond_budget_is_rejected(self) -> None:
        """GATE-relevant: kein unkontrolliertes Grid-Order-Bursting."""
        redis = FakeRedis()
        limiter = GridRateLimiter(redis, max_calls_per_window=3, window_seconds=10)

        results = [await limiter.acquire("binance", "gordon", "order_submit") for _ in range(6)]

        assert results == [True, True, True, False, False, False]

    async def test_expire_set_only_on_first_call_in_window(self) -> None:
        redis = FakeRedis()
        limiter = GridRateLimiter(redis, max_calls_per_window=5, window_seconds=7)

        for _ in range(3):
            await limiter.acquire("binance", "gordon", "order_submit")

        assert len(redis.expire_calls) == 1
        assert redis.expire_calls[0][1] == 7

    async def test_multiple_tenants_have_independent_budgets(self) -> None:
        """Mehrere Tenants (Gordon, Sumo) duerfen sich das Budget nicht
        gegenseitig wegnehmen."""
        redis = FakeRedis()
        limiter = GridRateLimiter(redis, max_calls_per_window=2, window_seconds=10)

        gordon_results = [
            await limiter.acquire("binance", "gordon", "order_submit") for _ in range(3)
        ]
        sumo_results = [await limiter.acquire("binance", "sumo", "order_submit") for _ in range(3)]

        assert gordon_results == [True, True, False]
        assert sumo_results == [True, True, False]  # unbeeinflusst von Gordons Budget

    async def test_multiple_grids_same_tenant_share_one_budget_per_category(self) -> None:
        """Mehrere gleichzeitige Grids DESSELBEN Tenants/derselben Exchange/
        Kategorie teilen sich bewusst EIN Budget (verhindert genau die
        Order-Multiplikation, die Phase J adressiert)."""
        redis = FakeRedis()
        limiter = GridRateLimiter(redis, max_calls_per_window=4, window_seconds=10)

        # Simuliert 2 Grids mit je 3 Fill-Versuchen auf demselben Symbol/
        # Tenant/Exchange - insgesamt 6 Versuche, Budget 4.
        results = []
        for _grid_idx in range(2):
            for _fill_idx in range(3):
                results.append(await limiter.acquire("binance", "gordon", "order_submit"))

        assert sum(results) == 4  # genau 4 erlaubt, Rest abgelehnt

    async def test_different_categories_have_independent_budgets(self) -> None:
        """order_submit und ein hypothetisches Polling teilen sich NICHT
        dasselbe Budget - eine Kategorie blockiert die andere nicht."""
        redis = FakeRedis()
        limiter = GridRateLimiter(redis, max_calls_per_window=1, window_seconds=10)

        assert await limiter.acquire("binance", "gordon", "order_submit") is True
        assert await limiter.acquire("binance", "gordon", "order_submit") is False
        assert await limiter.acquire("binance", "gordon", "polling") is True  # eigenes Budget

    async def test_redis_error_fails_open(self) -> None:
        """Fail-Safe: ein Redis-Fehler darf ein Grid nicht komplett
        stilllegen (siehe Modul-Docstring 'fail-open fuer Verfuegbarkeit')."""
        redis = AsyncMock()
        redis.incr.side_effect = RuntimeError("connection lost")
        limiter = GridRateLimiter(redis, max_calls_per_window=1)

        assert await limiter.acquire("binance", "gordon", "order_submit") is True

    async def test_rejected_call_records_metric(self, monkeypatch) -> None:
        recorded = []
        monkeypatch.setattr(
            "sgr.monitoring.metrics.record_futures_grid_rate_limit_event",
            lambda **kw: recorded.append(kw),
        )
        redis = FakeRedis()
        limiter = GridRateLimiter(redis, max_calls_per_window=1, window_seconds=10)

        await limiter.acquire("binance", "gordon", "order_submit")
        await limiter.acquire("binance", "gordon", "order_submit")  # 2nd -> rejected

        assert len(recorded) == 1
        assert recorded[0]["outcome"] == "rejected"
