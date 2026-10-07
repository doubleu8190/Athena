"""记忆后台任务消费者。"""

from __future__ import annotations

import asyncio

from application.memory.workflow import MemoryWriteWorkflow
from domain.memory import CompletedTurn, MemoryJobPort


class MemoryWorker:
    """可停止、可恢复的记忆后台任务消费者。"""

    def __init__(self, jobs: MemoryJobPort, workflow: MemoryWriteWorkflow, *, poll_interval: float = 1.0) -> None:
        self._jobs = jobs
        self._workflow = workflow
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """持续领取任务，失败时交给队列决定是否重试。"""
        await self._jobs.recover()
        while not self._stop.is_set():
            job = await self._jobs.claim()
            if job is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), self._poll_interval)
                except asyncio.TimeoutError:
                    continue
                continue
            turn = CompletedTurn(
                turn_id=str(job["turn_id"]),
                session_id=str(job["session_id"]),
                user_text=str(job.get("user_text", "")),
                assistant_text=str(job.get("assistant_text", "")),
            )
            try:
                await self._workflow.process(turn)
            except Exception as exc:
                await self._jobs.fail(turn.turn_id, str(exc), retry=True)
            else:
                await self._jobs.succeed(turn.turn_id)

    def stop(self) -> None:
        """请求 worker 在当前任务完成后停止。"""
        self._stop.set()


__all__ = ["MemoryWorker"]
