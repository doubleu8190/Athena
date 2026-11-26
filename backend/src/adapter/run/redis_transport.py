"""使用 Redis Pub/Sub 传输 Run 实时通知，不保存可重放事件。"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Awaitable, Callable
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
from domain.run.ports import RunEventSubscription, RunEventTransport, RunEventTransportError

MAX_REDIS_EVENT_BYTES = MAX_RUN_SSE_EVENT_BYTES
MAX_SSE_EVENT_BYTES = MAX_RUN_SSE_EVENT_BYTES
SSE_PREFIX_RESERVE = len(b"event: text_delta\nid: 99999999999999999999\ndata: \n\n")
MAX_SUBSCRIPTION_EVENTS = 256
MAX_SUBSCRIPTION_BYTES = 1024 * 1024
MAX_TRACKED_EPOCHS = 1024
MAX_FAILED_EPOCHS = 256
_TERMINAL_PERSISTED_EVENTS = frozenset({"completed", "failed", "cancelled"})
_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(cast(Any, RunStreamEvent))


class _RedisRunSubscription(RunEventSubscription):
    """单个 SSE 连接持有的有界本地队列，隔离慢客户端溢出并支持故障即时唤醒。"""

    def __init__(self, on_close: Callable[[], Awaitable[None]] | None = None) -> None:
        self._items: deque[tuple[RunStreamEvent, int]] = deque()
        self._queued_bytes = 0
        self._overflow = False
        self._error: RunEventTransportError | None = None
        self._closed = False
        self._wakeup = asyncio.Event()
        self._on_close = on_close

    def push(self, event: RunStreamEvent, raw_size: int) -> None:
        """写入一条已反序列化事件；超限时仅标记当前慢客户端溢出，不影响其他订阅者。"""
        if self._closed or self._overflow or self._error is not None:
            return
        if (
            len(self._items) >= MAX_SUBSCRIPTION_EVENTS
            or self._queued_bytes + raw_size > MAX_SUBSCRIPTION_BYTES
        ):
            self._overflow = True
            self._items.clear()
            self._queued_bytes = 0
        else:
            self._items.append((event, raw_size))
            self._queued_bytes += raw_size
        self._wakeup.set()

    def fail(self, error: RunEventTransportError) -> None:
        """广播共享传输故障并立即唤醒阻塞在 get() 上的消费者。"""
        if self._error is None:
            self._error = error
        self._wakeup.set()

    def _mark_closed(self) -> None:
        """持锁关闭路径内标记关闭并唤醒等待方，避免回调重入死锁。"""
        self._closed = True
        self._items.clear()
        self._queued_bytes = 0
        self._wakeup.set()

    async def get(self, *, timeout: float) -> RunStreamEvent | None:
        while True:
            if self._error is not None:
                raise self._error
            if self._overflow:
                raise RunEventTransportError("run_stream_buffer_overflow")
            if self._items:
                event, size = self._items.popleft()
                self._queued_bytes -= size
                if not self._items:
                    self._wakeup.clear()
                return event
            if self._closed:
                return None
            self._wakeup.clear()
            try:
                async with asyncio.timeout(timeout):
                    await self._wakeup.wait()
            except TimeoutError:
                return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._mark_closed()
        if self._on_close is not None:
            await self._on_close()


class RedisRunTransport(RunEventTransport):
    """基于进程内序号与单连接 Pub/Sub 多路复用的 Run 实时事件传输器。"""

    def __init__(self, client: Redis, *, owns_client: bool = True) -> None:
        self._client = client
        self._owns_client = owns_client
        # 活跃 Run 已由 execution_epoch 围栏到单一 Worker，直接用进程内有界 LRU 维护单调序号。
        self._sequences: OrderedDict[tuple[str, int], int] = OrderedDict()
        self._failed_epochs: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._pubsub: PubSub | None = None
        self._pubsub_loop: asyncio.AbstractEventLoop | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._pubsub_generation = 0
        self._subscribers: dict[bytes, set[_RedisRunSubscription]] = {}

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    @asynccontextmanager
    async def _publish_guard(self, run_id: str, execution_epoch: int) -> AsyncGenerator[None, None]:
        """统一捕获发布异常并记录故障代次，后续恢复推送前自动补发 publisher_error。"""
        try:
            yield
        except RedisError as error:
            self._remember_failed_epoch((run_id, execution_epoch))
            raise RunEventTransportError("run_stream_publish_failed") from error
        except BaseException:
            self._remember_failed_epoch((run_id, execution_epoch))
            raise

    async def publish_text(
        self, run_id: str, execution_epoch: int, text: str, *, round: int = 0
    ) -> None:
        key = (run_id, execution_epoch)
        async with self._publish_guard(run_id, execution_epoch):
            for chunk in self._split_text_chunks(run_id, execution_epoch, text, round=round):
                await self._flush_pending_interruption(run_id, execution_epoch)
                await self._publish_event(
                    run_id,
                    RunTextDeltaEvent(
                        run_id=run_id,
                        execution_epoch=execution_epoch,
                        stream_sequence=self._next_sequence(key),
                        payload=RunTextDeltaPayload(text=chunk, round=round),
                    ),
                )

    async def publish_persisted(self, run_id: str, execution_epoch: int, event: RunEvent) -> None:
        key = (run_id, execution_epoch)
        async with self._publish_guard(run_id, execution_epoch):
            await self._flush_pending_interruption(run_id, execution_epoch)
            await self._publish_event(
                run_id,
                RunPersistedEvent(
                    run_id=run_id,
                    execution_epoch=execution_epoch,
                    stream_sequence=self._next_sequence(key),
                    payload=RunPersistedEventPayload(event=event),
                ),
            )
        # 本 Run 进入终态后立即清理该代次序号与失败标记，防止常驻 Worker 内存累积。
        if event.run_id == run_id and event.type in _TERMINAL_PERSISTED_EVENTS:
            self._sequences.pop(key, None)
            self._failed_epochs.pop(key, None)

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncGenerator[RunEventSubscription, None]:
        """登记本地订阅者并复用单条共享 Pub/Sub 连接，按频道引用计数动态增减订阅。"""
        channel_str = self._channel(run_id)
        channel_key = channel_str.encode("utf-8")

        async def _on_close() -> None:
            await self._unregister_subscription(channel_str, channel_key, subscription)

        subscription = _RedisRunSubscription(on_close=_on_close)
        current_loop = asyncio.get_running_loop()
        async with self._get_lock():
            if self._pubsub is not None and self._pubsub_loop is not current_loop:
                self._subscribers.clear()
                await self._stop_shared_pubsub_locked()
            if self._pubsub is not None and channel_key in self._subscribers:
                # 同频道已有订阅者：纯内存登记，零 Redis 往返。
                self._subscribers[channel_key].add(subscription)
            else:
                # 首次建连或新增频道：复用连接时先预登记再 SUBSCRIBE，避免后台泵漏投首条消息。
                is_new = self._pubsub is None
                pubsub = self._client.pubsub() if is_new else self._pubsub
                assert pubsub is not None
                generation = self._pubsub_generation
                if not is_new:
                    self._subscribers[channel_key] = {subscription}
                try:
                    async with asyncio.timeout(1.0):
                        await pubsub.subscribe(channel_str)
                except BaseException as error:
                    if is_new:
                        await self._close_pubsub_safely(pubsub)
                    else:
                        bucket = self._subscribers.get(channel_key)
                        if bucket is not None:
                            bucket.discard(subscription)
                            if not bucket:
                                del self._subscribers[channel_key]
                        if isinstance(error, (RedisError, TimeoutError)):
                            await self._trip_shared_pubsub_locked(generation, cause=error)
                    if isinstance(error, RedisError):
                        raise RunEventTransportError("run_stream_subscribe_failed") from error
                    raise
                if is_new:
                    self._pubsub_generation += 1
                    self._pubsub, self._pubsub_loop = pubsub, current_loop
                    self._subscribers[channel_key] = {subscription}
                    self._pump_task = asyncio.create_task(
                        self._pump(pubsub, self._pubsub_generation)
                    )
        try:
            yield subscription
        finally:
            await subscription.aclose()

    async def aclose(self) -> None:
        """停止共享泵并关闭共享 Pub/Sub；仅当 owns_client=True 时关闭底层 Redis 客户端。"""
        async with self._get_lock():
            for bucket in self._subscribers.values():
                for subscriber in bucket:
                    subscriber._mark_closed()
            self._subscribers.clear()
            await self._stop_shared_pubsub_locked()
        if self._owns_client:
            with suppress(RedisError, TimeoutError):
                async with asyncio.timeout(1.0):
                    await self._client.aclose()

    async def _unregister_subscription(
        self, channel_str: str, channel_key: bytes, subscription: _RedisRunSubscription
    ) -> None:
        """移除本地订阅者；频道归零时 UNSUBSCRIBE，全局归零时释放共享连接。"""
        async with self._get_lock():
            bucket = self._subscribers.get(channel_key)
            if bucket is None or subscription not in bucket:
                return
            bucket.discard(subscription)
            if bucket:
                return
            del self._subscribers[channel_key]
            if not self._subscribers:
                await self._stop_shared_pubsub_locked()
                return
            if self._pubsub is not None:
                generation = self._pubsub_generation
                try:
                    async with asyncio.timeout(1.0):
                        await self._pubsub.unsubscribe(channel_str)
                except (RedisError, TimeoutError) as error:
                    await self._trip_shared_pubsub_locked(generation, cause=error)

    async def _pump(self, pubsub: PubSub, generation: int) -> None:
        """进程级单后台泵：单次反序列化并按频道扇出到全部活跃订阅队列。"""
        try:
            while self._pubsub is pubsub and self._pubsub_generation == generation:
                try:
                    async with asyncio.timeout(1.5):
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                except TimeoutError:
                    continue
                if self._pubsub is not pubsub or self._pubsub_generation != generation:
                    return
                if message is None or message.get("type") != "message":
                    continue
                raw_channel = message.get("channel")
                channel_key = (
                    raw_channel.encode("utf-8") if isinstance(raw_channel, str) else raw_channel
                )
                subscribers = (
                    self._subscribers.get(channel_key) if isinstance(channel_key, bytes) else None
                )
                if not subscribers:
                    continue
                data = message.get("data")
                raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
                if len(raw) > MAX_REDIS_EVENT_BYTES:
                    continue
                try:
                    event = _EVENT_ADAPTER.validate_json(raw)
                except (ValueError, TypeError):
                    continue
                for subscriber in tuple(subscribers):
                    subscriber.push(event, len(raw))
        except asyncio.CancelledError:
            raise
        except RedisError as error:
            async with self._get_lock():
                await self._trip_shared_pubsub_locked(generation, cause=error)

    async def _trip_shared_pubsub_locked(
        self, expected_generation: int, *, cause: Exception
    ) -> None:
        """代次匹配时熔断共享连接：向全部订阅者广播传输故障并关闭底层连接。"""
        if self._pubsub_generation != expected_generation or self._pubsub is None:
            return
        translated = RunEventTransportError("run_stream_transport_failed")
        translated.__cause__ = cause
        active = [sub for bucket in self._subscribers.values() for sub in bucket]
        self._subscribers.clear()
        for subscriber in active:
            subscriber.fail(translated)
        await self._stop_shared_pubsub_locked()

    async def _stop_shared_pubsub_locked(self) -> None:
        pubsub, pump_task, pubsub_loop = self._pubsub, self._pump_task, self._pubsub_loop
        self._pubsub = self._pump_task = self._pubsub_loop = None
        current_loop = asyncio.get_running_loop()
        if pump_task is not None and pump_task is not asyncio.current_task():
            pump_task.cancel()
            if pubsub_loop is current_loop:
                with suppress(asyncio.CancelledError, RedisError, TimeoutError):
                    async with asyncio.timeout(1.0):
                        await pump_task
        if pubsub is not None and (pubsub_loop is None or pubsub_loop is current_loop):
            await self._close_pubsub_safely(pubsub)

    @staticmethod
    async def _close_pubsub_safely(pubsub: PubSub) -> None:
        with suppress(RedisError, TimeoutError):
            async with asyncio.timeout(1.0):
                await cast(Callable[[], Awaitable[None]], pubsub.aclose)()

    async def _flush_pending_interruption(self, run_id: str, execution_epoch: int) -> None:
        key = (run_id, execution_epoch)
        if key not in self._failed_epochs:
            return
        await self._publish_event(
            run_id,
            RunInterruptedEvent(
                run_id=run_id,
                execution_epoch=execution_epoch,
                stream_sequence=self._next_sequence(key),
                payload=RunInterruptedPayload(reason="publisher_error"),
            ),
        )
        self._failed_epochs.pop(key, None)

    async def _publish_event(self, run_id: str, event: RunStreamEvent) -> None:
        raw = event.model_dump_json(exclude_none=True).encode("utf-8")
        if len(raw) > MAX_REDIS_EVENT_BYTES:
            raise ValueError("run_stream_event_exceeds_redis_limit")
        async with asyncio.timeout(1.0):
            await cast(Awaitable[int], self._client.publish(self._channel(run_id), raw))

    def _next_sequence(self, key: tuple[str, int]) -> int:
        sequence = self._sequences.get(key, 0) + 1
        self._sequences[key] = sequence
        self._sequences.move_to_end(key)
        while len(self._sequences) > MAX_TRACKED_EPOCHS:
            self._sequences.popitem(last=False)
        return sequence

    def _remember_failed_epoch(self, key: tuple[str, int]) -> None:
        self._failed_epochs[key] = None
        self._failed_epochs.move_to_end(key)
        while len(self._failed_epochs) > MAX_FAILED_EPOCHS:
            self._failed_epochs.popitem(last=False)

    @staticmethod
    def _split_text_chunks(
        run_id: str, execution_epoch: int, text: str, *, round: int
    ) -> list[str]:
        """按 UTF-8 安全字节边界与 JSON 转义字节预算切分文本，保证单帧严格 ≤ 64KB。"""
        max_json_bytes = min(MAX_REDIS_EVENT_BYTES, MAX_SSE_EVENT_BYTES - SSE_PREFIX_RESERVE)
        envelope_bytes = len(
            RunTextDeltaEvent(
                run_id=run_id,
                execution_epoch=execution_epoch,
                stream_sequence=9_999_999_999_999_999_999,
                payload=RunTextDeltaPayload(text="", round=round),
            )
            .model_dump_json(exclude_none=True)
            .encode("utf-8")
        )
        text_budget = max_json_bytes - envelope_bytes
        if not text:
            if text_budget < 0:
                raise ValueError("run_stream_event_exceeds_redis_limit")
            return [""]
        if text_budget <= 0:
            raise ValueError("run_stream_text_chunk_exceeds_redis_limit")
        # 单码点 JSON 转义至多占 6 字节，绝大多数小段文本直接 O(1) 放行。
        if len(text) * 6 <= text_budget:
            return [text]

        raw = text.encode("utf-8")
        chunks: list[str] = []
        pos, total = 0, len(raw)
        while pos < total:
            end = min(pos + text_budget, total)
            # 回退跳过 UTF-8 续字节（10xxxxxx），避免截断多字节字符。
            while end > pos and end < total and (raw[end] & 0xC0) == 0x80:
                end -= 1
            chunk = raw[pos:end].decode("utf-8")
            # 处理控制字符/引号/反斜杠在 JSON 转义后的膨胀，按比例收缩至预算内。
            escaped_bytes = len(json.dumps(chunk, ensure_ascii=False).encode("utf-8")) - 2
            while escaped_bytes > text_budget and chunk:
                shrink_to = min(len(chunk) - 1, max(1, (len(chunk) * text_budget) // escaped_bytes))
                chunk = chunk[:shrink_to]
                escaped_bytes = len(json.dumps(chunk, ensure_ascii=False).encode("utf-8")) - 2
            if not chunk:
                raise ValueError("run_stream_text_chunk_exceeds_redis_limit")
            chunks.append(chunk)
            pos += len(chunk.encode("utf-8"))
        return chunks

    @staticmethod
    def _channel(run_id: str) -> str:
        return f"athena:run:events:{run_id}"


__all__ = ["RedisRunTransport"]
