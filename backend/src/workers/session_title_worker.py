"""恢复并执行持久化的首轮会话命名任务。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from application.sessions.title_service import SessionTitleService
from domain.sessions import SessionTitleJob, SessionTitleJobRepository
from shared.logging import get_logger

logger = get_logger(__name__)


class SessionTitleWorker:
    """短轮询持久队列；每项最多调用三次模型。"""

    _RETRY_DELAYS = (5, 30)

    def __init__(
        self,
        jobs: SessionTitleJobRepository,
        titles: SessionTitleService,
        *,
        poll_interval: float = 1.0,
        lease_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._jobs = jobs
        self._titles = titles
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._worker_id = f"session-title:{uuid4()}"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动后台消费循环。"""
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self.run(), name="athena-session-title-worker"
            )

    async def stop(self) -> None:
        """停止轮询并取消等待中的退避。"""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run(self) -> None:
        """持续领取任务；未处理完的任务在停止或崩溃后可由租约恢复。"""
        while not self._stop.is_set():
            try:
                job = await self._jobs.claim_next(
                    now=self._clock(),
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("session_title_claim_failed", exc_info=True)
                job = None
            if job is None:
                await self._wait(self._poll_interval)
                continue
            await self._process(job)

    async def _process(self, job: SessionTitleJob) -> None:
        try:
            title = await self._titles.generate(job)
            await self._jobs.complete(job, title=title, now=self._clock())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failed_at = self._clock()
            retry_at = (
                failed_at + timedelta(seconds=self._RETRY_DELAYS[job.attempt - 1])
                if job.attempt <= len(self._RETRY_DELAYS)
                else None
            )
            error_code = (
                "source_message_missing"
                if isinstance(error, LookupError)
                else "title_generation_failed"
            )
            try:
                await self._jobs.fail(
                    job,
                    error_code=error_code,
                    retry_at=retry_at,
                    now=failed_at,
                )
            except Exception:
                logger.error(
                    "session_title_failure_persist_failed",
                    session_id=job.session_id,
                    exc_info=True,
                )
            logger.warning(
                "session_title_generation_failed",
                session_id=job.session_id,
                attempt=job.attempt,
                error_code=error_code,
            )

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


__all__ = ["SessionTitleWorker"]
