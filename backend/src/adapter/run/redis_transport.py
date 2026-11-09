"""使用 Redis Pub/Sub 传输 Run 实时通知，不保存可重放事件。"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

from pydantic import TypeAdapter
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from domain.run.model import (
    MAX_RUN_SSE_EVENT_BYTES,
    RunEvent,
    RunInterruptedEvent,
    RunInterruptedPayload,
    RunPersistedEvent,
    RunPersistedEventPayload,
    RunStreamEvent,
    RunTextDeltaEvent,
    RunTextDeltaPayload,
)
from domain.run.ports import RunEventSubscription, RunEventTransport

MAX_REDIS_EVENT_BYTES = MAX_RUN_SSE_EVENT_BYTES
MAX_SUBSCRIPTION_EVENTS = 256
MAX_SUBSCRIPTION_BYTES = 1024 * 1024
MAX_FAILED_EPOCHS = 256
FAILED_EPOCH_TTL_SECONDS = 3600.0
MAX_SSE_EVENT_BYTES = MAX_RUN_SSE_EVENT_BYTES
SSE_PREFIX_RESERVE = len(b"event: text_delta\nid: 99999999999999999999\ndata: \n\n")
_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(cast(Any, RunStreamEvent))


class _RedisRunSubscription(RunEventSubscription):
    def __init__(self, pubsub: PubSub) -> None:
        self._pubsub = pubsub
        self._queue: asyncio.Queue[tuple[RunStreamEvent, int]] = asyncio.Queue(
            maxsize=MAX_SUBSCRIPTION_EVENTS
        )
        self._queued_bytes = 0
        self._overflow = asyncio.Event()
        self._pump_task: asyncio.Task[None] | None = None
        self._error: RedisError | None = None

    def start(self) -> None:
        self._pump_task = asyncio.create_task(self._pump())

    async def get(self, *, timeout: float) -> RunStreamEvent | None:
        if self._error is not None:
            raise self._error
        if self._overflow.is_set():
            raise RedisError("run_stream_buffer_overflow")
        try:
            event, size = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None
        self._queued_bytes -= size
        return event

    async def _pump(self) -> None:
        try:
            while not self._overflow.is_set():
                try:
                    async with asyncio.timeout(1.5):
                        message = await self._pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=1.0,
                        )
                except TimeoutError:
                    continue
                if message is None or message.get("type") != "message":
                    continue
                data = message.get("data")
                raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
                if len(raw) > MAX_REDIS_EVENT_BYTES:
                    continue
                try:
                    event = _EVENT_ADAPTER.validate_json(raw)
                except (ValueError, TypeError):
                    continue
                if self._queue.full() or self._queued_bytes + len(raw) > MAX_SUBSCRIPTION_BYTES:
                    self._overflow.set()
                    return
                self._queued_bytes += len(raw)
                self._queue.put_nowait((event, len(raw)))
        except asyncio.CancelledError:
            raise
        except RedisError as error:
            self._error = error

    async def aclose(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            with suppress(asyncio.CancelledError, RedisError, TimeoutError):
                async with asyncio.timeout(1.0):
                    await self._pump_task
            self._pump_task = None
        with suppress(RedisError, TimeoutError):
            async with asyncio.timeout(1.0):
                close_pubsub = cast(Callable[[], Awaitable[None]], self._pubsub.aclose)
                await close_pubsub()


class RedisRunTransport(RunEventTransport):
    """为每条 Run 建立独立 channel 与执行 epoch 内的临时序号。"""

    def __init__(self, client: Redis) -> None:
        self._client = client
        self._failed_epochs: OrderedDict[tuple[str, int], float] = OrderedDict()

    async def publish_text(
        self,
        run_id: str,
        execution_epoch: int,
        text: str,
        *,
        round: int = 0,
    ) -> None:
        try:
            remaining = text
            if not remaining:
                await self._publish_text_chunk(
                    run_id,
                    execution_epoch,
                    "",
                    round=round,
                )
                return
            while remaining:
                chunk_length = self._max_text_chunk_length(
                    run_id,
                    execution_epoch,
                    remaining,
                    round=round,
                )
                chunk, remaining = remaining[:chunk_length], remaining[chunk_length:]
                await self._publish_text_chunk(
                    run_id,
                    execution_epoch,
                    chunk,
                    round=round,
                )
        except BaseException:
            self._remember_failed_epoch(run_id, execution_epoch)
            raise

    async def publish_persisted(self, run_id: str, execution_epoch: int, event: RunEvent) -> None:
        try:
            await self._publish_pending_interruption(run_id, execution_epoch)
            sequence = await self._next_sequence(run_id, execution_epoch)
            await self._publish(
                run_id,
                RunPersistedEvent(
                    run_id=run_id,
                    execution_epoch=execution_epoch,
                    stream_sequence=sequence,
                    payload=RunPersistedEventPayload(event=event),
                ),
            )
        except BaseException:
            self._remember_failed_epoch(run_id, execution_epoch)
            raise

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[RunEventSubscription]:
        pubsub = self._client.pubsub()
        try:
            async with asyncio.timeout(1.0):
                await pubsub.subscribe(self._channel(run_id))
        except BaseException:
            with suppress(RedisError, TimeoutError):
                async with asyncio.timeout(1.0):
                    close_pubsub = cast(Callable[[], Awaitable[None]], pubsub.aclose)
                    await close_pubsub()
            raise
        subscription = _RedisRunSubscription(pubsub)
        subscription.start()
        try:
            yield subscription
        finally:
            await subscription.aclose()

    async def aclose(self) -> None:
        with suppress(RedisError, TimeoutError):
            async with asyncio.timeout(1.0):
                await self._client.aclose()

    async def _next_sequence(self, run_id: str, epoch: int) -> int:
        key = f"athena:run:stream-sequence:{run_id}:{epoch}"
        async with asyncio.timeout(1.0):
            sequence = await cast(
                Awaitable[int],
                self._client.eval(
                    "local n=redis.call('INCR',KEYS[1]); "
                    "if n==1 then redis.call('EXPIRE',KEYS[1],3600) end; "
                    "return n",
                    1,
                    key,
                ),
            )
        return int(sequence)

    async def _publish(self, run_id: str, event: RunStreamEvent) -> None:
        raw = event.model_dump_json(exclude_none=True).encode("utf-8")
        if len(raw) > MAX_REDIS_EVENT_BYTES:
            raise ValueError("run_stream_event_exceeds_redis_limit")
        async with asyncio.timeout(1.0):
            await self._client.publish(self._channel(run_id), raw)

    async def _publish_text_chunk(
        self,
        run_id: str,
        execution_epoch: int,
        text: str,
        *,
        round: int,
    ) -> None:
        await self._publish_pending_interruption(run_id, execution_epoch)
        sequence = await self._next_sequence(run_id, execution_epoch)
        await self._publish(
            run_id,
            RunTextDeltaEvent(
                run_id=run_id,
                execution_epoch=execution_epoch,
                stream_sequence=sequence,
                payload=RunTextDeltaPayload(text=text, round=round),
            ),
        )

    async def _publish_pending_interruption(self, run_id: str, execution_epoch: int) -> None:
        key = (run_id, execution_epoch)
        if not self._failed_epoch_is_pending(key):
            return
        sequence = await self._next_sequence(run_id, execution_epoch)
        await self._publish(
            run_id,
            RunInterruptedEvent(
                run_id=run_id,
                execution_epoch=execution_epoch,
                stream_sequence=sequence,
                payload=RunInterruptedPayload(reason="publisher_error"),
            ),
        )
        self._failed_epochs.pop(key, None)

    def _remember_failed_epoch(self, run_id: str, execution_epoch: int) -> None:
        now = time.monotonic()
        self._expire_failed_epochs(now)
        key = (run_id, execution_epoch)
        self._failed_epochs[key] = now + FAILED_EPOCH_TTL_SECONDS
        self._failed_epochs.move_to_end(key)
        while len(self._failed_epochs) > MAX_FAILED_EPOCHS:
            self._failed_epochs.popitem(last=False)

    def _failed_epoch_is_pending(self, key: tuple[str, int]) -> bool:
        now = time.monotonic()
        self._expire_failed_epochs(now)
        return key in self._failed_epochs

    def _expire_failed_epochs(self, now: float) -> None:
        while self._failed_epochs:
            oldest_key, expires_at = next(iter(self._failed_epochs.items()))
            if expires_at > now:
                break
            self._failed_epochs.pop(oldest_key)

    @staticmethod
    def _max_text_chunk_length(
        run_id: str,
        execution_epoch: int,
        text: str,
        *,
        round: int,
    ) -> int:
        """按完整 UTF-8 JSON 大小切分文本，避免截断多字节字符。"""
        low = 1
        high = len(text)
        best = 0
        while low <= high:
            middle = (low + high) // 2
            event = RunTextDeltaEvent(
                run_id=run_id,
                execution_epoch=execution_epoch,
                stream_sequence=9_999_999_999_999_999_999,
                payload=RunTextDeltaPayload(text=text[:middle], round=round),
            )
            if (
                len(event.model_dump_json(exclude_none=True).encode("utf-8"))
                <= MAX_REDIS_EVENT_BYTES
                and len(event.model_dump_json(exclude_none=True).encode("utf-8"))
                + SSE_PREFIX_RESERVE
                <= MAX_SSE_EVENT_BYTES
            ):
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best == 0:
            raise ValueError("run_stream_text_chunk_exceeds_redis_limit")
        return best

    @staticmethod
    def _channel(run_id: str) -> str:
        return f"athena:run:events:{run_id}"


__all__ = ["RedisRunTransport"]
