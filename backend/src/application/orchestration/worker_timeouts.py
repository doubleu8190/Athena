"""负责将执行超时扫描转换为 Orchestrator 结果裁决。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from domain.orchestration.worker_entities import WorkerResultReceipt
from shared.logging import get_logger

logger = get_logger(__name__)


class WorkerTimeoutRepository(Protocol):
    """超时用例所需的候选读取和原子裁决端口。"""

    async def find_timeout_candidates(
        self, *, batch_size: int, queue_max_wait_seconds: float
    ) -> list[str]: ...

    async def timeout_execution(
        self, *, execution_id: str
    ) -> WorkerResultReceipt | None: ...

    async def expire_queued_execution(
        self, *, execution_id: str, queue_max_wait_seconds: float
    ) -> bool: ...


class WorkerTimeoutService:
    """协调超时扫描候选并交由结果事务复验。"""

    def __init__(self, repository: WorkerTimeoutRepository) -> None:
        """初始化超时裁决用例。

        参数：
        - `repository`（WorkerTimeoutRepository）：读取候选并以事务结果用例复验状态。

        返回：无。

        异常：本构造函数不主动抛出业务异常。
        """
        self._repository = repository

    async def scan_once(
        self, *, batch_size: int, queue_max_wait_seconds: float = 300.0
    ) -> int:
        """处理一批到期候选，业务状态只由仓储原子结果裁决更新。

        参数：
        - `batch_size`（int）：一次最多扫描的执行数量，必须为正数。

        返回：本轮实际裁决的超时数量；并发结果/取消已先行的候选不计数。

        异常：ValueError 表示批量大小无效；数据库错误原样传播。
        """
        if batch_size < 1 or queue_max_wait_seconds <= 0:
            raise ValueError("worker_timeout_batch_size_invalid")
        candidates = await self._repository.find_timeout_candidates(
            batch_size=batch_size,
            queue_max_wait_seconds=queue_max_wait_seconds,
        )
        handled = 0
        for execution_id in candidates:
            receipt = await self._repository.timeout_execution(execution_id=execution_id)
            if receipt is not None and receipt.disposition != "ignored":
                handled += 1
                continue
            if receipt is None and await self._repository.expire_queued_execution(
                execution_id=execution_id,
                queue_max_wait_seconds=queue_max_wait_seconds,
            ):
                handled += 1
        return handled


class WorkerTimeoutScanner:
    """以有界批次周期调用超时应用用例。"""

    def __init__(
        self,
        service: WorkerTimeoutService,
        *,
        interval_seconds: float,
        batch_size: int,
        queue_max_wait_seconds: float = 300.0,
    ) -> None:
        """配置可启停的扫描循环。

        参数：
        - `service`（WorkerTimeoutService）：唯一业务调用入口，不接触 ORM。
        - `interval_seconds`（float）：扫描间隔，必须大于零。
        - `batch_size`（int）：单次扫描上限，必须大于零。

        返回：无。

        异常：ValueError 表示扫描间隔或批量大小无效。
        """
        if interval_seconds <= 0 or batch_size < 1:
            raise ValueError("worker_timeout_scanner_configuration_invalid")
        self._service = service
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._queue_max_wait = queue_max_wait_seconds
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动唯一后台循环；重复调用保持幂等。"""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stop.clear()
        self._loop_task = asyncio.create_task(
            self.run(), name="athena-worker-timeout-scanner"
        )

    async def stop(self) -> None:
        """停止循环并等待当前批次安全结束。"""
        self._stop.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None

    async def scan_once(self) -> int:
        """手动执行一批扫描，供启动探测与集成测试使用。"""
        return await self._service.scan_once(
            batch_size=self._batch_size,
            queue_max_wait_seconds=self._queue_max_wait,
        )

    async def run(self) -> None:
        """持续扫描，数据库事务错误由下一轮重试恢复。"""
        while not self._stop.is_set():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 后台瞬时错误不终止进程；候选仍保存在数据库中，下一轮会重试。
                logger.exception("worker_timeout_scan_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval
                )
            except asyncio.TimeoutError:
                continue


__all__ = ["WorkerTimeoutScanner", "WorkerTimeoutService"]
