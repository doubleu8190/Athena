"""有界、异步、只读的影子检索。"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Awaitable, Callable

from athena.evaluation.redaction import redact_request, sanitize_event
from athena.evaluation.storage import JsonlEventStore
from athena.utils.ids import generate_time_id


@dataclass(frozen=True)
class ShadowRequest:
    """脱敏后的 shadow 检索请求及其基线结果。"""

    event_id: str
    query: str
    query_hash: str
    query_length: int
    scope: dict[str, Any]
    baseline_ids: tuple[str, ...]
    labels: tuple[str, ...] = ()
    enqueued_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class ShadowStats:
    """记录 shadow 采样、排队、完成和失败计数。"""

    sampled: int = 0
    enqueued: int = 0
    completed: int = 0
    timed_out: int = 0
    failed: int = 0
    dropped: int = 0
    redaction_failed: int = 0


class ShadowRetrievalRunner:
    """队列不持有任何面向用户的结果，并始终强制执行只读检索。"""

    def __init__(
        self,
        executor: Callable[..., Awaitable[Any]],
        *,
        enabled: bool = False,
        sample_rate: float = 0.05,
        max_concurrency: int = 4,
        timeout_ms: int = 5000,
        queue_size: int = 1000,
        drop_on_overload: bool = True,
        store: JsonlEventStore | None = None,
        hash_salt: str = "shadow",
    ) -> None:
        """创建有界、只读的异步 shadow 执行器。

        参数：
            executor: 接受查询、范围和 ``record_access=False`` 的异步检索函数。
            enabled: 是否启用 shadow 流程。
            sample_rate: 采样比例，范围为 0 到 1。
            max_concurrency: 并发 worker 数，必须为正数。
            timeout_ms: 单次 shadow 检索超时时间。
            queue_size: 待处理队列容量。
            drop_on_overload: 队列满时是否丢弃请求。
            store: 可选事件存储。
            hash_salt: 查询哈希盐。

        异常：
            ValueError: ``sample_rate`` 超出 0 到 1。
        """
        if not 0 <= sample_rate <= 1:
            raise ValueError("sample_rate must be between 0 and 1")
        self.executor = executor
        self.enabled = enabled
        self.sample_rate = sample_rate
        self.timeout = timeout_ms / 1000
        self.drop_on_overload = drop_on_overload
        self.queue: asyncio.Queue[ShadowRequest] = asyncio.Queue(maxsize=queue_size)
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.store = store
        self.hash_salt = hash_salt
        self.stats = ShadowStats()
        self._workers: list[asyncio.Task[None]] = []
        self._pending_puts: set[asyncio.Task[None]] = set()
        self._max_concurrency = max_concurrency

    async def start(self) -> None:
        """启动 worker；重复调用不会创建重复 worker。"""
        if not self._workers:
            self._workers = [
                asyncio.create_task(self._worker())
                for _ in range(self._max_concurrency)
            ]

    async def submit(
        self,
        *,
        query: str,
        scope: dict[str, Any],
        baseline_ids: list[str],
        labels: list[str] | None = None,
    ) -> bool:
        """按采样率提交脱敏 shadow 请求。

        参数：
            query: 原始查询，仅在内存中用于执行和哈希。
            scope: 检索范围元数据。
            baseline_ids: 基线结果 ID。
            labels: 可选用例标签。

        返回值：
            请求成功入队时为 ``True``，未采样、脱敏失败或过载丢弃时为 ``False``。
        """
        if not self.enabled or random.random() >= self.sample_rate:
            return False
        await self.start()
        self.stats.sampled += 1
        try:
            redacted = redact_request(query, scope, salt=self.hash_salt)
        except ValueError:
            self.stats.redaction_failed += 1
            return False
        request = ShadowRequest(
            generate_time_id(),
            query,
            redacted.query_hash,
            redacted.query_length,
            dict(scope),
            tuple(baseline_ids),
            tuple(labels or ()),
        )
        try:
            self.queue.put_nowait(request)
        except asyncio.QueueFull:
            # shadow 检索只用于观测，不得反向阻塞用户请求；过载时按策略丢弃。
            self.stats.dropped += 1
            if self.drop_on_overload:
                return False
            task = asyncio.create_task(self.queue.put(request))
            self._pending_puts.add(task)
            task.add_done_callback(self._pending_puts.discard)
            self.stats.enqueued += 1
            return True
        self.stats.enqueued += 1
        return True

    async def _worker(self) -> None:
        """持续消费队列，并确保每个请求释放队列计数。"""
        while True:
            request = await self.queue.get()
            try:
                await self._run(request)
            finally:
                self.queue.task_done()

    async def _run(self, request: ShadowRequest) -> None:
        """执行一个 shadow 请求，记录成功、超时或异常事件。"""
        started = perf_counter()
        try:
            async with self.semaphore:
                result = await asyncio.wait_for(
                    self.executor(request.query, request.scope, record_access=False),
                    timeout=self.timeout,
                )

            def result_id(item: Any) -> str:
                """从不同结果表示中提取稳定的结果 ID。"""
                if isinstance(item, str):
                    return item
                if hasattr(item, "chunk_id"):
                    return str(item.chunk_id)
                if isinstance(item, dict):
                    return str(item.get("id", ""))
                return ""

            ids = tuple(result_id(item) for item in (result or ()))
            event = sanitize_event(
                {
                    "event_id": request.event_id,
                    "query_hash": request.query_hash,
                    "query_length": request.query_length,
                    "scope": request.scope,
                    "baseline_ids": request.baseline_ids,
                    "shadow_ids": ids,
                    "top1_changed": bool(request.baseline_ids[:1] != ids[:1]),
                    "latency_ms": (perf_counter() - started) * 1000,
                    "labels": request.labels,
                    "status": "completed",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self.stats.completed += 1
        except asyncio.TimeoutError:
            self.stats.timed_out += 1
            event = {
                "event_id": request.event_id,
                "query_hash": request.query_hash,
                "query_length": request.query_length,
                "status": "timeout",
            }
        except Exception as exc:
            self.stats.failed += 1
            event = {
                "event_id": request.event_id,
                "query_hash": request.query_hash,
                "query_length": request.query_length,
                "status": "failed",
                "error_type": type(exc).__name__,
            }
        if self.store is not None:
            try:
                self.store.append(event)
            except Exception:
                self.stats.failed += 1

    async def close(self) -> None:
        """等待队列排空、取消 worker 并释放资源。"""
        if self._pending_puts:
            await asyncio.gather(*self._pending_puts, return_exceptions=True)
        await self.queue.join()
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
