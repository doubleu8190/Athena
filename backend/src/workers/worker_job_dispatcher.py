"""从 PostgreSQL 租约队列执行持久 Worker Job。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from application.orchestration.worker_job_service import WorkerJobService
from domain.orchestration import (
    TaskExecution,
    TaskResult,
    TaskStatus,
    WorkerJobLease,
    WorkerTaskTerminalEvent,
    WorkerExecutor,
)

logger = logging.getLogger(__name__)


class WorkerJobDispatcher:
    """并发领取、续租并提交 Worker 终态事件。"""

    def __init__(
        self,
        jobs: WorkerJobService,
        worker: WorkerExecutor,
        event_handler: Any,
        *,
        worker_id: str,
        lease_seconds: float,
        max_concurrency: int,
        approval_service: Any | None = None,
        poll_interval: float = 0.25,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存依赖和运行参数；执行器调用始终在数据库事务之外。"""
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds <= 0 or max_concurrency < 1 or poll_interval <= 0:
            raise ValueError("worker lease, concurrency, and polling must be positive")
        self._jobs = jobs
        self._worker = worker
        self._event_handler = event_handler
        self._approval_service = approval_service
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_concurrency = max_concurrency
        self._poll_interval = poll_interval
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """启动独立的持久 Job 拉取循环。"""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stop.clear()
        self._loop_task = asyncio.create_task(
            self.run(), name=f"athena-worker-job-dispatcher:{self._worker_id}"
        )

    async def stop(self) -> None:
        """停止领取并取消本进程中的 Graph 调用，让过期租约可被接管。"""
        self._stop.set()
        if self._loop_task is not None:
            await self._loop_task
            self._loop_task = None
        pending = tuple(self._inflight)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self) -> None:
        """持续领取作业；无工作时用短间隔轮询数据库。"""
        while not self._stop.is_set():
            claimed = False
            try:
                await self._jobs.reconcile_cancellations()
                while (
                    not self._stop.is_set()
                    and len(self._inflight) < self._max_concurrency
                ):
                    lease = await self._jobs.claim_next(self._worker_id)
                    if lease is None:
                        break
                    task = asyncio.create_task(
                        self._execute(lease),
                        name=f"athena-worker-execution:{lease.execution_id}",
                    )
                    self._inflight.add(task)
                    task.add_done_callback(self._on_task_done)
                    claimed = True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker Job claim failed")
            if not claimed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def _execute(self, lease: WorkerJobLease) -> None:
        execution = TaskExecution(
            plan_id=lease.plan_id,
            run_id=lease.run_id,
            session_id=lease.session_id,
            task=lease.task,
            execution_generation=lease.attempt,
            upstream_results=lease.upstream_results,
            execution_id=lease.execution_id,
            worker_thread_id=lease.worker_thread_id,
            fencing_token=lease.fencing_token,
            cancellation_epoch=lease.cancellation_epoch,
        )
        worker_call = self._run_worker(lease, execution)
        worker_task = asyncio.create_task(
            worker_call,
            name=f"athena-worker-graph:{lease.worker_thread_id}",
        )
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._maintain_lease(lease, worker_task, lease_lost),
            name=f"athena-worker-heartbeat:{lease.execution_id}",
        )
        try:
            try:
                timeout_scope = asyncio.timeout(lease.task.timeout_seconds)
                async with timeout_scope:
                    result = await worker_task
            except TimeoutError as exc:
                if timeout_scope.expired():
                    worker_task.cancel()
                    await asyncio.gather(worker_task, return_exceptions=True)
                    result = TaskResult(
                        task_id=lease.task_id,
                        status=TaskStatus.TIMED_OUT,
                        error={
                            "code": "task_timeout",
                            "timeout_seconds": lease.task.timeout_seconds,
                        },
                        retryable=True,
                    )
                else:
                    result = TaskResult(
                        task_id=lease.task_id,
                        status=TaskStatus.FAILED,
                        error={
                            "code": "worker_execution_failed",
                            "message": str(exc) or type(exc).__name__,
                        },
                        retryable=True,
                    )
            except asyncio.CancelledError:
                if not lease_lost.is_set():
                    raise
            except Exception as exc:
                logger.exception("Worker execution failed: %s", lease.execution_id)
                result = TaskResult(
                    task_id=lease.task_id,
                    status=TaskStatus.FAILED,
                    error={
                        "code": "worker_execution_failed",
                        "message": str(exc) or type(exc).__name__,
                    },
                    retryable=True,
                )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        if lease_lost.is_set():
            try:
                await self._jobs.acknowledge_cancelled(lease)
            except Exception:
                logger.exception(
                    "Worker cancellation acknowledgement failed: %s",
                    lease.execution_id,
                )
            return
        if result.status == TaskStatus.WAITING_APPROVAL:
            if self._approval_service is None:
                result = TaskResult(
                    task_id=lease.task_id,
                    status=TaskStatus.FAILED,
                    error={"code": "worker_approval_service_not_configured"},
                )
            else:
                accepted = await self._approval_service.request(
                    lease, result.approval_batch or {}
                )
                if not accepted:
                    logger.warning(
                        "Worker approval request rejected: execution=%s",
                        lease.execution_id,
                    )
                return
        result = self._terminal_result(result, lease.task_id)
        event = WorkerTaskTerminalEvent(
            message_id=self._id_factory(),
            occurred_at=self._clock(),
            worker_id=lease.worker_id,
            run_id=lease.run_id,
            plan_id=lease.plan_id,
            plan_version=lease.plan_version,
            round_id=lease.round_id,
            task_id=lease.task_id,
            execution_id=lease.execution_id,
            fencing_token=lease.fencing_token,
            cancellation_epoch=lease.cancellation_epoch,
            status=result.status,
            result={
                "output": _mapping(result.output),
                "evidence": [
                    dict(item)
                    if isinstance(item, Mapping)
                    else {"value": item}
                    for item in result.evidence
                ],
                "uncertainties": list(result.uncertainties),
                "metrics": _mapping(_mapping(result.output).get("metrics", {})),
            },
            error=dict(result.error) if result.error is not None else None,
            retryable=result.retryable,
        )
        receipt = await self._event_handler.apply_terminal_event(event)
        if not receipt.accepted:
            logger.warning(
                "Worker terminal event rejected: execution=%s reason=%s",
                lease.execution_id,
                receipt.reason,
            )

    async def _run_worker(
        self, lease: WorkerJobLease, execution: TaskExecution
    ) -> TaskResult:
        """Recover a saved approval interrupt before starting a reclaimed Job."""
        if lease.job_type == "resume_worker":
            return await self._worker.resume(execution, dict(lease.resume_value))
        recover = getattr(self._worker, "pending_interrupt", None)
        if callable(recover):
            pending = await recover(execution)
            if pending:
                return TaskResult(
                    task_id=lease.task_id,
                    status=TaskStatus.WAITING_APPROVAL,
                    approval_batch=pending,
                )
        return await self._worker.execute(execution)

    async def _maintain_lease(
        self,
        lease: WorkerJobLease,
        worker_task: asyncio.Task[TaskResult],
        lease_lost: asyncio.Event,
    ) -> None:
        """续租失败时中止当前调用，避免失去所有权后继续工作。"""
        current = lease
        interval = max(0.1, self._lease_seconds / 3)
        while not worker_task.done():
            await asyncio.sleep(interval)
            try:
                renewed = await self._jobs.renew_lease(current)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker lease renewal failed: %s", lease.execution_id)
                renewed = None
            if renewed is None:
                lease_lost.set()
                worker_task.cancel()
                return
            current = renewed

    def _terminal_result(self, result: TaskResult, task_id: str) -> TaskResult:
        """将旧 Worker 状态投影为新终态；未实现的审批中断安全失败。"""
        if result.task_id != task_id:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error={
                    "code": "worker_result_task_mismatch",
                    "reported_task_id": result.task_id,
                },
            )
        if result.status == TaskStatus.DONE:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                output=result.output,
                evidence=result.evidence,
                uncertainties=result.uncertainties,
                error=result.error,
                retryable=result.retryable,
            )
        if result.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
        }:
            return result
        if result.status == TaskStatus.WAITING_APPROVAL:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error={
                    "code": "approval_runtime_not_ready",
                    "message": "Worker approval dispatch is implemented in P4",
                },
            )
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.FAILED,
            error={
                "code": "invalid_worker_terminal_status",
                "status": result.status.value,
            },
        )

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Worker Job failed before event persistence", exc_info=error)

def _mapping(value: Any) -> dict[str, Any]:
    """返回 JSON 风格的字典值。"""
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = ["WorkerJobDispatcher"]
