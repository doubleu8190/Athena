"""知识文档任务 Worker。"""

from __future__ import annotations

import asyncio
from domain.leases import LeaseCenterPort, LeaseGrant, LeaseLostError

from application.files.ingestion_service import IngestionService
from domain.files import DocumentJobQueue
from workers.lease_guard import LeaseGuard


class KnowledgeDocumentWorker:
    """Worker 本身只负责队列生命周期，业务处理委托给 IngestionService。"""

    def __init__(
        self,
        queue: DocumentJobQueue,
        ingestion: IngestionService,
        *,
        max_attempts: int = 5,
        lease_seconds: float = 60.0,
        lease_center: LeaseCenterPort | None = None,
    ) -> None:
        self._queue = queue
        self._ingestion = ingestion
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._leases = lease_center or getattr(ingestion, "_leases")
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
        job = await self._queue.claim_next(
            max_attempts=self._max_attempts,
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return False
        job_id = str(job["job_id"])
        attachment_id = str(job["attachment_id"])
        attempt = int(job.get("attempt", 1))
        lease = job.get("lease")
        if not isinstance(lease, LeaseGrant):
            raise RuntimeError("document_ingestion_lease_missing")
        try:
            async def execute_and_publish() -> None:
                try:
                    result = await self._ingestion.process_attachment(
                        attachment_id, lease=lease
                    )
                except LeaseLostError:
                    raise
                except Exception as exc:
                    await self._queue.mark_failed(
                        job_id,
                        str(exc),
                        retry=attempt < self._max_attempts,
                        lease=lease,
                    )
                    return
                await self._queue.mark_succeeded(job_id, result, lease=lease)

            await LeaseGuard(
                self._leases,
                [lease],
                lease_seconds=self._lease_seconds,
            ).run(execute_and_publish())
        except LeaseLostError:
            await self._leases.acknowledge_stopped(lease)
            return True
        return True
