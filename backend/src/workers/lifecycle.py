"""Process-level lifecycle for target application workers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable


class WorkerSupervisor:
    """Start and stop a set of target workers as one bootstrap resource."""

    def __init__(self, workers: Iterable[object]) -> None:
        self._workers = tuple(workers)
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for worker in self._workers:
            callback = getattr(worker, "start", None)
            if callback is not None:
                result = callback()
                if result is not None:
                    await result
                continue
            run = getattr(worker, "run", None)
            if run is None:
                raise TypeError(f"worker has no start/run method: {type(worker).__name__}")
            self._tasks.append(asyncio.create_task(run(), name=f"athena-worker-{type(worker).__name__}"))

    async def stop(self) -> None:
        for worker in reversed(self._workers):
            callback = getattr(worker, "stop", None)
            if callback is not None:
                result = callback()
                if result is not None:
                    await result
        if self._tasks:
            results = await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result


__all__ = ["WorkerSupervisor"]
