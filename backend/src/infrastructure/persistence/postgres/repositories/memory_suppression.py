"""PostgreSQL lock/check helpers shared by memory scope writers."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.memory.suppression import (
    PROJECT_SUPPRESSION_FINGERPRINT,
    PROJECT_TURN_SUPPRESSION_FINGERPRINT,
    source_suppression_lock_id,
)

from ..models import (
    MemoryJobScopeSnapshotModel,
    MemoryProjectSpaceModel,
    MemorySourceSuppressionModel,
    SessionMemoryProjectBindingModel,
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
        select(MemoryProjectSpaceModel.id).where(
            MemoryProjectSpaceModel.id == project_id
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
    session: AsyncSession, turn_id: str, *, now: str
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
    now: str,
) -> tuple[bool, str | None]:
    """Freeze a Run's first project binding and enforce all forget markers.

    Callers serialize binding updates with a row lock on the parent session.
    The project advisory lock is always acquired before the turn lock, matching
    the project-forget transaction's lock order.
    """
    await session.scalar(
        select(SessionModel.id)
        .where(SessionModel.id == session_id)
        .with_for_update()
    )
    snapshot = await session.get(MemoryJobScopeSnapshotModel, turn_id)
    if snapshot is not None:
        project_id = snapshot.project_id
    else:
        binding = await session.get(SessionMemoryProjectBindingModel, session_id)
        project_id = None if binding is None else binding.project_id

    if not await project_scope_is_writable(session, project_id):
        await record_project_turn_suppression(session, turn_id, now=now)
        return False, None
    if not await project_turn_is_writable(session, turn_id):
        return False, None

    if snapshot is None:
        await session.execute(
            pg_insert(MemoryJobScopeSnapshotModel)
            .values(
                turn_id=turn_id,
                session_id=session_id,
                project_id=project_id,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["turn_id"])
        )
        snapshot = await session.get(
            MemoryJobScopeSnapshotModel, turn_id, populate_existing=True
        )
        if snapshot is not None:
            project_id = snapshot.project_id
    return True, project_id


__all__ = [
    "ensure_writable_run_scope",
    "project_scope_is_writable",
    "project_turn_is_writable",
    "record_project_turn_suppression",
]
