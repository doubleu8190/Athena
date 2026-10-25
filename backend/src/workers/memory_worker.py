"""记忆后台任务消费者。"""

from __future__ import annotations

import asyncio

from application.memory.workflow import MemoryWriteWorkflow
from domain.memory import MemoryJobQueue


class MemoryWorker:
    """可停止、可恢复的记忆后台任务消费者。"""

    def __init__(
        self,
        jobs: MemoryJobQueue,
        workflow: MemoryWriteWorkflow,
        *,
        poll_interval: float = 1.0,
        max_attempts: int = 3,
    ) -> None:
        self._jobs = jobs
        self._workflow = workflow
        self._poll_interval = poll_interval
        self._max_attempts = max_attempts
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """持续领取任务，失败时交给队列决定是否重试。"""
        await self._jobs.recover()
        while not self._stop.is_set():
            job = await self._jobs.claim_next_job()
            if job is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), self._poll_interval)
                except asyncio.TimeoutError:
                    continue
                continue
            turn = job.turn
            try:
                await self._workflow.process_completed_turn(turn)
            except Exception as exc:
                await self._jobs.mark_failed(
                    turn.turn_id,
                    str(exc),
                    retry=job.attempt < self._max_attempts,
                )
            else:
                await self._jobs.mark_succeeded(turn.turn_id)

    def stop(self) -> None:
        """Request shutdown after the current job finishes."""
        self._stop.set()


__all__ = ["MemoryWorker"]
