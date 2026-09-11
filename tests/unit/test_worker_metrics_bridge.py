"""
Tests für sgr/monitoring/worker_metrics_bridge.py - Redis-Push-Muster,
das die im sgr-worker-Prozess erzeugten Prometheus-Metriken ohne
eigenen HTTP-Port im Worker fuer /metrics scrapebar macht.

Teststrategie: analog zu tests/unit/test_ticker_cache.py und
tests/unit/test_risk_metrics_cache.py - Fail-Safe-Verhalten (kein
Redis-Client -> no-op, Redis-Fehler -> geloggt statt geworfen) sowie
das eigentliche Publish/Collect-Verhalten mit einem AsyncMock als
Redis-Stand-in.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sgr.monitoring.worker_metrics_bridge import (
    _REDIS_WORKER_SET_KEY,
    WorkerMetricsPublisher,
    _worker_key,
    collect_worker_metrics,
)


@pytest.fixture
def fake_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.set = AsyncMock()
    redis.sadd = AsyncMock()
    redis.srem = AsyncMock()
    redis.delete = AsyncMock()
    redis.smembers = AsyncMock(return_value=set())
    redis.get = AsyncMock(return_value=None)
    return redis


class TestWorkerKey:
    def test_key_includes_tenant_and_mode(self) -> None:
        key = _worker_key("gordon-tenant", "paper")
        assert key == "sgr:worker_metrics:snapshot:gordon-tenant:paper"

    def test_different_tenants_produce_different_keys(self) -> None:
        assert _worker_key("gordon-tenant", "paper") != _worker_key("sumo-tenant", "paper")


class TestWorkerMetricsPublisherWithoutRedis:
    """Regressionsschutz: ohne injizierten Redis-Client darf der Publisher
    kein Hintergrund-Task starten und beim stop() nicht crashen."""

    async def test_start_without_redis_does_not_create_task(self) -> None:
        publisher = WorkerMetricsPublisher(
            redis_client=None, tenant_id="gordon-tenant", trading_mode="paper"
        )
        await publisher.start()
        assert publisher._task is None

    async def test_stop_without_redis_does_not_raise(self) -> None:
        publisher = WorkerMetricsPublisher(
            redis_client=None, tenant_id="gordon-tenant", trading_mode="paper"
        )
        await publisher.start()
        await publisher.stop()  # darf nicht raisen


class TestWorkerMetricsPublisher:
    async def test_start_creates_background_task(self, fake_redis: AsyncMock) -> None:
        publisher = WorkerMetricsPublisher(
            redis_client=fake_redis, tenant_id="gordon-tenant", trading_mode="paper"
        )
        await publisher.start()
        try:
            assert publisher._task is not None
            assert not publisher._task.done()
        finally:
            await publisher.stop()

    async def test_publish_once_writes_registry_snapshot_with_ttl(
        self, fake_redis: AsyncMock
    ) -> None:
        publisher = WorkerMetricsPublisher(
            redis_client=fake_redis, tenant_id="gordon-tenant", trading_mode="paper"
        )

        await publisher._publish_once()

        fake_redis.set.assert_awaited_once()
        args, kwargs = fake_redis.set.call_args
        key, snapshot = args
        assert key == "sgr:worker_metrics:snapshot:gordon-tenant:paper"
        assert isinstance(snapshot, bytes)
        assert kwargs.get("ex") == 60

    async def test_publish_once_registers_key_in_worker_set(
        self, fake_redis: AsyncMock
    ) -> None:
        publisher = WorkerMetricsPublisher(
            redis_client=fake_redis, tenant_id="gordon-tenant", trading_mode="paper"
        )

        await publisher._publish_once()

        fake_redis.sadd.assert_awaited_once_with(
            _REDIS_WORKER_SET_KEY, "sgr:worker_metrics:snapshot:gordon-tenant:paper"
        )

    async def test_publish_once_swallows_redis_errors(self, fake_redis: AsyncMock) -> None:
        fake_redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
        publisher = WorkerMetricsPublisher(
            redis_client=fake_redis, tenant_id="gordon-tenant", trading_mode="paper"
        )

        # Darf NICHT raisen - ein Publish-Fehler darf den Trading-
        # Lifecycle im Worker niemals unterbrechen.
        await publisher._publish_once()

    async def test_stop_deregisters_key_and_deletes_snapshot(
        self, fake_redis: AsyncMock
    ) -> None:
        publisher = WorkerMetricsPublisher(
            redis_client=fake_redis, tenant_id="gordon-tenant", trading_mode="paper"
        )
        await publisher.start()

        await publisher.stop()

        fake_redis.srem.assert_awaited_once_with(
            _REDIS_WORKER_SET_KEY, "sgr:worker_metrics:snapshot:gordon-tenant:paper"
        )
        fake_redis.delete.assert_awaited_once_with(
            "sgr:worker_metrics:snapshot:gordon-tenant:paper"
        )

    async def test_stop_swallows_deregister_errors(self, fake_redis: AsyncMock) -> None:
        fake_redis.srem = AsyncMock(side_effect=ConnectionError("redis down"))
        publisher = WorkerMetricsPublisher(
            redis_client=fake_redis, tenant_id="gordon-tenant", trading_mode="paper"
        )
        await publisher.start()

        await publisher.stop()  # darf nicht raisen

    async def test_stop_is_idempotent(self, fake_redis: AsyncMock) -> None:
        publisher = WorkerMetricsPublisher(
            redis_client=fake_redis, tenant_id="gordon-tenant", trading_mode="paper"
        )
        await publisher.start()
        await publisher.stop()

        await publisher.stop()  # zweiter Aufruf darf nicht crashen

        assert publisher._task is None


class TestCollectWorkerMetrics:
    async def test_no_known_workers_returns_empty_bytes(self, fake_redis: AsyncMock) -> None:
        fake_redis.smembers = AsyncMock(return_value=set())

        result = await collect_worker_metrics(fake_redis)

        assert result == b""

    async def test_collects_and_joins_multiple_worker_snapshots(
        self, fake_redis: AsyncMock
    ) -> None:
        fake_redis.smembers = AsyncMock(
            return_value={
                b"sgr:worker_metrics:snapshot:gordon-tenant:paper",
                b"sgr:worker_metrics:snapshot:sumo-tenant:paper",
            }
        )

        async def fake_get(key: str) -> bytes | None:
            return {
                "sgr:worker_metrics:snapshot:gordon-tenant:paper": b"sgr_orders_total 1\n",
                "sgr:worker_metrics:snapshot:sumo-tenant:paper": b"sgr_orders_total 2\n",
            }.get(key)

        fake_redis.get = AsyncMock(side_effect=fake_get)

        result = await collect_worker_metrics(fake_redis)

        assert b"sgr_orders_total 1" in result
        assert b"sgr_orders_total 2" in result

    async def test_handles_string_keys_from_smembers(self, fake_redis: AsyncMock) -> None:
        """redis-py kann je nach decode_responses-Konfiguration str statt
        bytes liefern - beides muss funktionieren."""
        fake_redis.smembers = AsyncMock(return_value={"sgr:worker_metrics:snapshot:x:paper"})
        fake_redis.get = AsyncMock(return_value=b"sgr_orders_total 1\n")

        result = await collect_worker_metrics(fake_redis)

        assert b"sgr_orders_total 1" in result

    async def test_expired_snapshot_is_skipped_and_removed_from_set(
        self, fake_redis: AsyncMock
    ) -> None:
        """TTL abgelaufen (Worker vermutlich gestorben) -> None von GET,
        Key wird best-effort aus dem Set entfernt, keine Exception."""
        fake_redis.smembers = AsyncMock(return_value={"sgr:worker_metrics:snapshot:dead:paper"})
        fake_redis.get = AsyncMock(return_value=None)

        result = await collect_worker_metrics(fake_redis)

        assert result == b""
        fake_redis.srem.assert_awaited_once_with(
            _REDIS_WORKER_SET_KEY, "sgr:worker_metrics:snapshot:dead:paper"
        )

    async def test_single_broken_snapshot_does_not_block_others(
        self, fake_redis: AsyncMock
    ) -> None:
        fake_redis.smembers = AsyncMock(
            return_value={
                "sgr:worker_metrics:snapshot:broken:paper",
                "sgr:worker_metrics:snapshot:ok:paper",
            }
        )

        async def fake_get(key: str) -> bytes | None:
            if key == "sgr:worker_metrics:snapshot:broken:paper":
                raise ConnectionError("redis down for this key")
            return b"sgr_orders_total 1\n"

        fake_redis.get = AsyncMock(side_effect=fake_get)

        result = await collect_worker_metrics(fake_redis)

        assert b"sgr_orders_total 1" in result

    async def test_list_workers_failure_returns_empty_bytes(
        self, fake_redis: AsyncMock
    ) -> None:
        fake_redis.smembers = AsyncMock(side_effect=ConnectionError("redis down"))

        result = await collect_worker_metrics(fake_redis)

        assert result == b""
