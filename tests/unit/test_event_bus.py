"""
Tests für sgr.core.event_bus – EventBus (Redis Streams-backed).
Coverage-Ziel: ~34% -> hoch (Redis wird vollständig gemockt,
kein echter Redis-Server im Sandbox-Netzwerk verfügbar).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sgr.core import event_bus as event_bus_module
from sgr.core.event_bus import (
    EventBus,
    _dlq_name,
    _stream_name,
    _to_stream_name,
    get_event_bus,
)
from sgr.core.types import BaseEvent, CandleEvent

# ===========================================================================
# Helpers
# ===========================================================================


class DummyEvent(BaseEvent):
    source: str = "test"
    payload: str = "x"


def make_mock_config() -> MagicMock:
    config = MagicMock()
    config.redis.url = "redis://localhost:6379/0"
    config.redis.host = "localhost"
    config.redis.max_connections = 10
    return config


# ===========================================================================
# Stream naming helpers
# ===========================================================================


class TestStreamNaming:
    def test_stream_name_format(self) -> None:
        assert _stream_name("candle_event") == "sgr:candle_event"

    def test_dlq_name_format(self) -> None:
        assert _dlq_name("candle_event") == "sgr:candle_event:dlq"

    def test_to_stream_name_simple(self) -> None:
        assert _to_stream_name("CandleEvent") == "candle_event"

    def test_to_stream_name_multi_word(self) -> None:
        assert _to_stream_name("RiskApprovedEvent") == "risk_approved_event"

    def test_to_stream_name_with_acronym_like_pattern(self) -> None:
        assert _to_stream_name("OrderPartiallyFilledEvent") == "order_partially_filled_event"


# ===========================================================================
# Connect / Close / Lifespan
# ===========================================================================


class TestConnectCloseLifespan:
    @pytest.mark.asyncio
    async def test_connect_sets_up_redis_and_pings(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()

        with (
            patch("sgr.core.event_bus.get_config", return_value=make_mock_config()),
            patch("sgr.core.event_bus.aioredis.from_url", return_value=mock_redis),
        ):
            await bus.connect()

        mock_redis.ping.assert_awaited_once()
        assert bus._running is True
        assert bus._redis is mock_redis

    @pytest.mark.asyncio
    async def test_close_without_connect_is_safe(self) -> None:
        bus = EventBus()
        await bus.close()
        assert bus._running is False

    @pytest.mark.asyncio
    async def test_close_cancels_consumer_tasks_and_closes_redis(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        bus._redis = mock_redis
        bus._running = True

        async def never_ending() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(never_ending())
        bus._consumer_tasks.append(task)

        await bus.close()

        assert bus._running is False
        assert task.cancelled() or task.done()
        mock_redis.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_connects_and_closes(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()

        with (
            patch("sgr.core.event_bus.get_config", return_value=make_mock_config()),
            patch("sgr.core.event_bus.aioredis.from_url", return_value=mock_redis),
        ):
            async with bus.lifespan() as active_bus:
                assert active_bus is bus
                assert bus._running is True

        assert bus._running is False
        mock_redis.aclose.assert_awaited_once()


# ===========================================================================
# Publishing
# ===========================================================================


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_raises_when_not_connected(self) -> None:
        bus = EventBus()
        event = DummyEvent(timestamp=datetime.now(tz=UTC))

        with pytest.raises(RuntimeError, match="not connected"):
            await bus.publish(event)

    @pytest.mark.asyncio
    async def test_publish_sends_to_correct_stream(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock(return_value="1234-0")
        bus._redis = mock_redis

        event = DummyEvent(timestamp=datetime.now(tz=UTC))
        message_id = await bus.publish(event)

        assert message_id == "1234-0"
        mock_redis.xadd.assert_awaited_once()
        args, kwargs = mock_redis.xadd.call_args
        assert args[0] == "sgr:dummy_event"
        assert kwargs["maxlen"] == 10_000
        assert kwargs["approximate"] is True
        assert args[1]["type"] == "DummyEvent"

    @pytest.mark.asyncio
    async def test_publish_candle_event_uses_correct_stream(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock(return_value="1-0")
        bus._redis = mock_redis

        from sgr.core.types import AssetClass, Candle, ExchangeID, Symbol

        candle = Candle(
            symbol=Symbol(
                base="BTC", quote="USDT", exchange=ExchangeID.PIONEX, asset_class=AssetClass.SPOT
            ),
            timeframe="1h",
            timestamp=datetime.now(tz=UTC),
            open=1,
            high=2,
            low=0.5,
            close=1.5,
            volume=100,
        )
        event = CandleEvent(timestamp=datetime.now(tz=UTC), candle=candle)

        await bus.publish(event)

        stream_arg = mock_redis.xadd.call_args[0][0]
        assert stream_arg == "sgr:candle_event"


# ===========================================================================
# Subscribing
# ===========================================================================


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_registers_handler_and_schedules_task(self) -> None:
        bus = EventBus()
        bus._redis = AsyncMock()

        async def handler(event: BaseEvent) -> None:
            pass

        with patch.object(bus, "_consume", AsyncMock()):
            bus.subscribe(
                DummyEvent,
                handler,
                consumer_group="test_group",
                consumer_name="consumer1",
            )

        key = "sgr:dummy_event:test_group"
        assert key in bus._handlers
        assert handler in bus._handlers[key]
        assert len(bus._consumer_tasks) == 1

        # Cleanup pending task to avoid warnings
        for t in bus._consumer_tasks:
            t.cancel()
        await asyncio.gather(*bus._consumer_tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_subscribe_multiple_handlers_same_group(self) -> None:
        bus = EventBus()
        bus._redis = AsyncMock()

        async def handler1(event: BaseEvent) -> None:
            pass

        async def handler2(event: BaseEvent) -> None:
            pass

        with patch.object(bus, "_consume", AsyncMock()):
            bus.subscribe(DummyEvent, handler1, "group_a", "c1")
            bus.subscribe(DummyEvent, handler2, "group_a", "c2")

        key = "sgr:dummy_event:group_a"
        assert len(bus._handlers[key]) == 2

        for t in bus._consumer_tasks:
            t.cancel()
        await asyncio.gather(*bus._consumer_tasks, return_exceptions=True)


# ===========================================================================
# _consume
# ===========================================================================


class TestConsume:
    @pytest.mark.asyncio
    async def test_consume_returns_immediately_when_redis_none(self) -> None:
        bus = EventBus()
        bus._redis = None
        # Should return without raising
        await bus._consume(
            stream="sgr:dummy_event",
            event_class=DummyEvent,
            handler=AsyncMock(),
            consumer_group="g",
            consumer_name="c",
        )

    @pytest.mark.asyncio
    async def test_consume_creates_group_and_processes_message(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock()
        bus._redis = mock_redis
        bus._running = True

        call_count = 0

        async def fake_xreadgroup(**kwargs: object) -> list:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [
                    (
                        b"sgr:dummy_event",
                        [(b"1-0", {b"data": b'{"foo": "bar"}'})],
                    )
                ]
            bus._running = False
            return []

        mock_redis.xreadgroup = fake_xreadgroup

        with patch.object(bus, "_process_message", AsyncMock()) as mock_process:
            await bus._consume(
                stream="sgr:dummy_event",
                event_class=DummyEvent,
                handler=AsyncMock(),
                consumer_group="g",
                consumer_name="c",
            )

        mock_redis.xgroup_create.assert_awaited_once_with(
            "sgr:dummy_event", "g", id="0", mkstream=True
        )
        mock_process.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_consume_handles_busygroup_error_silently(self) -> None:
        import redis.asyncio as aioredis

        bus = EventBus()
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock(
            side_effect=aioredis.ResponseError("BUSYGROUP Consumer Group name already exists")
        )
        mock_redis.xreadgroup = AsyncMock(return_value=[])
        bus._redis = mock_redis
        bus._running = False  # Exit loop immediately after group creation

        # Should not raise
        await bus._consume(
            stream="sgr:dummy_event",
            event_class=DummyEvent,
            handler=AsyncMock(),
            consumer_group="g",
            consumer_name="c",
        )

    @pytest.mark.asyncio
    async def test_consume_reraises_non_busygroup_response_error(self) -> None:
        import redis.asyncio as aioredis

        bus = EventBus()
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock(side_effect=aioredis.ResponseError("Some other error"))
        bus._redis = mock_redis
        bus._running = True

        with pytest.raises(aioredis.ResponseError, match="Some other error"):
            await bus._consume(
                stream="sgr:dummy_event",
                event_class=DummyEvent,
                handler=AsyncMock(),
                consumer_group="g",
                consumer_name="c",
            )

    @pytest.mark.asyncio
    async def test_consume_breaks_on_cancelled_error(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock()
        mock_redis.xreadgroup = AsyncMock(side_effect=asyncio.CancelledError())
        bus._redis = mock_redis
        bus._running = True

        # Should exit cleanly without propagating CancelledError further up
        await bus._consume(
            stream="sgr:dummy_event",
            event_class=DummyEvent,
            handler=AsyncMock(),
            consumer_group="g",
            consumer_name="c",
        )

    @pytest.mark.asyncio
    async def test_consume_logs_and_retries_on_generic_exception(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock()
        bus._redis = mock_redis
        bus._running = True

        call_count = 0

        async def fake_xreadgroup(**kwargs: object) -> list:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            bus._running = False
            return []

        mock_redis.xreadgroup = fake_xreadgroup

        with patch("sgr.core.event_bus.asyncio.sleep", AsyncMock()) as mock_sleep:
            await bus._consume(
                stream="sgr:dummy_event",
                event_class=DummyEvent,
                handler=AsyncMock(),
                consumer_group="g",
                consumer_name="c",
            )

        mock_sleep.assert_awaited_with(1)

    @pytest.mark.asyncio
    async def test_consume_no_messages_continues_loop(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock()
        bus._redis = mock_redis
        bus._running = True

        call_count = 0

        async def fake_xreadgroup(**kwargs: object) -> list:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                bus._running = False
            return []

        mock_redis.xreadgroup = fake_xreadgroup

        await bus._consume(
            stream="sgr:dummy_event",
            event_class=DummyEvent,
            handler=AsyncMock(),
            consumer_group="g",
            consumer_name="c",
        )

        assert call_count >= 2


# ===========================================================================
# _process_message
# ===========================================================================


class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_process_message_returns_when_redis_none(self) -> None:
        bus = EventBus()
        bus._redis = None
        # Should return without raising
        await bus._process_message(
            stream="sgr:dummy_event",
            message_id=b"1-0",
            fields={b"data": b"{}"},
            event_class=DummyEvent,
            handler=AsyncMock(),
            consumer_group="g",
        )

    @pytest.mark.asyncio
    async def test_process_message_success_acks(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        bus._redis = mock_redis
        handler = AsyncMock()

        fields = {
            b"data": (
                b'{"event_id": "11111111-1111-1111-1111-111111111111", '
                b'"timestamp": "2024-01-01T00:00:00Z", "source": "test", "payload": "hi"}'
            )
        }

        await bus._process_message(
            stream="sgr:dummy_event",
            message_id=b"1-0",
            fields=fields,
            event_class=DummyEvent,
            handler=handler,
            consumer_group="g",
        )

        handler.assert_awaited_once()
        published_event = handler.call_args[0][0]
        assert published_event.payload == "hi"
        mock_redis.xack.assert_awaited_once_with("sgr:dummy_event", "g", b"1-0")

    @pytest.mark.asyncio
    async def test_process_message_retries_then_succeeds(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        bus._redis = mock_redis

        call_count = 0

        async def flaky_handler(event: BaseEvent) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("transient")

        fields = {
            b"data": (
                b'{"event_id": "11111111-1111-1111-1111-111111111111", '
                b'"timestamp": "2024-01-01T00:00:00Z", "source": "test", "payload": "hi"}'
            )
        }

        with patch("sgr.core.event_bus.asyncio.sleep", AsyncMock()):
            await bus._process_message(
                stream="sgr:dummy_event",
                message_id=b"1-0",
                fields=fields,
                event_class=DummyEvent,
                handler=flaky_handler,
                consumer_group="g",
                max_retries=3,
            )

        assert call_count == 2
        mock_redis.xack.assert_awaited_once_with("sgr:dummy_event", "g", b"1-0")

    @pytest.mark.asyncio
    async def test_process_message_exhausts_retries_moves_to_dlq(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        bus._redis = mock_redis

        async def always_fails(event: BaseEvent) -> None:
            raise ValueError("permanent failure")

        fields = {
            b"data": (
                b'{"event_id": "11111111-1111-1111-1111-111111111111", '
                b'"timestamp": "2024-01-01T00:00:00Z", "source": "test", "payload": "hi"}'
            )
        }

        with patch("sgr.core.event_bus.asyncio.sleep", AsyncMock()):
            await bus._process_message(
                stream="sgr:dummy_event",
                message_id=b"42-0",
                fields=fields,
                event_class=DummyEvent,
                handler=always_fails,
                consumer_group="g",
                max_retries=2,
            )

        mock_redis.xadd.assert_awaited_once()
        dlq_args, dlq_kwargs = mock_redis.xadd.call_args
        assert dlq_args[0] == "sgr:dummy_event:dlq"
        assert dlq_args[1]["original_stream"] == "sgr:dummy_event"
        assert dlq_args[1]["message_id"] == str(b"42-0")
        assert dlq_kwargs["maxlen"] == 1_000

        mock_redis.xack.assert_awaited_once_with("sgr:dummy_event", "g", b"42-0")

    @pytest.mark.asyncio
    async def test_process_message_malformed_json_goes_to_dlq(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        bus._redis = mock_redis

        fields = {b"data": b"not-valid-json{{{"}

        with patch("sgr.core.event_bus.asyncio.sleep", AsyncMock()):
            await bus._process_message(
                stream="sgr:dummy_event",
                message_id=b"7-0",
                fields=fields,
                event_class=DummyEvent,
                handler=AsyncMock(),
                consumer_group="g",
                max_retries=1,
            )

        mock_redis.xadd.assert_awaited_once()
        mock_redis.xack.assert_awaited_once_with("sgr:dummy_event", "g", b"7-0")

    @pytest.mark.asyncio
    async def test_process_message_missing_data_field_defaults_empty(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        bus._redis = mock_redis

        # No b"data" key at all -> defaults to b"{}" -> validation fails -> DLQ
        fields: dict[bytes, bytes] = {}

        with patch("sgr.core.event_bus.asyncio.sleep", AsyncMock()):
            await bus._process_message(
                stream="sgr:dummy_event",
                message_id=b"8-0",
                fields=fields,
                event_class=DummyEvent,
                handler=AsyncMock(),
                consumer_group="g",
                max_retries=1,
            )

        mock_redis.xadd.assert_awaited_once()


# ===========================================================================
# Pub/Sub (simple, non-persistent)
# ===========================================================================


class TestPubSub:
    @pytest.mark.asyncio
    async def test_get_pubsub_raises_when_not_connected(self) -> None:
        bus = EventBus()
        with pytest.raises(RuntimeError, match="not connected"):
            await bus.get_pubsub()

    @pytest.mark.asyncio
    async def test_get_pubsub_returns_redis_pubsub(self) -> None:
        bus = EventBus()
        mock_redis = MagicMock()
        mock_pubsub = MagicMock()
        mock_redis.pubsub.return_value = mock_pubsub
        bus._redis = mock_redis

        result = await bus.get_pubsub()

        assert result is mock_pubsub
        mock_redis.pubsub.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_realtime_raises_when_not_connected(self) -> None:
        bus = EventBus()
        with pytest.raises(RuntimeError, match="not connected"):
            await bus.publish_realtime("channel", {"foo": "bar"})

    @pytest.mark.asyncio
    async def test_publish_realtime_publishes_serialized_data(self) -> None:
        bus = EventBus()
        mock_redis = AsyncMock()
        bus._redis = mock_redis

        await bus.publish_realtime("price_ticks", {"symbol": "BTC/USDT", "price": 50000})

        mock_redis.publish.assert_awaited_once()
        args = mock_redis.publish.call_args[0]
        assert args[0] == "price_ticks"
        assert b"BTC/USDT" in args[1]


# ===========================================================================
# Singleton
# ===========================================================================


class TestSingleton:
    def test_get_event_bus_returns_singleton(self) -> None:
        event_bus_module._bus = None
        try:
            first = get_event_bus()
            second = get_event_bus()
            assert first is second
            assert isinstance(first, EventBus)
        finally:
            event_bus_module._bus = None
