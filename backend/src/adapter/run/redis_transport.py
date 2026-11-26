"""使用独立 Redis Pub/Sub 订阅传输 Run 实时通知，不保存可重放事件。"""

from __future__ import annotations

import asyncio
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
    RunPersistedEvent,
    RunPersistedEventPayload,
    RunStreamEvent,
    RunTextDeltaEvent,
    RunTextDeltaPayload,
)
from domain.run.ports import RunEventSubscription, RunEventTransport, RunEventTransportError

MAX_REDIS_EVENT_BYTES = MAX_RUN_SSE_EVENT_BYTES
# 按 8192 字符定长切分：最坏单字 JSON 转义 6B（如 \u0000）仅 48 KiB，严格满足单帧 <= 64 KiB。
_TEXT_CHUNK_CHARS = 8192
_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(cast(Any, RunStreamEvent))


class _RedisRunSubscription(RunEventSubscription):
    """单个 SSE 连接直连的独立 Redis Pub/Sub 订阅。"""

    def __init__(self, pubsub: PubSub, channel: str) -> None:
        self._pubsub = pubsub
        self._channel = channel
        self._closed = False

    async def get(self, *, timeout: float) -> RunStreamEvent | None:
        """在时限内读取下一条有效事件；忽略超限或损坏载荷，传输故障抛 RunEventTransportError。"""
        if self._closed:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        first = True
        while True:
            remaining = max(0.0, deadline - loop.time())
            if not first and remaining <= 0:
                return None
            first = False
            try:
                async with asyncio.timeout(remaining + 0.2):
                    # 保留 subscribe 控制帧而非静默返回 None，避免首条确认帧触发提前返回。
                    message = await self._pubsub.get_message(
                        ignore_subscribe_messages=False,
                        timeout=remaining,
                    )
            except TimeoutError:
                return None
            except RedisError as error:
                raise RunEventTransportError("run_stream_transport_failed") from error
            if message is None:
                return None
            if message.get("type") != "message":
                continue
            data = message.get("data")
            raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
            if len(raw) > MAX_REDIS_EVENT_BYTES:
                continue
            try:
                return cast(RunStreamEvent, _EVENT_ADAPTER.validate_json(raw))
            except (ValueError, TypeError):
                continue

    async def aclose(self) -> None:
        """退订频道并安全关闭独立 Pub/Sub 连接，避免连接泄漏或未关闭告警。"""
        if self._closed:
            return
        self._closed = True
        # 使用 try/finally 隔离退订与连接关闭：退订遇 OSError 或取消时仍必须关闭 Pub/Sub。
        try:
            with suppress(RedisError, TimeoutError, OSError):
                async with asyncio.timeout(1.0):
                    await self._pubsub.unsubscribe(self._channel)
        finally:
            with suppress(RedisError, TimeoutError, OSError):
                async with asyncio.timeout(1.0):
                    await cast(Callable[[], Awaitable[None]], self._pubsub.aclose)()


class RedisRunTransport(RunEventTransport):
    """基于按连接独立 Pub/Sub 订阅的 Run 实时事件传输器。"""

    def __init__(self, client: Redis, *, owns_client: bool = True) -> None:
        self._client = client
        self._owns_client = owns_client

    async def publish_text(
        self, run_id: str, execution_epoch: int, text: str, *, round: int = 0
    ) -> None:
        for chunk in self._split_text_chunks(text):
            await self._publish_event(
                run_id,
                RunTextDeltaEvent(
                    run_id=run_id,
                    execution_epoch=execution_epoch,
                    # 传输层不维护序号，由 SSE 编排层统一分配递增的 SSE id。
                    stream_sequence=0,
                    payload=RunTextDeltaPayload(text=chunk, round=round),
                ),
            )

    async def publish_persisted(self, run_id: str, execution_epoch: int, event: RunEvent) -> None:
        await self._publish_event(
            run_id,
            RunPersistedEvent(
                run_id=run_id,
                execution_epoch=execution_epoch,
                stream_sequence=0,
                payload=RunPersistedEventPayload(event=event),
            ),
        )

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncGenerator[RunEventSubscription, None]:
        """为每个 SSE 连接直连独立 Pub/Sub 订阅，退出时干净退订并关闭连接。"""
        channel = self._channel(run_id)
        pubsub = self._client.pubsub()
        try:
            async with asyncio.timeout(1.0):
                await pubsub.subscribe(channel)
        except BaseException as error:
            with suppress(RedisError, TimeoutError, OSError):
                async with asyncio.timeout(1.0):
                    await cast(Callable[[], Awaitable[None]], pubsub.aclose)()
            if isinstance(error, (RedisError, TimeoutError)):
                raise RunEventTransportError("run_stream_subscribe_failed") from error
            raise
        subscription = _RedisRunSubscription(pubsub, channel)
        try:
            yield subscription
        finally:
            await subscription.aclose()

    async def aclose(self) -> None:
        """仅当 owns_client=True 时关闭底层 Redis 客户端。"""
        if self._owns_client:
            with suppress(RedisError, TimeoutError, OSError):
                async with asyncio.timeout(1.0):
                    await self._client.aclose()

    async def _publish_event(self, run_id: str, event: RunStreamEvent) -> None:
        raw = event.model_dump_json(exclude_none=True).encode("utf-8")
        if len(raw) > MAX_REDIS_EVENT_BYTES:
            raise ValueError("run_stream_event_exceeds_redis_limit")
        try:
            async with asyncio.timeout(1.0):
                await cast(Awaitable[int], self._client.publish(self._channel(run_id), raw))
        except (RedisError, TimeoutError) as error:
            raise RunEventTransportError("run_stream_publish_failed") from error

    @staticmethod
    def _split_text_chunks(text: str) -> list[str]:
        """按固定字符数切分文本，避免切断 UTF-8 多字节字符且确保单帧严格 <= 64 KiB。"""
        if not text:
            return [""]
        return [
            text[index : index + _TEXT_CHUNK_CHARS]
            for index in range(0, len(text), _TEXT_CHUNK_CHARS)
        ]

    @staticmethod
    def _channel(run_id: str) -> str:
        return f"athena:run:events:{run_id}"


__all__ = ["RedisRunTransport"]
