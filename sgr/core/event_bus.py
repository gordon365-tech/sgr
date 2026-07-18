"""
SGR Event Bus
=============
Redis Streams-backed async event bus.

Design decisions:
- Redis Streams chosen over Kafka for MVP simplicity.
  Migration path: replace RedisEventBus with KafkaEventBus,
  keeping the same EventBus protocol.
- Consumer groups: each module has its own group → replay possible
- Events are serialized as JSON (orjson for speed)
- Dead letter stream for failed processing
- Max stream length enforced (no unbounded growth)

Architecture:
    Publisher → Redis Stream → Consumer Group → Handler
                             ↘ Dead Letter Stream (on failure)

Stream naming: sgr:{event_type} e.g. sgr:candle, sgr:signal, sgr:order
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import orjson
import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.core.types import BaseEvent

log = get_logger(__name__)

T = TypeVar("T", bound=BaseEvent)
HandlerFn = Callable[[BaseEvent], Coroutine[Any, Any, None]]

# Stream naming convention
_STREAM_PREFIX = "sgr"
_DLQ_SUFFIX = "dlq"
_MAX_STREAM_LEN = 10_000  # ~10k events per stream, then FIFO trim


def _stream_name(event_type: str) -> str:
    return f"{_STREAM_PREFIX}:{event_type}"


def _dlq_name(event_type: str) -> str:
    return f"{_STREAM_PREFIX}:{event_type}:{_DLQ_SUFFIX}"


class EventBus:
    """
    Async event bus backed by Redis Streams.

    Lifecycle:
        bus = EventBus()
        await bus.connect()
        ...
        await bus.close()

    Or use as async context manager:
        async with EventBus() as bus:
            await bus.publish(event)
    """

    def __init__(self) -> None:
        self._redis: Redis | None = None
        self._handlers: dict[str, list[HandlerFn]] = {}
        self._consumer_tasks: list[asyncio.Task[None]] = []
        self._running = False

    async def connect(self) -> None:
        config = get_config()
        self._redis = aioredis.from_url(
            config.redis.url,
            encoding="utf-8",
            decode_responses=False,  # We handle bytes ourselves
            max_connections=config.redis.max_connections,
        )
        # Verify connection
        await self._redis.ping()
        self._running = True
        log.info("event_bus.connected", url=config.redis.host)

    async def close(self) -> None:
        self._running = False
        # Cancel consumer tasks
        for task in self._consumer_tasks:
            task.cancel()
        if self._consumer_tasks:
            await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
        if self._redis:
            await self._redis.aclose()
        log.info("event_bus.closed")

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[EventBus]:
        await self.connect()
        try:
            yield self
        finally:
            await self.close()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, event: BaseEvent) -> str:
        """
        Publish an event to the appropriate stream.
        Returns the Redis message ID.

        Stream is determined by event class name (snake_case).
        e.g. CandleEvent → sgr:candle_event
        """
        if self._redis is None:
            raise RuntimeError("EventBus not connected. Call connect() first.")

        event_type = _to_stream_name(type(event).__name__)
        stream = _stream_name(event_type)

        # Serialize with orjson (handles Decimal, datetime, UUID natively)
        payload = orjson.dumps(
            event.model_dump(mode="json"),
            option=orjson.OPT_NON_STR_KEYS,
        )

        message_id = await self._redis.xadd(
            stream,
            {"data": payload, "type": type(event).__name__},
            maxlen=_MAX_STREAM_LEN,
            approximate=True,
        )

        log.debug(
            "event_bus.published",
            stream=stream,
            event_type=type(event).__name__,
            event_id=str(event.event_id),
            message_id=message_id,
        )

        return str(message_id)

    # ------------------------------------------------------------------
    # Subscribing
    # ------------------------------------------------------------------

    def subscribe(
        self,
        event_class: type[T],
        handler: HandlerFn,
        consumer_group: str,
        consumer_name: str,
    ) -> None:
        """
        Register a handler for an event type.
        Handlers run in consumer groups (independent processing).
        """
        event_type = _to_stream_name(event_class.__name__)
        stream = _stream_name(event_type)
        key = f"{stream}:{consumer_group}"

        if key not in self._handlers:
            self._handlers[key] = []
        self._handlers[key].append(handler)

        # Schedule consumer task
        task = asyncio.create_task(
            self._consume(
                stream=stream,
                event_class=event_class,
                handler=handler,
                consumer_group=consumer_group,
                consumer_name=consumer_name,
            ),
            name=f"consumer:{consumer_group}:{event_type}",
        )
        self._consumer_tasks.append(task)

        log.info(
            "event_bus.subscribed",
            stream=stream,
            consumer_group=consumer_group,
            consumer_name=consumer_name,
        )

    async def _consume(
        self,
        stream: str,
        event_class: type[T],
        handler: HandlerFn,
        consumer_group: str,
        consumer_name: str,
    ) -> None:
        """
        Consumer loop for a stream + group.
        Runs until bus is closed.
        Handles:
        - Consumer group creation (idempotent)
        - Message acknowledgment
        - Retry with backoff on transient errors
        - Dead letter queue after max retries
        """
        if self._redis is None:
            return

        # Create consumer group (idempotent)
        try:
            await self._redis.xgroup_create(stream, consumer_group, id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        log.info(
            "event_bus.consumer.started",
            stream=stream,
            consumer_group=consumer_group,
        )

        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=consumer_group,
                    consumername=consumer_name,
                    streams={stream: ">"},
                    count=10,
                    block=1000,  # 1s blocking read
                )

                if not messages:
                    continue

                for _stream_name_bytes, stream_messages in messages:
                    for message_id, fields in stream_messages:
                        await self._process_message(
                            stream=stream,
                            message_id=message_id,
                            fields=fields,
                            event_class=event_class,
                            handler=handler,
                            consumer_group=consumer_group,
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(
                    "event_bus.consumer.error",
                    stream=stream,
                    consumer_group=consumer_group,
                    error=str(e),
                )
                await asyncio.sleep(1)  # Brief pause before retry

    async def _process_message(
        self,
        stream: str,
        message_id: bytes,
        fields: dict[bytes, bytes],
        event_class: type[T],
        handler: HandlerFn,
        consumer_group: str,
        max_retries: int = 3,
    ) -> None:
        if self._redis is None:
            return

        for attempt in range(max_retries):
            try:
                raw_data = fields.get(b"data", b"{}")
                data = orjson.loads(raw_data)
                event = event_class.model_validate(data)

                await handler(event)

                # ACK on success
                await self._redis.xack(stream, consumer_group, message_id)
                return

            except Exception as e:
                if attempt == max_retries - 1:
                    # Move to DLQ
                    log.error(
                        "event_bus.message.dlq",
                        stream=stream,
                        message_id=str(message_id),
                        error=str(e),
                        attempts=max_retries,
                    )
                    event_type = stream.split(":")[-1]
                    dlq = _dlq_name(event_type)
                    if self._redis:
                        await self._redis.xadd(
                            dlq,
                            {
                                "original_stream": stream,
                                "message_id": str(message_id),
                                "error": str(e),
                                "data": fields.get(b"data", b"{}"),
                            },
                            maxlen=1_000,
                        )
                    await self._redis.xack(stream, consumer_group, message_id)
                else:
                    backoff = 0.1 * (2**attempt)
                    await asyncio.sleep(backoff)

    # ------------------------------------------------------------------
    # Simple Pub/Sub (for low-latency, non-persistent events)
    # ------------------------------------------------------------------

    async def get_pubsub(self) -> PubSub:
        """
        For ultra-low-latency events that don't need persistence.
        e.g. live price ticks for the dashboard.
        """
        if self._redis is None:
            raise RuntimeError("EventBus not connected.")
        return self._redis.pubsub()

    async def publish_realtime(self, channel: str, data: dict[str, Any]) -> None:
        """Publish to pub/sub channel (no persistence, fire-and-forget)."""
        if self._redis is None:
            raise RuntimeError("EventBus not connected.")
        await self._redis.publish(channel, orjson.dumps(data))


def _to_stream_name(class_name: str) -> str:
    """CandleEvent → candle_event"""
    import re

    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", class_name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


# ---------------------------------------------------------------------------
# Global singleton (initialized at app startup)
# ---------------------------------------------------------------------------

_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
