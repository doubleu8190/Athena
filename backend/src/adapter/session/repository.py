"""会话目录 async SQLAlchemy 适配器。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.project.model import ProjectModel
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from common.date import as_utc
from domain.project import ProjectNotFoundError
from domain.run import RunStatus, SessionHasActiveRunError
from domain.session import (
    Session,
    SessionPage,
    SessionScope,
)


def session_to_domain(row: SessionModel) -> Session:
    """把会话 ORM 行转换为最小领域对象。"""
    return Session(
        id=row.id,
        owner_id=row.owner_id,
        title=row.title,
        project_id=row.project_id,
        last_message_sequence=row.last_message_sequence,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        deleted_time=as_utc(row.deleted_time) if row.deleted_time is not None else None,
    )


_ACTIVE_RUN_STATUSES = (
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_FOR_INPUT.value,
    RunStatus.WAITING_FOR_APPROVAL.value,
    RunStatus.WAITING_FOR_SUBAGENTS.value,
    RunStatus.CANCELLING.value,
)


def _escape_like_pattern(value: str) -> str:
    """对 SQL LIKE 通配符 %、_ 与转义符 \\ 做安全转义。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqlAlchemySessionRepository:
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
        query: str | None = None,
    ) -> SessionPage:
        """分页读取当前 owner 的未删除会话，支持转义后的不区分大小写标题子串搜索。"""
        filters = [
            SessionModel.owner_id == owner_id,
            SessionModel.deleted_time.is_(None),
        ]
        if project_id is not None:
            filters.append(SessionModel.project_id == project_id)
        if unbound:
            filters.append(SessionModel.project_id.is_(None))
        if query is not None:
            escaped = _escape_like_pattern(query)
            filters.append(SessionModel.title.is_not(None))
            filters.append(SessionModel.title.ilike(f"%{escaped}%", escape="\\"))

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
        """在同一事务内锁定会话并更新目录字段；仅在存在活跃 Run 时禁止变更项目归属。"""
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
                    # 在会话行锁内检查活跃 Run：允许已有历史消息的会话自由改绑/解绑项目，
                    # 但执行中或等待中的 Run 未结束前禁止变更项目归属。
                    active_run_id = await database_session.scalar(
                        select(RunModel.id)
                        .where(
                            RunModel.session_id == scope.session_id,
                            RunModel.status.in_(_ACTIVE_RUN_STATUSES),
                        )
                        .limit(1)
                    )
                    if active_run_id is not None:
                        raise SessionHasActiveRunError("session_has_active_run")
                    if project_id is not None:
                        await self._lock_active_project(
                            database_session,
                            scope.owner_id,
                            project_id,
                        )
                    row.project_id = project_id
                if title_changed:
                    row.title = title
                row.updated_at = max(as_utc(row.updated_at), as_utc(updated_at))
                await database_session.flush()
                result = session_to_domain(row)
        return result

    async def set_title_if_empty(
        self,
        owner_id: str,
        session_id: str,
        title: str,
    ) -> bool:
        """条件更新会话标题：仅当未软删除且 title IS NULL 时写入，绝不覆盖用户手动标题。"""
        async with self._session_factory() as database_session:
            async with database_session.begin():
                updated_id = (
                    await database_session.execute(
                        update(SessionModel)
                        .where(
                            SessionModel.id == session_id,
                            SessionModel.owner_id == owner_id,
                            SessionModel.deleted_time.is_(None),
                            SessionModel.title.is_(None),
                        )
                        .values(title=title)
                        .returning(SessionModel.id)
                    )
                ).scalar_one_or_none()
                return updated_id is not None

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
                    active_run_id = await database_session.scalar(
                        select(RunModel.id).where(
                            RunModel.session_id == session_id,
                            RunModel.status.in_(_ACTIVE_RUN_STATUSES),
                        )
                    )
                    if active_run_id is not None:
                        raise SessionHasActiveRunError("session_has_active_run")
                    row.deleted_time = deleted_at
                    row.updated_at = max(as_utc(row.updated_at), as_utc(deleted_at))
                    await database_session.flush()
        return True


__all__ = ["SqlAlchemySessionRepository"]
