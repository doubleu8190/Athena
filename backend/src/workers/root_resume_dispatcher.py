"""Deliver durable Round completion jobs to suspended Root graphs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from application.orchestration.protocols import RootGraphPort
from domain.runs import CommandType, RunCommand
from shared.logging import get_logger

logger = get_logger(__name__)


class RootResumeDispatcher:
    """Resume Root checkpoints only after their matching interrupt is durable."""

    def __init__(
        self,
        jobs: Any,
        graph: RootGraphPort,
        execution: Any,
        runs: Any,
        *,
        worker_id: str,
        lease_seconds: float = 60.0,
        poll_interval: float = 0.25,
        retry_delay: float = 0.5,
    ) -> None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker identity and positive lease are required")
        self._jobs = jobs
        self._graph = graph
        self._execution = execution
        self._runs = runs
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._retry_delay = retry_delay
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="athena-root-resume-dispatcher")

    async def stop(self) -> None:
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
        while not self._stop.is_set():
            claimed = False
            try:
                while not self._stop.is_set():
                    lease = await self._jobs.claim_next(
                        self._worker_id, lease_seconds=self._lease_seconds
                    )
                    if lease is None:
                        break
                    task = asyncio.create_task(
                        self._resume(lease),
                        name=f"athena-root-resume:{lease.job_id}",
                    )
                    self._inflight.add(task)
                    task.add_done_callback(self._on_task_done)
                    claimed = True
                    if len(self._inflight) >= 8:
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("root_resume_job_claim_failed", exc_info=True)
            if not claimed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def _resume(self, lease: Any) -> None:
        interrupt_id = lease.interrupt_id
        checkpoint_id = lease.checkpoint_id
        if not interrupt_id or not checkpoint_id:
            # Root checkpoint 与 Run 行登记分属两个持久化动作。进程若在两者
            # 之间退出，Resume Job 可从 checkpoint 找回中断身份并补登记。
            find_pending = getattr(self._graph, "find_pending_round_wait", None)
            arm_root_wait = getattr(self._runs, "arm_root_wait", None)
            if not callable(find_pending) or not callable(arm_root_wait):
                await self._jobs.retry(lease, delay_seconds=self._retry_delay)
                return
            try:
                recovered = await find_pending(
                    run_id=lease.run_id,
                    plan_id=lease.plan_id,
                    round_id=lease.round_id,
                    plan_version=lease.plan_version,
                )
            except Exception:
                logger.error(
                    "root_checkpoint_recovery_failed",
                    job_id=lease.job_id,
                    exc_info=True,
                )
                await self._jobs.retry(lease, delay_seconds=self._retry_delay)
                return
            if (
                not isinstance(recovered, tuple)
                or len(recovered) != 2
                or not all(isinstance(value, str) and value for value in recovered)
            ):
                await self._jobs.retry(lease, delay_seconds=self._retry_delay)
                return
            interrupt_id, checkpoint_id = recovered
            if not await arm_root_wait(
                lease.run_id,
                plan_id=lease.plan_id,
                round_id=lease.round_id,
                plan_version=lease.plan_version,
                interrupt_id=interrupt_id,
                checkpoint_id=checkpoint_id,
            ):
                await self._jobs.retry(lease, delay_seconds=self._retry_delay)
                return
        try:
            has_pending = await self._graph.has_pending_round_wait(
                run_id=lease.run_id,
                plan_id=lease.plan_id,
                round_id=lease.round_id,
                plan_version=lease.plan_version,
                interrupt_id=interrupt_id,
                checkpoint_id=checkpoint_id,
            )
        except Exception:
            logger.error(
                "root_checkpoint_inspection_failed",
                job_id=lease.job_id,
                exc_info=True,
            )
            await self._jobs.retry(lease, delay_seconds=self._retry_delay)
            return
        if not has_pending:
            await self._jobs.retry(lease, delay_seconds=self._retry_delay)
            return

        command = RunCommand(
            command_id=f"root-resume:{lease.job_id}:{lease.root_wait_generation}",
            command_type=CommandType.RUN_RESUME,
            session_id=lease.session_id,
            run_id=lease.run_id,
            payload={
                "message": "",
                "message_id": f"root-resume-result:{lease.job_id}",
                "resume_value": {
                    "type": "round_completed",
                    "run_id": lease.run_id,
                    "plan_id": lease.plan_id,
                    "round_id": lease.round_id,
                    "plan_version": lease.plan_version,
                    "root_wait_generation": lease.root_wait_generation,
                    "interrupt_id": interrupt_id,
                },
            },
            issued_at=datetime.now(timezone.utc),
        )
        invocation = asyncio.create_task(
            self._execution.execute(command),
            name=f"athena-root-resume-call:{lease.run_id}",
        )
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._maintain_lease(lease, invocation, lease_lost),
            name=f"athena-root-resume-heartbeat:{lease.job_id}",
        )
        try:
            await invocation
        except asyncio.CancelledError:
            if not lease_lost.is_set():
                raise
            return
        except Exception:
            logger.error(
                "root_checkpoint_resume_failed",
                job_id=lease.job_id,
                exc_info=True,
            )
            await self._jobs.retry(lease, delay_seconds=self._retry_delay)
            return
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        if lease_lost.is_set():
            return
        try:
            run = await self._runs.get(lease.run_id)
            raw_status = getattr(run, "status", None)
            status = str(getattr(raw_status, "value", raw_status or ""))
            if status == "running":
                await self._jobs.retry(lease, delay_seconds=self._retry_delay)
            else:
                await self._jobs.complete(lease)
        except Exception:
            logger.error(
                "root_resume_job_convergence_failed",
                job_id=lease.job_id,
                exc_info=True,
            )
            await self._jobs.retry(lease, delay_seconds=self._retry_delay)

    async def _maintain_lease(
        self, lease: Any, invocation: asyncio.Task[Any], lease_lost: asyncio.Event
    ) -> None:
        current = lease
        interval = max(0.1, self._lease_seconds / 3)
        while not invocation.done():
            await asyncio.sleep(interval)
            try:
                renewed = await self._jobs.renew(
                    current, lease_seconds=self._lease_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(
                    "root_resume_lease_renewal_failed",
                    job_id=lease.job_id,
                    exc_info=True,
                )
                renewed = None
            if renewed is None:
                lease_lost.set()
                invocation.cancel()
                return
            current = renewed

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "root_resume_job_failed_before_convergence", exc_info=error
            )


__all__ = ["RootResumeDispatcher"]
