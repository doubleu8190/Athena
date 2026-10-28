"""记忆后台任务消费者。"""

from __future__ import annotations

import asyncio
from domain.leases import LeaseCenterPort, LeaseLostError

from application.memory.workflow import MemoryWriteWorkflow
from domain.memory import MemoryJobQueue
from workers.lease_guard import LeaseGuard


class MemoryWorker:
    """可停止、可恢复的记忆后台任务消费者。"""

    def __init__(
        self,
        jobs: MemoryJobQueue,
        workflow: MemoryWriteWorkflow,
        *,
        poll_interval: float = 1.0,
        max_attempts: int = 3,
        lease_seconds: float = 60.0,
        lease_center: LeaseCenterPort | None = None,
    ) -> None:
        self._jobs = jobs
        self._workflow = workflow
        self._poll_interval = poll_interval
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._leases = lease_center or getattr(jobs, "_leases")
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """持续领取任务，失败时交给队列决定是否重试。"""
        while not self._stop.is_set():
            job = await self._jobs.claim_next_job(
                lease_seconds=self._lease_seconds
            )
            if job is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), self._poll_interval)
                except asyncio.TimeoutError:
                    continue
                continue
            turn = job.turn
            try:
                async def process_and_commit() -> None:
                    try:
                        await self._workflow.process_completed_turn(
                            turn, lease=job.lease
                        )
                    except LeaseLostError:
                        raise
                    except Exception as exc:
                        await self._jobs.mark_failed(
                            turn.turn_id,
                            str(exc),
                            retry=not isinstance(exc, ValueError) and job.attempt < self._max_attempts,
                            lease=job.lease,
                        )
                        return
                    await self._jobs.mark_succeeded(
                        turn.turn_id, lease=job.lease
                    )

                await LeaseGuard(
                    self._leases,
                    [job.lease],
                    lease_seconds=self._lease_seconds,
                ).run(process_and_commit())
            except LeaseLostError:
                await self._leases.acknowledge_stopped(job.lease)
                continue

    def stop(self) -> None:
        """Request shutdown after the current job finishes."""
        self._stop.set()


__all__ = ["MemoryWorker"]
