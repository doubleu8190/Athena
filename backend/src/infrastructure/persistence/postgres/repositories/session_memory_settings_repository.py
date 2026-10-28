"""PostgreSQL 会话级记忆读取设置仓储。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.memory import SessionMemorySettings, SessionMemorySettingsPort
from ..models import SessionMemorySettingsModel, SessionModel


class PostgresSessionMemorySettingsRepository(SessionMemorySettingsPort):
    """使用独立表保存会话记忆读取偏好。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
    ) -> None:
        """保存异步数据库会话工厂。"""
        self._session_factory = session_factory

    async def get(self, session_id: str) -> SessionMemorySettings:
        """读取设置；首次读取默认允许使用记忆。"""
        async with self._session_factory() as session:
            row = await session.get(SessionMemorySettingsModel, session_id)
            project_id = await session.scalar(
                select(SessionModel.project_id).where(SessionModel.id == session_id)
            )
        if row is None:
            return SessionMemorySettings(
                session_id=session_id,
                project_id=project_id,
            )
        return SessionMemorySettings(
            session_id=row.session_id,
            use_memories=row.use_memories,
            project_id=project_id,
        )

    async def save(
        self, settings: SessionMemorySettings
    ) -> SessionMemorySettings:
        """原子写入读取设置，避免并发 PUT 覆盖其他设置。"""
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
                await session.execute(
                    pg_insert(SessionMemorySettingsModel)
                    .values(
                        session_id=settings.session_id,
                        use_memories=settings.use_memories,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["session_id"],
                        set_={
                            "use_memories": settings.use_memories,
                            "updated_at": now,
                        },
                    )
                )
        project_id = settings.project_id
        if project_id is None:
            async with self._session_factory() as session:
                project_id = await session.scalar(
                    select(SessionModel.project_id).where(
                        SessionModel.id == settings.session_id
                    )
                )
        return SessionMemorySettings(
            session_id=settings.session_id,
            use_memories=settings.use_memories,
            project_id=project_id,
        )


__all__ = ["PostgresSessionMemorySettingsRepository"]
