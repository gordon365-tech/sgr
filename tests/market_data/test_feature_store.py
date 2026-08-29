"""
Tests für sgr.market_data.feature_store.FeatureStore.

Coverage-Ziel: 26% -> hoch. Redis wird vollständig gemockt (kein echter
Redis-Server im Sandbox-Netzwerk verfügbar), analog zum etablierten Muster
in tests/unit/test_event_bus.py (AsyncMock + patch auf `aioredis.from_url`).

Abgedeckt:
    - connect() / close() / _require_connected()
    - save(): Pipeline-Writes (timestamped + latest key), Pub/Sub-Notify
    - get_latest(): Hit, Miss (None), Deserialisierungsfehler
    - get_at(): Hit, Miss
    - get_many_latest(): Batch mit Hit/Miss/Fehler gemischt
    - invalidate()
    - get_feature_store() Singleton
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest

from sgr.core.types import ExchangeID, Symbol
from sgr.market_data import feature_store as feature_store_module
from sgr.market_data.feature_store import FeatureStore, get_feature_store
from sgr.market_data.types import FeatureSet

pytestmark = pytest.mark.asyncio


def make_feature_set(
    timeframe: str = "1h",
    timestamp: datetime | None = None,
) -> FeatureSet:
    symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)
    return FeatureSet(
        symbol=symbol,
        timestamp=timestamp or datetime(2026, 1, 1, tzinfo=UTC),
        timeframe=timeframe,
        close=Decimal("50000"),
        volume=Decimal("100"),
    )


def make_mock_config() -> MagicMock:
    config = MagicMock()
    config.redis.url = "redis://localhost:6379/0"
    return config


# ---------------------------------------------------------------------
# connect() / close() / _require_connected()
# ---------------------------------------------------------------------


class TestConnectClose:
    async def test_connect_pings_and_sets_redis(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()

        with (
            patch.object(feature_store_module, "get_config", return_value=make_mock_config()),
            patch.object(feature_store_module.aioredis, "from_url", return_value=mock_redis),
        ):
            await store.connect()

        mock_redis.ping.assert_awaited_once()
        assert store._redis is mock_redis

    async def test_close_closes_when_connected(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        store._redis = mock_redis

        await store.close()

        mock_redis.aclose.assert_awaited_once()

    async def test_close_noop_when_not_connected(self) -> None:
        store = FeatureStore()
        # Should not raise even though _redis is None.
        await store.close()

    def test_require_connected_raises_when_not_connected(self) -> None:
        store = FeatureStore()
        with pytest.raises(RuntimeError, match="not connected"):
            store._require_connected()

    def test_require_connected_returns_redis_when_connected(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        store._redis = mock_redis
        assert store._require_connected() is mock_redis


# ---------------------------------------------------------------------
# save()
# ---------------------------------------------------------------------


class TestSave:
    async def test_save_writes_pipeline_and_publishes(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        mock_pipe = AsyncMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)
        store._redis = mock_redis

        features = make_feature_set(timeframe="1h")

        await store.save(features)

        mock_redis.pipeline.assert_called_once_with(transaction=False)
        assert mock_pipe.set.call_count == 2

        # First call: timestamped key with 10x TTL.
        args1, kwargs1 = mock_pipe.set.call_args_list[0]
        assert args1[0] == features.cache_key
        assert kwargs1["ex"] == 6 * 60 * 60 * 10

        # Second call: latest key with base TTL.
        args2, kwargs2 = mock_pipe.set.call_args_list[1]
        assert args2[0] == features.latest_key
        assert kwargs2["ex"] == 6 * 60 * 60

        mock_pipe.execute.assert_awaited_once()

        mock_redis.publish.assert_awaited_once()
        channel_arg, payload_arg = mock_redis.publish.call_args[0]
        expected_channel = (
            f"feature_update:{features.symbol.exchange.value}:"
            f"{features.symbol.ccxt_symbol}:1h"
        )
        assert channel_arg == expected_channel
        assert payload_arg == features.latest_key.encode()

    async def test_save_uses_default_ttl_for_unknown_timeframe(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        mock_pipe = AsyncMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)
        store._redis = mock_redis

        features = make_feature_set(timeframe="weird_tf")

        await store.save(features)

        _, kwargs2 = mock_pipe.set.call_args_list[1]
        assert kwargs2["ex"] == 60 * 60  # _DEFAULT_TTL

    async def test_save_raises_when_not_connected(self) -> None:
        store = FeatureStore()
        with pytest.raises(RuntimeError, match="not connected"):
            await store.save(make_feature_set())


# ---------------------------------------------------------------------
# get_latest()
# ---------------------------------------------------------------------


class TestGetLatest:
    async def test_get_latest_returns_none_on_miss(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        store._redis = mock_redis

        result = await store.get_latest("binance:BTC/USDT", "1h")

        assert result is None
        mock_redis.get.assert_awaited_once_with("features:latest:binance:BTC/USDT:1h")

    async def test_get_latest_returns_feature_set_on_hit(self) -> None:
        store = FeatureStore()
        features = make_feature_set()
        payload = orjson.dumps(features.model_dump(mode="json"), option=orjson.OPT_NON_STR_KEYS)

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=payload)
        store._redis = mock_redis

        result = await store.get_latest("binance:BTC/USDT", "1h")

        assert result is not None
        assert result.close == features.close
        assert result.timeframe == "1h"

    async def test_get_latest_returns_none_on_deserialize_error(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b"not-valid-json{{{")
        store._redis = mock_redis

        result = await store.get_latest("binance:BTC/USDT", "1h")

        assert result is None


# ---------------------------------------------------------------------
# get_at()
# ---------------------------------------------------------------------


class TestGetAt:
    async def test_get_at_returns_none_on_miss(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        store._redis = mock_redis

        ts = datetime(2026, 1, 1, tzinfo=UTC)
        result = await store.get_at("binance:BTC/USDT", "1h", ts)

        assert result is None
        expected_key = f"features:binance:BTC/USDT:1h:{int(ts.timestamp())}"
        mock_redis.get.assert_awaited_once_with(expected_key)

    async def test_get_at_returns_feature_set_on_hit(self) -> None:
        store = FeatureStore()
        features = make_feature_set()
        payload = orjson.dumps(features.model_dump(mode="json"), option=orjson.OPT_NON_STR_KEYS)

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=payload)
        store._redis = mock_redis

        result = await store.get_at("binance:BTC/USDT", "1h", features.timestamp)

        assert result is not None
        assert result.close == features.close

    async def test_get_at_returns_none_on_deserialize_error(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b"garbage")
        store._redis = mock_redis

        result = await store.get_at(
            "binance:BTC/USDT", "1h", datetime(2026, 1, 1, tzinfo=UTC)
        )

        assert result is None


# ---------------------------------------------------------------------
# get_many_latest()
# ---------------------------------------------------------------------


class TestGetManyLatest:
    async def test_get_many_latest_mixed_results(self) -> None:
        store = FeatureStore()
        features = make_feature_set()
        good_payload = orjson.dumps(
            features.model_dump(mode="json"), option=orjson.OPT_NON_STR_KEYS
        )

        mock_redis = AsyncMock()
        mock_redis.mget = AsyncMock(
            return_value=[good_payload, None, b"corrupt-data"]
        )
        store._redis = mock_redis

        symbol_keys = ["binance:BTC/USDT", "binance:ETH/USDT", "binance:SOL/USDT"]
        result = await store.get_many_latest(symbol_keys, "1h")

        assert result["binance:BTC/USDT"] is not None
        assert result["binance:BTC/USDT"].close == features.close
        assert result["binance:ETH/USDT"] is None
        assert result["binance:SOL/USDT"] is None

        expected_keys = [f"features:latest:{sk}:1h" for sk in symbol_keys]
        mock_redis.mget.assert_awaited_once_with(*expected_keys)

    async def test_get_many_latest_empty_list(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        mock_redis.mget = AsyncMock(return_value=[])
        store._redis = mock_redis

        result = await store.get_many_latest([], "1h")

        assert result == {}


# ---------------------------------------------------------------------
# invalidate()
# ---------------------------------------------------------------------


class TestInvalidate:
    async def test_invalidate_deletes_key(self) -> None:
        store = FeatureStore()
        mock_redis = AsyncMock()
        store._redis = mock_redis

        await store.invalidate("binance:BTC/USDT", "1h")

        mock_redis.delete.assert_awaited_once_with("features:latest:binance:BTC/USDT:1h")


# ---------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------


class TestSingleton:
    def test_get_feature_store_returns_singleton(self) -> None:
        feature_store_module._store = None
        try:
            first = get_feature_store()
            second = get_feature_store()
            assert first is second
            assert isinstance(first, FeatureStore)
        finally:
            feature_store_module._store = None
