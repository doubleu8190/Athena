"""PostgreSQL lock/check helpers shared by memory scope writers."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.memory.suppression import (
    PROJECT_SUPPRESSION_FINGERPRINT,
    PROJECT_TURN_SUPPRESSION_FINGERPRINT,
    source_suppression_lock_id,
)

from ..models import (
    ProjectModel,
    MemorySourceSuppressionModel,
    SessionModel,
)


async def project_scope_is_writable(
    session: AsyncSession, project_id: str | None
) -> bool:
    """Serialize with project deletion and reject a deleted project scope."""
    if project_id is None:
        return True
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                source_suppression_lock_id(
                    project_id, PROJECT_SUPPRESSION_FINGERPRINT
                )
            )
        )
    )
    exists = await session.scalar(
        select(ProjectModel.id).where(
            ProjectModel.id == project_id,
            ProjectModel.deleted_time.is_(None),
        )
    )
    suppressed = await session.scalar(
        select(MemorySourceSuppressionModel.source_id)
        .where(
            MemorySourceSuppressionModel.source_type == "project",
            MemorySourceSuppressionModel.source_id == project_id,
            MemorySourceSuppressionModel.item_fingerprint
            == PROJECT_SUPPRESSION_FINGERPRINT,
        )
        .limit(1)
    )
    return exists is not None and suppressed is None


async def project_turn_is_writable(
    session: AsyncSession, turn_id: str
) -> bool:
    """Serialize with project forgetting and reject a previously forgotten Run."""
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                source_suppression_lock_id(
                    turn_id, PROJECT_TURN_SUPPRESSION_FINGERPRINT
                )
            )
        )
    )
    suppressed = await session.scalar(
        select(MemorySourceSuppressionModel.source_id)
        .where(
            MemorySourceSuppressionModel.source_type == "project_turn",
            MemorySourceSuppressionModel.source_id == turn_id,
            MemorySourceSuppressionModel.item_fingerprint
            == PROJECT_TURN_SUPPRESSION_FINGERPRINT,
        )
        .limit(1)
    )
    return suppressed is None


async def record_project_turn_suppression(
    session: AsyncSession, turn_id: str, *, now: datetime
) -> None:
    """Persist a minimal marker for a Run that raced with project deletion."""
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                source_suppression_lock_id(
                    turn_id, PROJECT_TURN_SUPPRESSION_FINGERPRINT
                )
            )
        )
    )
    await session.execute(
        pg_insert(MemorySourceSuppressionModel)
        .values(
            source_type="project_turn",
            source_id=turn_id,
            item_fingerprint=PROJECT_TURN_SUPPRESSION_FINGERPRINT,
            created_at=now,
        )
        .on_conflict_do_nothing()
    )


async def ensure_writable_run_scope(
    session: AsyncSession,
    *,
    session_id: str,
    turn_id: str,
    now: datetime,
) -> bool:
    """Resolve the immutable session project and enforce all forget markers.

    The project advisory lock is always acquired before the turn lock, matching
    the project-forget transaction's lock order.
    """
    project_id = await session.scalar(
        select(SessionModel.project_id).where(SessionModel.id == session_id)
    )
    if project_id is None:
        await record_project_turn_suppression(session, turn_id, now=now)
        return False
    if not await project_scope_is_writable(session, project_id):
        await record_project_turn_suppression(session, turn_id, now=now)
        return False
    if not await project_turn_is_writable(session, turn_id):
        return False
    return True


__all__ = [
    "ensure_writable_run_scope",
    "project_scope_is_writable",
    "project_turn_is_writable",
    "record_project_turn_suppression",
]
