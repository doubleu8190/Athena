"""使用 Redis Pub/Sub 传输 Run 实时通知，不保存可重放事件。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

from pydantic import TypeAdapter
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import NoScriptError, RedisError

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
MAX_SUBSCRIPTION_EVENTS = 256
MAX_SUBSCRIPTION_BYTES = 1024 * 1024
MAX_FAILED_EPOCHS = 256
FAILED_EPOCH_TTL_SECONDS = 3600.0
MAX_SSE_EVENT_BYTES = MAX_RUN_SSE_EVENT_BYTES
SSE_PREFIX_RESERVE = len(b"event: text_delta\nid: 99999999999999999999\ndata: \n\n")
_SEQUENCE_TTL_SECONDS = 3600
_STREAM_SEQUENCE_MARKER = b',"stream_sequence":0,"payload":'
_STREAM_SEQUENCE_PREFIX_MARKER_LEN = len(b',"stream_sequence":')
_STREAM_SEQUENCE_FULL_FIELD_LEN = len(b',"stream_sequence":0')
# 单次 Lua 脚本原子完成：递增执行代次序号、首次设置 3600s TTL、二进制拼接序号与上限校验并 PUBLISH。
# 不在 Lua 内使用 cjson，直接按 Python 端切分好的不可变前后缀拼接，零额外编解码开销。
_PUBLISH_LUA_SCRIPT = (
    "local n = redis.call('INCR', KEYS[1])\n"
    "if n == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end\n"
    "local msg = ARGV[2] .. tostring(n) .. ARGV[3]\n"
    "if #msg > tonumber(ARGV[5]) then return -1 end\n"
    "redis.call('PUBLISH', ARGV[4], msg)\n"
    "return n"
)
_PUBLISH_SCRIPT_SHA = hashlib.sha1(_PUBLISH_LUA_SCRIPT.encode("utf-8")).hexdigest()
_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(cast(Any, RunStreamEvent))


class _RedisRunSubscription(RunEventSubscription):
    """单个 SSE 连接持有的本地有界订阅视图，独立承担背压隔离与故障即时唤醒。"""

    def __init__(self, on_close: Callable[[], Awaitable[None]] | None = None) -> None:
        self._items: deque[tuple[RunStreamEvent, int]] = deque()
        self._queued_bytes = 0
        self._overflow = False
        # 对外只保存领域异常，Redis 客户端异常在共享泵或订阅握手阶段完成翻译。
        self._error: RunEventTransportError | None = None
        self._closed = False
        self._wakeup = asyncio.Event()
        self._on_close = on_close

    def push(self, event: RunStreamEvent, raw_size: int) -> None:
        """写入一条已反序列化事件；超限时仅标记当前慢客户端溢出，不影响同频道其他订阅者。"""
        if self._closed or self._overflow or self._error is not None:
            return
        if (
            len(self._items) >= MAX_SUBSCRIPTION_EVENTS
            or self._queued_bytes + raw_size > MAX_SUBSCRIPTION_BYTES
        ):
            self._overflow = True
            self._items.clear()
            self._queued_bytes = 0
            self._wakeup.set()
            return
        self._items.append((event, raw_size))
        self._queued_bytes += raw_size
        self._wakeup.set()

    def fail(self, error: RunEventTransportError) -> None:
        """广播共享传输故障并立即唤醒阻塞在 get() 上的消费者。"""
        if self._error is None:
            self._error = error
        self._wakeup.set()

    def _mark_closed(self) -> None:
        """在持锁关闭路径内无回调地标记关闭并唤醒等待方，避免锁重入死锁。"""
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
    """为每条 Run 维护频道路由与临时序号，并在进程内共享单条 Redis Pub/Sub 连接多路复用。"""

    def __init__(self, client: Redis, *, owns_client: bool = True) -> None:
        self._client = client
        self._owns_client = owns_client
        self._failed_epochs: OrderedDict[tuple[str, int], float] = OrderedDict()
        # 严禁在 __init__ 中创建 asyncio.Lock / Task，按当前运行事件循环惰性初始化以支持跨循环复用。
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._pubsub: PubSub | None = None
        self._pubsub_loop: asyncio.AbstractEventLoop | None = None
        self._pump_task: asyncio.Task[None] | None = None
        # 共享 PubSub 代次号：每次新建连接递增，供后台泵故障熔断时精确校验是否仍为当前连接。
        self._pubsub_generation = 0
        # 规范化后的 channel(bytes) -> 当前活跃的本地订阅者集合（兼作频道引用计数）。
        self._subscribers: dict[bytes, set[_RedisRunSubscription]] = {}

    def _get_lock(self) -> asyncio.Lock:
        """按当前运行事件循环惰性获取或重建锁。"""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

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
        except RedisError as error:
            # 发布链路不可用时先记录失败代次，再翻译为领域异常并保留原始原因。
            self._remember_failed_epoch(run_id, execution_epoch)
            raise RunEventTransportError("run_stream_publish_failed") from error
        except BaseException:
            self._remember_failed_epoch(run_id, execution_epoch)
            raise

    async def publish_persisted(self, run_id: str, execution_epoch: int, event: RunEvent) -> None:
        try:
            await self._publish_pending_interruption(run_id, execution_epoch)
            await self._publish_with_sequence(
                run_id,
                execution_epoch,
                RunPersistedEvent(
                    run_id=run_id,
                    execution_epoch=execution_epoch,
                    stream_sequence=0,
                    payload=RunPersistedEventPayload(event=event),
                ),
            )
        except RedisError as error:
            # 正式通知发布失败同样记录代次，后续恢复时补发中断标记。
            self._remember_failed_epoch(run_id, execution_epoch)
            raise RunEventTransportError("run_stream_publish_failed") from error
        except BaseException:
            self._remember_failed_epoch(run_id, execution_epoch)
            raise

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncGenerator[RunEventSubscription, None]:
        """登记本地订阅者并复用共享 Pub/Sub 连接；按频道引用计数动态执行 subscribe/unsubscribe。"""
        channel_str = self._channel(run_id)
        channel_key = channel_str.encode("utf-8")
        subscription: _RedisRunSubscription

        async def _on_close() -> None:
            await self._unregister_subscription(channel_str, channel_key, subscription)

        subscription = _RedisRunSubscription(on_close=_on_close)
        current_loop = asyncio.get_running_loop()
        async with self._get_lock():
            # 若上一事件循环遗留未关闭的共享 PubSub，先清理句柄再在当前事件循环重建。
            if self._pubsub is not None and self._pubsub_loop is not current_loop:
                self._subscribers.clear()
                await self._stop_shared_pubsub_locked()

            if self._pubsub is None:
                # 首个全局订阅者：必须先完成首次 subscribe 握手建立底层连接，再启动后台 _pump_task。
                pubsub = self._client.pubsub()
                try:
                    async with asyncio.timeout(1.0):
                        await pubsub.subscribe(channel_str)
                except BaseException as error:
                    await self._close_pubsub_safely(pubsub)
                    if isinstance(error, RedisError):
                        # 订阅握手失败：先清理连接，再翻译为领域异常并保留原因。
                        raise RunEventTransportError("run_stream_subscribe_failed") from error
                    raise
                self._pubsub_generation += 1
                generation = self._pubsub_generation
                self._pubsub = pubsub
                self._pubsub_loop = current_loop
                self._subscribers[channel_key] = {subscription}
                self._pump_task = asyncio.create_task(self._pump(pubsub, generation))
            elif channel_key in self._subscribers:
                # 同频道已有活跃订阅者（refcount >= 1）：纯内存登记，零 Redis 网络往返。
                self._subscribers[channel_key].add(subscription)
            else:
                # 共享连接已存在但该频道 refcount 0 -> 1：先预登记再发 SUBSCRIBE，
                # 防止后台 _pump 在 subscribe 返回前先收到首条消息时漏投。
                existing_pubsub = self._pubsub
                generation = self._pubsub_generation
                self._subscribers[channel_key] = {subscription}
                try:
                    async with asyncio.timeout(1.0):
                        await existing_pubsub.subscribe(channel_str)
                except BaseException as error:
                    bucket = self._subscribers.get(channel_key)
                    if bucket is not None:
                        bucket.discard(subscription)
                        if not bucket:
                            del self._subscribers[channel_key]
                    if isinstance(error, (RedisError, TimeoutError)):
                        # 共享连接新增频道失败：先熔断旧池并广播故障唤醒全部既有订阅者，
                        # 再向本次订阅调用方抛出订阅失败/超时异常。
                        translated = RunEventTransportError("run_stream_transport_failed")
                        translated.__cause__ = error
                        await self._trip_shared_pubsub_locked(
                            expected_generation=generation,
                            error=translated,
                        )
                        if isinstance(error, RedisError):
                            raise RunEventTransportError("run_stream_subscribe_failed") from error
                    raise
        try:
            yield subscription
        finally:
            await subscription.aclose()

    async def aclose(self) -> None:
        """始终停止共享泵并关闭共享 PubSub；严格遵循 owns_client 契约决定是否关闭 Redis 客户端。"""
        async with self._get_lock():
            active_subscribers = [
                subscriber
                for subscribers in self._subscribers.values()
                for subscriber in subscribers
            ]
            self._subscribers.clear()
            for subscriber in active_subscribers:
                subscriber._mark_closed()
            await self._stop_shared_pubsub_locked()
        if not self._owns_client:
            return
        with suppress(RedisError, TimeoutError):
            async with asyncio.timeout(1.0):
                await self._client.aclose()

    async def _unregister_subscription(
        self,
        channel_str: str,
        channel_key: bytes,
        subscription: _RedisRunSubscription,
    ) -> None:
        """退订本地订阅者；频道归零时按需 UNSUBSCRIBE，全局归零时立即停泵并释放共享连接。"""
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
            pubsub = self._pubsub
            generation = self._pubsub_generation
            if pubsub is not None:
                try:
                    async with asyncio.timeout(1.0):
                        await pubsub.unsubscribe(channel_str)
                except (RedisError, TimeoutError) as error:
                    # 退订指令失败说明共享连接已损坏：熔断共享连接并通知其余订阅者重连对账。
                    translated = RunEventTransportError("run_stream_transport_failed")
                    translated.__cause__ = error
                    await self._trip_shared_pubsub_locked(
                        expected_generation=generation,
                        error=translated,
                    )

    async def _pump(self, pubsub: PubSub, generation: int) -> None:
        """进程级单后台泵：持续读取共享 PubSub，单次反序列化后按频道扇出至各本地订阅队列。"""
        try:
            while self._pubsub is pubsub and self._pubsub_generation == generation:
                try:
                    async with asyncio.timeout(1.5):
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=1.0,
                        )
                except TimeoutError:
                    continue
                if self._pubsub is not pubsub or self._pubsub_generation != generation:
                    return
                if message is None or message.get("type") != "message":
                    continue
                channel_key = self._normalize_channel(message.get("channel"))
                if channel_key is None:
                    continue
                subscribers = self._subscribers.get(channel_key)
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
                raw_len = len(raw)
                # 单次反序列化结果扇出给同频道全体订阅者；慢客户端溢出仅作用于其自身。
                for subscriber in tuple(subscribers):
                    subscriber.push(event, raw_len)
        except asyncio.CancelledError:
            raise
        except RedisError as error:
            # 共享连接读取异常：翻译为领域传输异常并保留原因链，校验代次后广播给全部活跃订阅者。
            translated = RunEventTransportError("run_stream_transport_failed")
            translated.__cause__ = error
            async with self._get_lock():
                await self._trip_shared_pubsub_locked(
                    expected_generation=generation,
                    error=translated,
                )

    async def _trip_shared_pubsub_locked(
        self,
        *,
        expected_generation: int,
        error: RunEventTransportError,
    ) -> None:
        """在持锁且代次匹配时熔断共享连接：先同步广播故障唤醒消费者，再清理泵与连接。"""
        if self._pubsub_generation != expected_generation or self._pubsub is None:
            return
        active_subscribers = [
            subscriber for subscribers in self._subscribers.values() for subscriber in subscribers
        ]
        self._subscribers.clear()
        for subscriber in active_subscribers:
            subscriber.fail(error)
        await self._stop_shared_pubsub_locked()

    async def _stop_shared_pubsub_locked(self) -> None:
        """停止后台泵并关闭共享 PubSub；需在持有 self._lock 状态下调用。"""
        pubsub = self._pubsub
        pump_task = self._pump_task
        pubsub_loop = self._pubsub_loop
        self._pubsub = None
        self._pump_task = None
        self._pubsub_loop = None
        current_loop = asyncio.get_running_loop()
        current_task = asyncio.current_task()
        if pump_task is not None and pump_task is not current_task:
            if pubsub_loop is current_loop:
                pump_task.cancel()
                with suppress(asyncio.CancelledError, RedisError, TimeoutError):
                    async with asyncio.timeout(1.0):
                        await pump_task
            elif not pump_task.done():
                pump_task.cancel()
        if pubsub is not None and (pubsub_loop is None or pubsub_loop is current_loop):
            await self._close_pubsub_safely(pubsub)

    @staticmethod
    async def _close_pubsub_safely(pubsub: PubSub) -> None:
        with suppress(RedisError, TimeoutError):
            async with asyncio.timeout(1.0):
                close_pubsub = cast(Callable[[], Awaitable[None]], pubsub.aclose)
                await close_pubsub()

    async def _publish_with_sequence(
        self,
        run_id: str,
        execution_epoch: int,
        event: RunStreamEvent,
    ) -> int:
        """以单次 Lua EVALSHA 原子完成序号分配与 PUBLISH；未缓存脚本时自动回退 EVAL。"""
        raw = event.model_dump_json(exclude_none=True).encode("utf-8")
        prefix, suffix = self._split_stream_sequence(raw)
        # 序号至少占 1 字节，提前拦截必然超限事件，避免无效网络 RTT 与空耗序号。
        if len(prefix) + 1 + len(suffix) > MAX_REDIS_EVENT_BYTES:
            raise ValueError("run_stream_event_exceeds_redis_limit")
        key = f"athena:run:stream-sequence:{run_id}:{execution_epoch}"
        channel = self._channel(run_id)
        client = cast(Any, self._client)
        async with asyncio.timeout(1.0):
            try:
                result = await cast(
                    Awaitable[int],
                    client.evalsha(
                        _PUBLISH_SCRIPT_SHA,
                        1,
                        key,
                        _SEQUENCE_TTL_SECONDS,
                        prefix,
                        suffix,
                        channel,
                        MAX_REDIS_EVENT_BYTES,
                    ),
                )
            except NoScriptError:
                # NoScriptError 继承自 RedisError，必须在内层优先截获并回退 EVAL 自动加载脚本。
                result = await cast(
                    Awaitable[int],
                    client.eval(
                        _PUBLISH_LUA_SCRIPT,
                        1,
                        key,
                        _SEQUENCE_TTL_SECONDS,
                        prefix,
                        suffix,
                        channel,
                        MAX_REDIS_EVENT_BYTES,
                    ),
                )
        sequence = int(result)
        if sequence < 0:
            raise ValueError("run_stream_event_exceeds_redis_limit")
        return sequence

    @staticmethod
    def _split_stream_sequence(raw: bytes) -> tuple[bytes, bytes]:
        """定位顶层 stream_sequence=0 占位符（位于 payload 之前）拆分为前后缀字节串。"""
        marker_index = raw.find(_STREAM_SEQUENCE_MARKER)
        if marker_index < 0:
            raise ValueError("run_stream_event_sequence_marker_missing")
        prefix = raw[: marker_index + _STREAM_SEQUENCE_PREFIX_MARKER_LEN]
        suffix = raw[marker_index + _STREAM_SEQUENCE_FULL_FIELD_LEN :]
        return prefix, suffix

    async def _publish_text_chunk(
        self,
        run_id: str,
        execution_epoch: int,
        text: str,
        *,
        round: int,
    ) -> None:
        await self._publish_pending_interruption(run_id, execution_epoch)
        await self._publish_with_sequence(
            run_id,
            execution_epoch,
            RunTextDeltaEvent(
                run_id=run_id,
                execution_epoch=execution_epoch,
                stream_sequence=0,
                payload=RunTextDeltaPayload(text=text, round=round),
            ),
        )

    async def _publish_pending_interruption(self, run_id: str, execution_epoch: int) -> None:
        key = (run_id, execution_epoch)
        if not self._failed_epoch_is_pending(key):
            return
        await self._publish_with_sequence(
            run_id,
            execution_epoch,
            RunInterruptedEvent(
                run_id=run_id,
                execution_epoch=execution_epoch,
                stream_sequence=0,
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
        if not text:
            raise ValueError("run_stream_text_chunk_exceeds_redis_limit")
        # 动态读取模块级上限，保证单测 monkeypatch 生效。
        max_json_bytes = min(MAX_REDIS_EVENT_BYTES, MAX_SSE_EVENT_BYTES - SSE_PREFIX_RESERVE)
        # 一级 O(1) 数学上界快路径：JSON 单码点转义后至多占 6 UTF-8 字节，
        # 信封固定开销小于 160 字节，绝大多数正常小包直接短路，零 Pydantic 构造与二分开销。
        if (
            len(text) * 6 + len(run_id) * 6 + len(str(execution_epoch)) + len(str(round)) + 160
            <= max_json_bytes
        ):
            return len(text)
        # 二级中包快路径：整段仅执行 1 次 JSON 序列化，未超限直接返回。
        full_event = RunTextDeltaEvent(
            run_id=run_id,
            execution_epoch=execution_epoch,
            stream_sequence=9_999_999_999_999_999_999,
            payload=RunTextDeltaPayload(text=text, round=round),
        )
        if len(full_event.model_dump_json(exclude_none=True).encode("utf-8")) <= max_json_bytes:
            return len(text)
        # 超长包回退二分：按 Python 码点索引二分，每次迭代仅序列化 1 次并缓存字节长度。
        low = 1
        high = len(text) - 1
        best = 0
        while low <= high:
            middle = (low + high) // 2
            event = RunTextDeltaEvent(
                run_id=run_id,
                execution_epoch=execution_epoch,
                stream_sequence=9_999_999_999_999_999_999,
                payload=RunTextDeltaPayload(text=text[:middle], round=round),
            )
            encoded_len = len(event.model_dump_json(exclude_none=True).encode("utf-8"))
            if encoded_len <= max_json_bytes:
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

    @staticmethod
    def _normalize_channel(channel: object) -> bytes | None:
        if isinstance(channel, bytes):
            return channel
        if isinstance(channel, str):
            return channel.encode("utf-8")
        return None


__all__ = ["RedisRunTransport"]
