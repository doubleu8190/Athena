from __future__ import annotations

import asyncio


class MemoryWorker:
    """Process durable memory jobs through an injected workflow."""

    def __init__(self, jobs, workflow, *, poll_interval: float = 1.0) -> None:
        self._jobs = jobs
        self._workflow = workflow
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await self._jobs.recover()
        self._stop.clear()

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self._jobs.recover()
        while not self._stop.is_set():
            job = await self._jobs.claim()
            if job is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), self._poll_interval)
                except asyncio.TimeoutError:
                    continue
                continue
            try:
                await self._workflow.process(job)
                await self._jobs.succeed(str(job["turn_id"]))
            except Exception as exc:
                await self._jobs.fail(str(job["turn_id"]), str(exc), retry=True)


__all__ = ["MemoryWorker"]
