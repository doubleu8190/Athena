"""Durable worker for memory write jobs."""

from __future__ import annotations

import asyncio

from athena.infrastructure.sqlite.repositories.memory_job_repository import MemoryJobRepository
from .contracts import CompletedTurn
from .write_workflow import MemoryWriteWorkflow


class MemoryWriteJobWorker:
    def __init__(
        self,
        repository: MemoryJobRepository,
        workflow: MemoryWriteWorkflow | None,
        *,
        poll_interval: float = 1.0,
    ) -> None:
        self._repository = repository
        self._workflow = workflow
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            await self._repository.recover_interrupted_jobs()
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="athena-memory-worker")

    def configure_workflow(self, workflow: MemoryWriteWorkflow) -> None:
        """Attach the workflow before starting the worker."""
        self._workflow = workflow

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self) -> None:
        while not self._stop.is_set():
            job = await self._repository.claim_next_job()
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    continue
                continue
            try:
                if self._workflow is None:
                    raise RuntimeError("memory job worker workflow is not configured")
                turn = CompletedTurn.model_validate(job)
                await self._workflow.process_turn(turn)
                await self._repository.mark_job_succeeded(turn.turn_id)
            except Exception as exc:
                attempt = int(job.get("attempt", 1))
                await self._repository.mark_job_failed(
                    job["turn_id"], str(exc), retry=attempt < 5
                )
