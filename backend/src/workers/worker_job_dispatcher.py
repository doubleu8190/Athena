"""从 PostgreSQL 租约队列执行持久 Worker Job。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from application.orchestration import WorkerApprovalService
from application.orchestration.protocols import GraphThreadLock, WorkerExecutor
from application.orchestration.worker_job_service import WorkerJobService
from domain.orchestration import (
    TaskExecution,
    TaskResult,
    TaskStatus,
    TaskEventRepository,
    WorkerJobLease,
    WorkerTaskTerminalEvent,
)
from shared.logging import get_logger

logger = get_logger(__name__)


class WorkerJobDispatcher:
    """并发领取、续租并提交 Worker 终态事件。"""

    def __init__(
        self,
        jobs: WorkerJobService,
        worker: WorkerExecutor,
        task_events: TaskEventRepository,
        *,
        worker_id: str,
        lease_seconds: float,
        max_concurrency: int,
        approval_service: WorkerApprovalService,
        thread_lock: GraphThreadLock,
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
        self._task_events = task_events
        self._approval_service = approval_service
        self._thread_lock = thread_lock
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
                logger.error("worker_job_claim_failed", exc_info=True)
            if not claimed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def _execute(self, lease: WorkerJobLease) -> None:
        """运行已领取的 Worker，并将审批或终态交给对应边界处理。"""
        result = await self._run_with_lease(lease)
        if result is None:
            await self._acknowledge_lost_lease(lease)
            return
        terminal_candidate = await self._request_approval_if_needed(lease, result)
        if terminal_candidate is None:
            return

        terminal_result = self._terminal_result(terminal_candidate, lease.task_id)
        await self._commit_terminal_event(lease, terminal_result)

    async def _run_with_lease(
        self, lease: WorkerJobLease
    ) -> TaskResult | None:
        """在心跳续租期间等待 Worker；租约丢失时返回 None。

        Args:
            lease: 当前 Worker Job 的所有权凭据。

        Returns:
            Worker 的执行结果；租约丢失时返回 None。

        Raises:
            asyncio.CancelledError: Dispatcher 主动关闭且租约仍有效时。
        """
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
        result: TaskResult | None = None
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
                        status=TaskStatus.FAILED,
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
                logger.error(
                    "worker_execution_failed",
                    execution_id=lease.execution_id,
                    exc_info=True,
                )
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

        return None if lease_lost.is_set() else result

    async def _acknowledge_lost_lease(self, lease: WorkerJobLease) -> None:
        """通知队列租约已失效，避免提交旧 Worker 的终态结果。"""
        try:
            await self._jobs.acknowledge_cancelled(lease)
        except Exception:
            logger.error(
                "worker_cancellation_acknowledgement_failed",
                execution_id=lease.execution_id,
                exc_info=True,
            )

    async def _request_approval_if_needed(
        self, lease: WorkerJobLease, result: TaskResult
    ) -> TaskResult | None:
        """提交审批请求，或返回应进入终态事件的结果。

        Args:
            lease: 当前 Worker Job 的所有权凭据。
            result: Worker 执行结果。

        Returns:
            需要持久化为终态的结果；已交给审批流程时返回 None。
        """
        if result.status == TaskStatus.WAITING_APPROVAL:
            if self._approval_service is None:
                return TaskResult(
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
                        "worker_approval_request_rejected",
                        execution_id=lease.execution_id,
                    )
                return None
        return result

    async def _commit_terminal_event(
        self, lease: WorkerJobLease, result: TaskResult
    ) -> None:
        """把 Worker 结果转换为终态事件并交给持久化命令边界。"""
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
        receipt = await self._task_events.apply_worker_terminal_event(event)
        if not receipt.accepted:
            logger.warning(
                "worker_terminal_event_rejected",
                execution_id=lease.execution_id,
                reason=receipt.reason,
            )

    async def _run_worker(
        self, lease: WorkerJobLease, execution: TaskExecution
    ) -> TaskResult:
        """Recover a saved approval interrupt before starting a reclaimed Job."""
        if self._thread_lock is not None:
            async with self._thread_lock.hold(lease.worker_thread_id):
                return await self._invoke_worker(lease, execution)
        return await self._invoke_worker(lease, execution)

    async def _invoke_worker(
        self, lease: WorkerJobLease, execution: TaskExecution
    ) -> TaskResult:
        """Invoke the Worker only while its thread lock is held."""
        if lease.job_type == "resume_worker":
            return await self._worker.resume(execution, dict(lease.resume_value))
        pending = await self._worker.pending_interrupt(execution)
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
                logger.error(
                    "worker_lease_renewal_failed",
                    execution_id=lease.execution_id,
                    exc_info=True,
                )
                renewed = None
            if renewed is None:
                lease_lost.set()
                worker_task.cancel()
                return
            current = renewed

    def _terminal_result(self, result: TaskResult, task_id: str) -> TaskResult:
        """校验 Worker 终态；未实现的审批中断安全失败。"""
        if result.task_id != task_id:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error={
                    "code": "worker_result_task_mismatch",
                    "reported_task_id": result.task_id,
                },
            )
        if result.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        }:
            return result
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
            logger.error(
                "worker_job_failed_before_event_persistence", exc_info=error
            )

def _mapping(value: Any) -> dict[str, Any]:
    """返回 JSON 风格的字典值。"""
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = ["WorkerJobDispatcher"]
