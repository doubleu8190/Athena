"""PostgreSQL 会话级记忆开关仓储。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.memory import SessionMemorySettings, SessionMemorySettingsPort
from domain.memory.suppression import (
    PROJECT_SUPPRESSION_FINGERPRINT,
    source_suppression_lock_id,
)

from ..models import (
    MemoryProjectSpaceModel,
    SessionMemoryProjectBindingModel,
    SessionMemorySettingsModel,
    SessionModel,
    MemorySourceSuppressionModel,
)


class PostgresSessionMemorySettingsRepository(SessionMemorySettingsPort):
    """使用独立新表保存会话记忆读取和生成偏好。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
    ) -> None:
        """保存异步数据库会话工厂。"""
        self._session_factory = session_factory

    async def get(self, session_id: str) -> SessionMemorySettings:
        """读取设置；首次读取沿用现有行为并默认开启两个开关。"""
        async with self._session_factory() as session:
            row = await session.get(SessionMemorySettingsModel, session_id)
            binding = await session.get(SessionMemoryProjectBindingModel, session_id)
        if row is None:
            return SessionMemorySettings(
                session_id=session_id,
                project_id=None if binding is None else binding.project_id,
            )
        return SessionMemorySettings(
            session_id=row.session_id,
            use_memories=row.use_memories,
            generate_memories=row.generate_memories,
            project_id=None if binding is None else binding.project_id,
        )

    async def save(
        self, settings: SessionMemorySettings
    ) -> SessionMemorySettings:
        """原子写入两项设置，避免并发 PUT 产生半更新。"""
        now = datetime.now(timezone.utc).isoformat()
        async with self._session_factory() as session:
            async with session.begin():
                active_session_id = await session.scalar(
                    select(SessionModel.id)
                    .where(
                        SessionModel.id == settings.session_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if active_session_id is None:
                    raise LookupError("session_not_found")
                if settings.project_id is not None:
                    await session.execute(
                        select(
                            func.pg_advisory_xact_lock(
                                source_suppression_lock_id(
                                    settings.project_id,
                                    PROJECT_SUPPRESSION_FINGERPRINT,
                                )
                            )
                        )
                    )
                    suppressed = await session.scalar(
                        select(MemorySourceSuppressionModel.source_id)
                        .where(
                            MemorySourceSuppressionModel.source_type == "project",
                            MemorySourceSuppressionModel.source_id
                            == settings.project_id,
                            MemorySourceSuppressionModel.item_fingerprint
                            == PROJECT_SUPPRESSION_FINGERPRINT,
                        )
                        .limit(1)
                    )
                    if suppressed is not None:
                        raise LookupError("memory_project_not_found")
                    project = await session.get(
                        MemoryProjectSpaceModel, settings.project_id
                    )
                    if project is None:
                        raise LookupError("memory_project_not_found")
                await session.execute(
                    pg_insert(SessionMemorySettingsModel)
                    .values(
                        session_id=settings.session_id,
                        use_memories=settings.use_memories,
                        generate_memories=settings.generate_memories,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["session_id"],
                        set_={
                            "use_memories": settings.use_memories,
                            "generate_memories": settings.generate_memories,
                            "updated_at": now,
                        },
                    )
                )
                if settings.project_id is None:
                    await session.execute(
                        delete(SessionMemoryProjectBindingModel).where(
                            SessionMemoryProjectBindingModel.session_id
                            == settings.session_id
                        )
                    )
                else:
                    await session.execute(
                        pg_insert(SessionMemoryProjectBindingModel)
                        .values(
                            session_id=settings.session_id,
                            project_id=settings.project_id,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=["session_id"],
                            set_={"project_id": settings.project_id, "updated_at": now},
                        )
                    )
        return settings


__all__ = ["PostgresSessionMemorySettingsRepository"]
