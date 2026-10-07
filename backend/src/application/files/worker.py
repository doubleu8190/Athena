"""知识文档任务 Worker。"""

from __future__ import annotations

from typing import Protocol

from .ingestion_service import IngestionService
import asyncio


class DocumentJobQueue(Protocol):
    async def claim_next(self, *, max_attempts: int = 5) -> dict[str, object] | None: ...
    async def mark_succeeded(self, job_id: str, result: dict[str, object]) -> None: ...
    async def mark_failed(self, job_id: str, error: str, *, retry: bool) -> None: ...


class KnowledgeDocumentWorker:
    """Worker 本身只负责队列生命周期，业务处理委托给 IngestionService。"""

    def __init__(self, queue: DocumentJobQueue, ingestion: IngestionService, *, max_attempts: int = 5) -> None:
        self._queue = queue
        self._ingestion = ingestion
        self._max_attempts = max_attempts
        self._stop = asyncio.Event()

    async def run(self, *, poll_interval: float = 1.0) -> None:
        """Continuously process queued jobs until the supervisor stops us."""
        self._stop.clear()
        while not self._stop.is_set():
            if not await self.process_one():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()

    async def process_one(self) -> bool:
        job = await self._queue.claim_next(max_attempts=self._max_attempts)
        if job is None:
            return False
        job_id = str(job["job_id"])
        attachment_id = str(job["attachment_id"])
        attempt = int(job.get("attempt", 1))
        try:
            result = await self._ingestion.process(attachment_id)
            await self._queue.mark_succeeded(job_id, result)
        except Exception as exc:
            await self._queue.mark_failed(
                job_id,
                str(exc),
                retry=attempt < self._max_attempts,
            )
        return True
