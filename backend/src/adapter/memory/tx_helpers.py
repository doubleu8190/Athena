"""跨模块复用的记忆事务辅助函数（用户设置锁、Session 失效与 Project 禁用）。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.memory.model import (
    MemoryJobModel,
    MemoryScopeStateModel,
    MemorySettingModel,
)
from adapter.run.model import RunModel
from common.date import utc_now
from domain.memory.lifecycle import (
    CleanupScopePayload,
    ExtractionStatus,
    JobKind,
    JobStatus,
)

__all__ = [
    "disable_project_memory_scope",
    "enqueue_cleanup_job",
    "invalidate_session_memory_generation",
    "lock_scope_state",
    "lock_scope_states",
    "lock_user_memory_setting",
]


def _scope_key(project_id: str | None) -> str:
    return "personal" if project_id is None else f"project:{project_id}"


def _json_value(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


async def lock_user_memory_setting(
    session: AsyncSession,
    namespace_id: str,
) -> MemorySettingModel:
    """创建并锁定用户级记忆设置行，作为用户设置与控制写事务的串行点。"""
    if not namespace_id.strip():
        raise ValueError("memory_owner_required")
    await session.execute(
        insert(MemorySettingModel)
        .values(
            namespace_id=namespace_id,
            read_enabled=True,
            auto_extract_enabled=True,
            generation=1,
        )
        .on_conflict_do_nothing(index_elements=["namespace_id"])
    )
    row = await session.scalar(
        select(MemorySettingModel)
        .where(MemorySettingModel.namespace_id == namespace_id)
        .with_for_update()
    )
    if row is None:
        raise RuntimeError("memory_setting_missing")
    return row


async def lock_scope_states(
    session: AsyncSession,
    namespace_id: str,
    scope_keys: tuple[str, ...] | list[str],
) -> dict[str, MemoryScopeStateModel]:
    """按 scope_key 字典序初始化并锁定多个 scope 状态行，避免多 scope 并发死锁。"""
    ordered_keys = tuple(sorted(set(scope_keys)))
    if not ordered_keys:
        return {}
    now = utc_now()
    await session.execute(
        insert(MemoryScopeStateModel)
        .values(
            [
                {
                    "namespace_id": namespace_id,
                    "scope_key": scope_key,
                    "enabled": True,
                    "last_maintained_day": None,
                    "created_at": now,
                    "updated_at": now,
                }
                for scope_key in ordered_keys
            ]
        )
        .on_conflict_do_nothing(index_elements=["namespace_id", "scope_key"])
    )
    rows = (
        await session.scalars(
            select(MemoryScopeStateModel)
            .where(
                MemoryScopeStateModel.namespace_id == namespace_id,
                MemoryScopeStateModel.scope_key.in_(ordered_keys),
            )
            .order_by(
                MemoryScopeStateModel.namespace_id,
                MemoryScopeStateModel.scope_key,
            )
            .with_for_update()
        )
    ).all()
    scopes = {row.scope_key: row for row in rows}
    if len(scopes) != len(ordered_keys):
        raise RuntimeError("memory_scope_state_missing")
    return scopes


async def lock_scope_state(
    session: AsyncSession,
    namespace_id: str,
    scope_key: str,
) -> MemoryScopeStateModel:
    """创建并锁定单个 scope 状态行。"""
    scopes = await lock_scope_states(session, namespace_id, (scope_key,))
    return scopes[scope_key]


async def enqueue_cleanup_job(
    session: AsyncSession,
    *,
    namespace_id: str,
    scope_key: str,
    now: datetime,
) -> None:
    """幂等安排一个 scope 的内容清理任务。"""
    dedupe_key = f"cleanup_scope:{namespace_id}:{scope_key}"
    await session.execute(
        insert(MemoryJobModel)
        .values(
            id=f"mjob_{hashlib.sha256(dedupe_key.encode()).hexdigest()[:24]}",
            dedupe_key=dedupe_key,
            kind=JobKind.CLEANUP_SCOPE.value,
            status=JobStatus.QUEUED.value,
            payload=_json_value(
                CleanupScopePayload(
                    namespace_id=namespace_id,
                    scope_key=scope_key,
                )
            ),
            result=None,
            namespace_id=namespace_id,
            scope_key=scope_key,
            attempt_count=0,
            schema_retry_count=0,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )


async def invalidate_session_memory_generation(
    session: AsyncSession,
    *,
    owner_id: str,
    session_id: str,
    invalidated_at: datetime,
) -> int:
    """在 Session 来源失效事务内推进 generation 并跳过该 Session 相关 Run。"""
    setting = await lock_user_memory_setting(session, owner_id)
    setting.generation += 1
    rows = (
        await session.scalars(
            select(RunModel)
            .where(
                RunModel.owner_id == owner_id,
                RunModel.session_id == session_id,
                or_(
                    RunModel.memory_extraction_status.is_(None),
                    RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
                    RunModel.finished_at.is_(None),
                ),
            )
            .order_by(RunModel.id)
            .with_for_update()
        )
    ).all()
    for row in rows:
        if row.memory_extraction_status in {
            None,
            ExtractionStatus.PENDING.value,
        }:
            row.memory_extraction_status = ExtractionStatus.SKIPPED.value
            row.memory_extraction_safe_error_code = "memory_session_deleted"
        if row.finished_at is not None:
            row.memory_extracted_at = invalidated_at
        row.updated_at = invalidated_at
    return setting.generation


async def disable_project_memory_scope(
    session: AsyncSession,
    *,
    owner_id: str,
    project_id: str,
    disabled_at: datetime,
) -> int:
    """在项目删除事务内禁用项目 scope 并安排幂等清理。"""
    scope_key = _scope_key(project_id)
    setting = await lock_user_memory_setting(session, owner_id)
    state = await lock_scope_state(session, owner_id, scope_key)
    if state.enabled:
        state.enabled = False
        state.updated_at = disabled_at
        setting.generation += 1
    rows = (
        await session.scalars(
            select(RunModel)
            .where(
                RunModel.owner_id == owner_id,
                RunModel.memory_project_id == project_id,
                or_(
                    RunModel.memory_extraction_status.is_(None),
                    RunModel.memory_extraction_status == ExtractionStatus.PENDING.value,
                    RunModel.finished_at.is_(None),
                ),
            )
            .order_by(RunModel.id)
            .with_for_update()
        )
    ).all()
    for run in rows:
        if run.memory_extraction_status in {
            None,
            ExtractionStatus.PENDING.value,
        }:
            run.memory_extraction_status = ExtractionStatus.SKIPPED.value
            run.memory_extraction_safe_error_code = "memory_scope_disabled"
        if run.finished_at is not None:
            run.memory_extracted_at = disabled_at
        run.updated_at = disabled_at
    await enqueue_cleanup_job(
        session,
        namespace_id=owner_id,
        scope_key=scope_key,
        now=disabled_at,
    )
    return setting.generation
