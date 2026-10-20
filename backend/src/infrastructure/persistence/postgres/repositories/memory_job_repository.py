"""PostgreSQL-backed durable queue for memory extraction jobs."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.memory import MemoryJobPort, MemoryJobQueue

from ..models import MemoryProcessingJobModel


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresMemoryJobRepository(MemoryJobPort, MemoryJobQueue):
    """Persist memory jobs so extraction survives process restarts."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        max_attempts: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_attempts

    async def enqueue(
        self,
        *,
        turn_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str = "",
    ) -> bool:
        if not user_text.strip():
            return False
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(MemoryProcessingJobModel).where(
                    MemoryProcessingJobModel.turn_id == turn_id
                )
            )
            if existing is not None:
                if existing.status == "failed":
                    existing.status = "queued"
                    existing.attempt = 0
                    existing.available_at = _now()
                    existing.updated_at = _now()
                    await session.commit()
                    return True
                return False
            now = _now()
            session.add(
                MemoryProcessingJobModel(
                    job_id=f"memjob_{uuid.uuid4().hex}",
                    turn_id=turn_id,
                    session_id=session_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    status="queued",
                    attempt=0,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return True

    async def recover(self) -> None:
        """Return jobs abandoned by a crashed worker to the queue."""
        async with self._session_factory() as session:
            await session.execute(
                update(MemoryProcessingJobModel)
                .where(MemoryProcessingJobModel.status == "processing")
                .values(status="queued", available_at=_now(), updated_at=_now())
            )
            await session.commit()

    async def claim_next_job(self) -> dict[str, object] | None:
        now = _now()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(MemoryProcessingJobModel)
                    .where(
                        MemoryProcessingJobModel.status == "queued",
                        MemoryProcessingJobModel.available_at <= now,
                    )
                    .order_by(MemoryProcessingJobModel.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = "processing"
            row.attempt += 1
            row.updated_at = now
            await session.commit()
            return {
                "turn_id": row.turn_id,
                "session_id": row.session_id,
                "user_text": row.user_text,
                "assistant_text": row.assistant_text,
                "attempt": row.attempt,
            }

    async def mark_succeeded(self, turn_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(MemoryProcessingJobModel)
                .where(MemoryProcessingJobModel.turn_id == turn_id)
                .values(
                    status="succeeded",
                    error_json=None,
                    updated_at=_now(),
                )
            )
            await session.commit()

    async def mark_failed(self, turn_id: str, error: str, *, retry: bool) -> None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(MemoryProcessingJobModel).where(
                    MemoryProcessingJobModel.turn_id == turn_id
                )
            )
            if row is None:
                return
            row.error_json = json.dumps({"message": error}, ensure_ascii=False)
            row.updated_at = _now()
            if retry and row.attempt < self._max_attempts:
                delay = min(300, 5 * (2 ** max(0, row.attempt - 1)))
                row.status = "queued"
                row.available_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat()
            else:
                row.status = "failed"
            await session.commit()


__all__ = ["PostgresMemoryJobRepository"]
