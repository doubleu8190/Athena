"""将实时消息增量合并为有界大小的事件。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from athena.contracts.events import ApplicationEvent, EventDurability, EventType


class StreamCoalescer:
    """按时间或字节阈值合并消息增量，并维护连续的字节偏移量。"""

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        stream_id: str,
        publish_realtime: Callable[[ApplicationEvent], Awaitable[object]],
        stream_type: str = "answer",
        message_id: str | None = None,
        interval_ms: int = 500,
        max_bytes: int = 512,
        initial_version: int = 0,
        initial_offset: int = 0,
    ) -> None:
        """创建一个流式增量合并器。

        参数:
            session_id (str): 所属会话 ID，必须非空。
            run_id (str): 所属运行 ID，必须非空。
            stream_id (str): 流 ID，必须在同一流生命周期内保持稳定。
            publish_realtime (Callable): 只向在线客户端广播事件的异步回调。
            interval_ms (int): 最长合并间隔，单位为毫秒，必须大于等于 0。
            max_bytes (int): 单次增量达到该 UTF-8 字节数时立即刷新，必须为正数。
        返回值:
            None: 合并器初始为空，版本号和偏移量均为 0。
        异常:
            参数不满足约束时不会在构造阶段主动校验；发布阶段的回调异常会原样传播。
        """
        self.session_id, self.run_id, self.stream_id = session_id, run_id, stream_id
        self.stream_type = stream_type
        self.message_id = message_id
        self.publish_realtime = publish_realtime
        self.interval = interval_ms / 1000
        self.max_bytes = max_bytes
        self._buffer = ""
        self._offset = initial_offset
        self._version = initial_version
        self._last_flush = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def version(self) -> int:
        """已分配的最后一个 Chunk 序号。"""
        return self._version

    @property
    def offset(self) -> int:
        """已发布文本的 UTF-8 字节偏移量。"""
        return self._offset

    async def append(self, text: str) -> None:
        """追加文本，并在达到刷新条件时发布实时增量事件。

        参数:
            text (str): 要追加的文本；空字符串会被忽略。
        返回值:
            None: 文本已缓存或已通过回调发布。
        异常:
            ``publish`` 回调抛出的异常会原样传播。
        """
        if not text:
            return
        async with self._lock:
            self._buffer += text
            if (
                len(self._buffer.encode("utf-8")) >= self.max_bytes
                or time.monotonic() - self._last_flush >= self.interval
            ):
                await self._flush_locked()

    async def flush(self, *, is_complete: bool = False) -> None:
        """立即刷新当前缓存的文本。

        参数:
            无。
        返回值:
            None: 缓存为空或已发布一个增量事件；``is_complete`` 只用于最后一个非空 Chunk。
        异常:
            ``publish`` 回调抛出的异常会原样传播。
        """
        async with self._lock:
            await self._flush_locked(is_complete=is_complete)

    async def _flush_locked(self, *, is_complete: bool = False) -> None:
        """在已持有锁的前提下发布缓存内容。

        参数:
            无。
        返回值:
            None: 空缓存不执行操作，否则更新版本、偏移量并发布事件。
        异常:
            ``publish`` 回调抛出的异常会原样传播；调用方必须先持有内部锁。
        """
        if not self._buffer:
            return
        delta = self._buffer
        start = self._offset
        self._offset += len(delta.encode("utf-8"))
        self._version += 1
        self._buffer = ""
        self._last_flush = time.monotonic()
        await self.publish_realtime(
            ApplicationEvent(
                event_type=EventType.LLM_TOKEN,
                durability=EventDurability.REALTIME,
                session_id=self.session_id,
                run_id=self.run_id,
                message_id=self.message_id,
                stream_id=self.stream_id,
                stream_type=self.stream_type,
                chunk_id=self._version,
                is_complete=is_complete,
                payload={
                    "stream_id": self.stream_id,
                    "message_id": self.message_id,
                    "chunk_id": self._version,
                    "base_version": self._version - 1,
                    "start_offset": start,
                    "end_offset": self._offset,
                    "delta": delta,
                },
            )
        )
