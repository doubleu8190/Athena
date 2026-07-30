"""Simple asyncio-based task scheduler — replaces ARQ (Redis-based task queue).

Provides fire-and-forget background task execution without external
broker dependencies. Tasks run as asyncio Tasks within the application
event loop.

Usage:
    from athena.tasks.scheduler import get_scheduler
    scheduler = get_scheduler()
    await scheduler.enqueue(extract_conversation_insights, session_id, user_id)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from athena.logging_config import get_logger

logger = get_logger(__name__)


class TaskScheduler:
    """Lightweight asyncio task scheduler.

    Replaces ARQ by running background tasks directly on the event loop.
    Supports task tracking, graceful shutdown, and basic error handling.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()
        self._running = True

    async def enqueue(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Enqueue a coroutine function for background execution.

        Returns a job ID for tracking. The task is wrapped with error
        handling and automatic cleanup on completion.
        """
        job_id = str(uuid.uuid4())

        async def _run_job() -> Any:
            try:
                result = await func(*args, **kwargs)
                logger.info(
                    "scheduler_task_completed",
                    job_id=job_id,
                    func=getattr(func, "__name__", str(func)),
                )
                return result
            except Exception:
                logger.exception(
                    "scheduler_task_failed",
                    job_id=job_id,
                    func=getattr(func, "__name__", str(func)),
                )
                raise
            finally:
                async with self._lock:
                    self._tasks.pop(job_id, None)

        task = asyncio.create_task(_run_job())
        async with self._lock:
            self._tasks[job_id] = task

        logger.info(
            "scheduler_task_enqueued",
            job_id=job_id,
            func=getattr(func, "__name__", str(func)),
            pending=self.pending_count,
        )
        return job_id

    @property
    def pending_count(self) -> int:
        """Number of tasks currently pending or running."""
        return len(self._tasks)

    async def shutdown(self) -> None:
        """Cancel all pending tasks and wait for them to complete."""
        self._running = False
        async with self._lock:
            tasks = list(self._tasks.values())

        if tasks:
            logger.info("scheduler_shutdown_cancelling", count=len(tasks))
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("scheduler_shutdown_complete")


# ── Singleton ──────────────────────────────────────────────────────

_scheduler: TaskScheduler | None = None


def get_scheduler() -> TaskScheduler:
    """Get or create the default task scheduler singleton."""
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler


async def close_scheduler() -> None:
    """Shut down the default scheduler. Called during shutdown."""
    global _scheduler
    if _scheduler is not None:
        await _scheduler.shutdown()
        _scheduler = None
