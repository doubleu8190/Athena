"""Durable queue for completed-turn memory processing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult

from athena.infrastructure.postgre.engine import (
    get_session,
)


class MemoryJobRepository:
    """SQLite-backed, at-least-once queue keyed by ``turn_id``."""

    async def enqueue_job(self, payload: dict[str, Any]) -> bool:
        now = datetime.now().isoformat()
        async with get_session() as session:
            async with session.begin():
                result = await session.execute(
                    text(
                        """INSERT INTO memory_processing_jobs
                        (job_id, turn_id, session_id, status, attempt,
                         available_at, payload_json, created_at, updated_at)
                        VALUES (:job_id, :turn_id, :session_id, 'queued', 0,
                                :available_at, :payload_json, :created_at, :updated_at)
                        ON CONFLICT (turn_id) DO NOTHING"""
                    ),
                    {
                        "job_id": payload["turn_id"],
                        "turn_id": payload["turn_id"],
                        "session_id": payload["session_id"],
                        "available_at": now,
                        "payload_json": json.dumps(payload, ensure_ascii=False),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                return cast(CursorResult[Any], result).rowcount > 0

    async def claim_next_job(self, *, max_attempts: int = 5) -> dict[str, Any] | None:
        now = datetime.now().isoformat()
        async with get_session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text("""SELECT job_id, payload_json, attempt
                            FROM memory_processing_jobs
                            WHERE status IN ('queued', 'retry')
                              AND available_at <= :now
                              AND attempt < :max_attempts
                            ORDER BY available_at, created_at
                            LIMIT 1"""),
                        {"now": now, "max_attempts": max_attempts},
                    )
                ).first()
                if row is None:
                    return None
                updated = cast(
                    CursorResult[Any],
                    await session.execute(
                        text("""UPDATE memory_processing_jobs
                            SET status = 'running', attempt = attempt + 1,
                                updated_at = :now
                            WHERE job_id = :job_id
                              AND status IN ('queued', 'retry')"""),
                        {"job_id": row.job_id, "now": now},
                    ),
                )
                if updated.rowcount != 1:
                    return None
                payload = json.loads(row.payload_json)
                payload["attempt"] = int(row.attempt) + 1
                return payload

    async def recover_interrupted_jobs(self) -> int:
        """Return interrupted and previously misconfigured jobs to the retry queue."""
        now = datetime.now().isoformat()
        async with get_session() as session:
            async with session.begin():
                result = cast(
                    CursorResult[Any],
                    await session.execute(
                        text("""UPDATE memory_processing_jobs
                            SET status = 'retry',
                                attempt = CASE WHEN status = 'failed' THEN 0 ELSE attempt END,
                                available_at = :now,
                                error_json = CASE
                                    WHEN status = 'running' THEN 'process_interrupted'
                                    ELSE error_json
                                END,
                                updated_at = :now
                            WHERE status = 'running'
                               OR (
                                   status = 'failed'
                                   AND error_json = 'memory job worker workflow is not configured'
                               )"""),
                        {"now": now},
                    ),
                )
                return result.rowcount

    async def mark_job_succeeded(self, job_id: str) -> None:
        await self._mark(job_id, "succeeded", None, None)

    async def mark_job_failed(self, job_id: str, error: str, *, retry: bool) -> None:
        available = (
            (datetime.now() + timedelta(seconds=30)).isoformat() if retry else None
        )
        await self._mark(job_id, "retry" if retry else "failed", error, available)

    async def _mark(
        self, job_id: str, status: str, error: str | None, available: str | None
    ) -> None:
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    text("""UPDATE memory_processing_jobs
                        SET status = :status, error_json = :error_json,
                            available_at = COALESCE(:available_at, available_at),
                            updated_at = :updated_at
                        WHERE job_id = :job_id"""),
                    {
                        "job_id": job_id,
                        "status": status,
                        "error_json": error,
                        "available_at": available,
                        "updated_at": datetime.now().isoformat(),
                    },
                )
