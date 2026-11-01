"""将已完成 Plan 的持久恢复作业交付给真实 Root Run 执行器。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from application.orchestration.protocols import RootGraphPort
from application.runs.execution_service import RunExecutionService
from domain.leases import LeaseCenterPort, LeaseGrant, LeaseLostError
from domain.orchestration import RootPlanResumeLease, RootResumeJobRepository
from domain.runs import (
    CommandType,
    RootPlanCheckpointRecovery,
    RootPlanCompletedResumeValue,
    RootPlanWait,
    RunCommand,
    RunLifecyclePort,
    RunQueryPort,
    RunResumePayload,
    RunStatus,
)
from shared.logging import get_logger
from workers.lease_guard import LeaseGuard

logger = get_logger(__name__)


class RootResumeDispatcher:
    """领取完成作业并在双重 fencing 租约下恢复 Root Graph。"""

    def __init__(
        self,
        jobs: RootResumeJobRepository,
        graph: RootGraphPort,
        execution: RunExecutionService,
        lifecycle: RunLifecyclePort,
        runs: RunQueryPort,
        *,
        worker_id: str,
        lease_seconds: float = 60.0,
        poll_interval: float = 0.25,
        retry_delay: float = 0.5,
        lease_center: LeaseCenterPort,
    ) -> None:
        """保存恢复队列、实际 Root 执行器、Run 状态和租约中心。"""
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker identity and positive lease are required")
        self._jobs = jobs
        self._graph = graph
        self._execution = execution
        self._lifecycle = lifecycle
        self._runs = runs
        self._leases = lease_center
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._retry_delay = retry_delay
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """启动单个队列轮询协程。"""
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="athena-root-resume-dispatcher")

    async def stop(self) -> None:
        """停止领取并等待正在运行的恢复协程退出。"""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        pending = tuple(self._inflight)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self) -> None:
        """轮询并发领取持久 Root 恢复作业。"""
        while not self._stop.is_set():
            claimed = False
            try:
                while not self._stop.is_set() and len(self._inflight) < 8:
                    lease = await self._jobs.claim_next(self._worker_id, lease_seconds=self._lease_seconds)
                    if lease is None:
                        break
                    task = asyncio.create_task(self._resume(lease), name=f"athena-root-resume:{lease.job_id}")
                    self._inflight.add(task)
                    task.add_done_callback(self._on_task_done)
                    claimed = True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("root_resume_job_claim_failed", exc_info=True)
            if not claimed or len(self._inflight) >= 8:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass

    async def _resume(self, lease: RootPlanResumeLease) -> None:
        """持有恢复作业租约期间协调恢复并同时持有 Run 执行租约。"""
        try:
            await LeaseGuard(self._leases, [lease.lease], lease_seconds=self._lease_seconds).run(self._resume_claimed(lease))
        except LeaseLostError:
            logger.warning("root_resume_job_lease_lost", job_id=lease.job_id, run_id=lease.run_id)
            await self._leases.acknowledge_stopped(lease.lease)

    async def _resume_claimed(self, lease: RootPlanResumeLease) -> None:
        """恢复匹配 checkpoint；所有 DB 与 graph 动作都受 Run lease fencing。"""
        run_lease = await self._lifecycle.acquire_execution_lease(
            lease.run_id,
            owner=f"root-resume:{self._worker_id}:{uuid4().hex}",
            lease_seconds=self._lease_seconds,
        )
        if run_lease is None:
            current = await self._runs.get(lease.run_id)
            if current is None or current.status != RunStatus.RUNNING:
                await self._jobs.complete(lease)
            else:
                await self._jobs.retry(lease, delay_seconds=self._retry_delay)
            return
        try:
            await LeaseGuard(
                self._leases,
                [lease.lease, run_lease],
                lease_seconds=self._lease_seconds,
            ).run(self._resume_under_run_lease(lease, run_lease))
        except LeaseLostError:
            logger.warning("root_resume_execution_lease_lost", job_id=lease.job_id, run_id=lease.run_id)
            await self._jobs.retry(lease, delay_seconds=self._retry_delay)
        finally:
            await self._leases.acknowledge_stopped(run_lease)

    async def _resume_under_run_lease(self, lease: RootPlanResumeLease, run_lease: LeaseGrant) -> None:
        """核对 checkpoint 后调用真实 RunExecutionService 并收敛重复作业。"""
        if not await self._jobs.is_wait_current(lease):
            await self._jobs.complete(lease)
            return
        interrupt_id = lease.interrupt_id
        checkpoint_id = lease.checkpoint_id
        checkpoint_recovery = False
        if interrupt_id is None or checkpoint_id is None:
            recovered = await self._graph.find_pending_plan_wait(
                run_id=lease.run_id,
                plan_id=lease.plan_id,
                plan_sequence=lease.sequence,
                root_wait_generation=lease.root_wait_generation,
            )
            if recovered is None:
                checkpoint_recovery = await self._graph.is_plan_checkpoint_recoverable(
                    run_id=lease.run_id,
                    plan_id=lease.plan_id,
                    plan_sequence=lease.sequence,
                    root_wait_generation=lease.root_wait_generation,
                )
                if not checkpoint_recovery:
                    await self._jobs.retry(lease, delay_seconds=self._retry_delay)
                    return
            else:
                interrupt_id, checkpoint_id = recovered
                wait = RootPlanWait(
                    run_id=lease.run_id,
                    plan_id=lease.plan_id,
                    plan_sequence=lease.sequence,
                    root_wait_generation=lease.root_wait_generation,
                    interrupt_id=interrupt_id,
                    checkpoint_id=checkpoint_id,
                )
                if not await self._lifecycle.arm_root_wait(lease.run_id, wait=wait, lease=run_lease):
                    await self._jobs.retry(lease, delay_seconds=self._retry_delay)
                    return
        elif not await self._graph.has_pending_plan_wait(
            run_id=lease.run_id,
            plan_id=lease.plan_id,
            plan_sequence=lease.sequence,
            root_wait_generation=lease.root_wait_generation,
            interrupt_id=interrupt_id,
            checkpoint_id=checkpoint_id,
        ):
            checkpoint_recovery = await self._graph.is_plan_checkpoint_recoverable(
                run_id=lease.run_id,
                plan_id=lease.plan_id,
                plan_sequence=lease.sequence,
                root_wait_generation=lease.root_wait_generation,
            )
            if not checkpoint_recovery:
                await self._converge_or_retry(lease)
                return
        command = RunCommand(
            command_id=f"root-plan-resume:{lease.run_id}:{lease.plan_id}:{lease.root_wait_generation}",
            command_type=CommandType.RUN_RESUME,
            session_id=lease.session_id,
            run_id=lease.run_id,
            payload=RunResumePayload(resume_value=(
                RootPlanCheckpointRecovery(
                    type="plan_checkpoint_recovery",
                    run_id=lease.run_id,
                    plan_id=lease.plan_id,
                    plan_sequence=lease.sequence,
                    root_wait_generation=lease.root_wait_generation,
                )
                if checkpoint_recovery
                else RootPlanCompletedResumeValue(
                    type="plan_completed",
                    run_id=lease.run_id,
                    plan_id=lease.plan_id,
                    plan_sequence=lease.sequence,
                    root_wait_generation=lease.root_wait_generation,
                    interrupt_id=interrupt_id,
                )
            )),
            issued_at=datetime.now(timezone.utc),
            lease=run_lease,
        )
        try:
            await self._execution.execute(command)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("root_plan_resume_failed", job_id=lease.job_id, exc_info=True)
            await self._jobs.retry(lease, delay_seconds=self._retry_delay)
            return
        await self._converge_or_retry(lease)

    async def _converge_or_retry(self, lease: RootPlanResumeLease) -> None:
        """仅在 Root 已推进等待代次或 Run 终态后完成原恢复作业。"""
        if not await self._jobs.is_wait_current(lease):
            await self._jobs.complete(lease)
        else:
            await self._jobs.retry(lease, delay_seconds=self._retry_delay)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """移除已完成协程并记录未处理异常。"""
        self._inflight.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("root_resume_job_failed_before_convergence", exc_info=error)


__all__ = ["RootResumeDispatcher"]
