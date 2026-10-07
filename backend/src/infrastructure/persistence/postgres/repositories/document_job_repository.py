"""PostgreSQL queue for knowledge-document ingestion jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import KnowledgeDocumentJobModel


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresDocumentJobRepository:
    """Implements both the enqueue port and the worker queue protocol.

    Queue state is persisted with the attachment record, so a process restart
    can recover jobs without consulting the old worker implementation.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def enqueue(self, attachment_id: str) -> bool:
        now = _now()
        async with self._session_factory() as session:
            async with session.begin():
                existing = (
                    await session.execute(
                        select(KnowledgeDocumentJobModel).where(
                            KnowledgeDocumentJobModel.attachment_id == attachment_id
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.status in {"failed", "cancelled"}:
                        existing.status = "queued"
                        existing.available_at = now
                        existing.updated_at = now
                        return True
                    return False
                session.add(
                    KnowledgeDocumentJobModel(
                        job_id=uuid4().hex,
                        attachment_id=attachment_id,
                        status="queued",
                        attempt=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
        return True

    async def cancel(self, attachment_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(KnowledgeDocumentJobModel)
                    .where(KnowledgeDocumentJobModel.attachment_id == attachment_id)
                    .values(status="cancelled", updated_at=_now())
                )

    async def claim_next(self, *, max_attempts: int = 5) -> dict[str, object] | None:
        now = _now()
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(KnowledgeDocumentJobModel)
                        .where(
                            KnowledgeDocumentJobModel.status == "queued",
                            KnowledgeDocumentJobModel.attempt < max_attempts,
                            KnowledgeDocumentJobModel.available_at <= now,
                        )
                        .order_by(KnowledgeDocumentJobModel.available_at)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                row.status = "running"
                row.attempt += 1
                row.updated_at = now
                return {
                    "job_id": row.job_id,
                    "attachment_id": row.attachment_id,
                    "attempt": row.attempt,
                }

    async def mark_succeeded(self, job_id: str, result: Mapping[str, object]) -> None:
        import json

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(KnowledgeDocumentJobModel)
                    .where(KnowledgeDocumentJobModel.job_id == job_id)
                    .values(
                        status="succeeded",
                        result_json=json.dumps(dict(result), default=str),
                        error_json=None,
                        updated_at=_now(),
                    )
                )

    async def mark_failed(self, job_id: str, error: str, *, retry: bool) -> None:
        import json

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(KnowledgeDocumentJobModel)
                    .where(KnowledgeDocumentJobModel.job_id == job_id)
                    .values(
                        status="queued" if retry else "failed",
                        error_json=json.dumps({"message": error}),
                        updated_at=_now(),
                    )
                )


__all__ = ["PostgresDocumentJobRepository"]
