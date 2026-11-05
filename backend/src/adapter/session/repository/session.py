"""会话目录 async SQLAlchemy 适配器。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.project.model import ProjectModel
from adapter.session.converter.session import session_to_domain
from adapter.session.model.session import SessionModel
from domain.project import ProjectNotFoundError
from domain.session import (
    Session,
    SessionPage,
    SessionProjectLockedError,
    SessionRepository,
    SessionScope,
)


def _monotonic_updated_at(current: datetime, candidate: datetime) -> datetime:
    """取锁内当前时间与候选时间的较大值，兼容 SQLite 丢失时区。"""
    current_utc = current.replace(tzinfo=UTC) if current.tzinfo is None else current
    candidate_utc = candidate.replace(tzinfo=UTC) if candidate.tzinfo is None else candidate
    return max(current_utc, candidate_utc)


class SqlAlchemySessionRepository(SessionRepository):
    """使用 PostgreSQL 兼容 SQL 持久化会话目录。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """保存会话工厂。"""
        self._session_factory = session_factory

    async def create(self, session: Session) -> Session:
        """插入新会话。"""
        async with self._session_factory() as database_session:
            async with database_session.begin():
                if session.project_id is not None:
                    await self._lock_active_project(
                        database_session,
                        session.owner_id,
                        session.project_id,
                    )
                database_session.add(
                    SessionModel(
                        id=session.id,
                        owner_id=session.owner_id,
                        title=session.title,
                        project_id=session.project_id,
                        last_message_sequence=session.last_message_sequence,
                        created_at=session.created_at,
                        updated_at=session.updated_at,
                        deleted_time=session.deleted_time,
                    )
                )
        return session

    async def get_visible(self, scope: SessionScope) -> Session | None:
        """读取未删除且属于 owner 的会话。"""
        async with self._session_factory() as database_session:
            row = await database_session.scalar(
                select(SessionModel).where(
                    SessionModel.id == scope.session_id,
                    SessionModel.owner_id == scope.owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
        return None if row is None else session_to_domain(row)

    async def list_active(
        self,
        owner_id: str,
        *,
        project_id: str | None,
        unbound: bool,
        offset: int,
        limit: int,
    ) -> SessionPage:
        """分页读取当前 owner 的未删除会话。"""
        filters = [
            SessionModel.owner_id == owner_id,
            SessionModel.deleted_time.is_(None),
        ]
        if project_id is not None:
            filters.append(SessionModel.project_id == project_id)
        if unbound:
            filters.append(SessionModel.project_id.is_(None))

        async with self._session_factory() as database_session:
            total = int(
                (
                    await database_session.scalar(
                        select(func.count()).select_from(SessionModel).where(*filters)
                    )
                )
                or 0
            )
            rows = (
                await database_session.scalars(
                    select(SessionModel)
                    .where(*filters)
                    .order_by(SessionModel.updated_at.desc(), SessionModel.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return SessionPage(items=tuple(session_to_domain(row) for row in rows), total=total)

    async def update(
        self,
        scope: SessionScope,
        *,
        update_title: bool,
        title: str | None,
        update_project: bool,
        project_id: str | None,
        updated_at: datetime,
    ) -> Session | None:
        """在同一事务内锁定会话并更新目录字段。"""
        async with self._session_factory() as database_session:
            async with database_session.begin():
                row = await database_session.scalar(
                    select(SessionModel)
                    .where(
                        SessionModel.id == scope.session_id,
                        SessionModel.owner_id == scope.owner_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                title_changed = update_title and title != row.title
                project_changed = update_project and project_id != row.project_id
                if not title_changed and not project_changed:
                    return session_to_domain(row)
                if project_changed:
                    if row.last_message_sequence > 0:
                        raise SessionProjectLockedError("session_project_locked")
                    if project_id is not None:
                        await self._lock_active_project(
                            database_session,
                            scope.owner_id,
                            project_id,
                        )
                    row.project_id = project_id
                if title_changed:
                    row.title = title
                row.updated_at = _monotonic_updated_at(row.updated_at, updated_at)
                await database_session.flush()
                result = session_to_domain(row)
        return result

    async def _lock_active_project(
        self,
        database_session: AsyncSession,
        owner_id: str,
        project_id: str,
    ) -> None:
        """在写事务内以共享行锁确认项目可见且未软删除。"""
        locked_id = await database_session.scalar(
            select(ProjectModel.id)
            .where(
                ProjectModel.id == project_id,
                ProjectModel.owner_id == owner_id,
                ProjectModel.deleted_time.is_(None),
            )
            .with_for_update(read=True)
        )
        if locked_id is None:
            raise ProjectNotFoundError("project_not_found")

    async def soft_delete(
        self,
        owner_id: str,
        session_id: str,
        deleted_at: datetime,
    ) -> bool:
        """软删除会话，并保证 owner 内重复删除幂等。"""
        async with self._session_factory() as database_session:
            async with database_session.begin():
                row = await database_session.scalar(
                    select(SessionModel)
                    .where(
                        SessionModel.id == session_id,
                        SessionModel.owner_id == owner_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                if row.deleted_time is None:
                    row.deleted_time = deleted_at
                    row.updated_at = _monotonic_updated_at(row.updated_at, deleted_at)
                    await database_session.flush()
        return True


__all__ = ["SqlAlchemySessionRepository"]
