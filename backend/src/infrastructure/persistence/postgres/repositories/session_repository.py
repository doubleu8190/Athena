"""新 Session PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.sessions import Session, SessionRepository

from ..models import ProjectModel, SessionModel, SessionTitleJobModel
from ..mappers.converters import session_domain_to_model, session_model_to_domain


class PostgresSessionRepository(SessionRepository):
    """通过注入的异步会话工厂实现 SessionRepository。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create(self, session: Session) -> Session:
        """创建会话并返回领域实体。"""
        async with self._session_factory() as db:
            async with db.begin():
                db.add(session_domain_to_model(session))
        return session

    async def create_idempotent(
        self, session: Session, *, creation_request_id: str
    ) -> Session:
        """锁定项目后幂等创建聊天，确保并发重试只生成一条会话。"""
        if session.project_id is None:
            raise ValueError("session_project_required")
        async with self._session_factory() as db:
            async with db.begin():
                project = await db.scalar(
                    select(ProjectModel)
                    .where(
                        ProjectModel.id == session.project_id,
                        ProjectModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if project is None:
                    raise LookupError("project_not_found")
                # creation_request_id 是跨项目全局唯一键；事务级 advisory lock
                # 也串行化不同项目上的并发重试，返回稳定的冲突语义。
                await db.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended(creation_request_id, 0)
                        )
                    )
                )
                existing = await db.scalar(
                    select(SessionModel)
                    .where(SessionModel.creation_request_id == creation_request_id)
                    .with_for_update()
                )
                if existing is not None:
                    if existing.project_id != session.project_id:
                        raise ValueError("idempotency_conflict")
                    if existing.deleted_time is not None:
                        raise ValueError("session_deleted")
                    return session_model_to_domain(existing)
                row = session_domain_to_model(session)
                row.creation_request_id = creation_request_id
                db.add(row)
                await db.flush()
        return session

    async def get(self, session_id: str) -> Session | None:
        """查询未删除会话。"""
        async with self._session_factory() as db:
            result = await db.execute(
                select(
                    SessionModel,
                    exists(
                        select(SessionTitleJobModel.session_id).where(
                            SessionTitleJobModel.session_id == SessionModel.id,
                            SessionTitleJobModel.status.in_(("queued", "running")),
                        )
                    ).label("title_pending"),
                ).where(
                    SessionModel.id == session_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            result_row = result.one_or_none()
        if result_row is None:
            return None
        return _to_session(result_row[0], title_pending=result_row[1])

    async def list_sessions(
        self, *, project_id: str | None = None
    ) -> list[Session]:
        """按项目及最近聊天活动列出未删除会话。"""
        async with self._session_factory() as db:
            statement = select(
                SessionModel,
                exists(
                    select(SessionTitleJobModel.session_id).where(
                        SessionTitleJobModel.session_id == SessionModel.id,
                        SessionTitleJobModel.status.in_(("queued", "running")),
                    )
                ).label("title_pending"),
            ).where(SessionModel.deleted_time.is_(None))
            if project_id is not None:
                statement = statement.where(SessionModel.project_id == project_id)
            result = await db.execute(
                statement.order_by(
                    SessionModel.last_activity_at.desc().nullslast(),
                    SessionModel.id.desc(),
                )
            )
            rows = result.all()
        return [_to_session(row, title_pending=pending) for row, pending in rows]

    async def save(self, session: Session) -> Session:
        """更新会话业务字段并返回传入领域实体。"""
        async with self._session_factory() as db:
            async with db.begin():
                await db.execute(
                    update(SessionModel)
                    .where(
                        SessionModel.id == session.id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .values(
                        status=session.status.value,
                        run_id=session.run_id,
                        updated_at=session.updated_at.isoformat(),
                        compression_summary=session.compression_summary,
                        last_compressed_message_id=session.last_compressed_message_id,
                        last_summarized_message_id=session.last_summarized_message_id,
                    )
                )
        return session

    async def rename(self, session: Session) -> Session:
        """只更新标题来源和版本，避免旧快照覆盖并发的 Run 状态。"""
        async with self._session_factory() as db:
            async with db.begin():
                row = await db.scalar(
                    select(SessionModel)
                    .where(
                        SessionModel.id == session.id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise LookupError("session_not_found")
                row.title = session.title
                row.title_source = "manual"
                row.title_revision += 1
                row.updated_at = session.updated_at.isoformat()
                job = await db.scalar(
                    select(SessionTitleJobModel)
                    .where(
                        SessionTitleJobModel.session_id == session.id,
                        SessionTitleJobModel.status.in_(("queued", "running")),
                    )
                    .with_for_update()
                )
                if job is not None:
                    job.status = "cancelled"
                    job.lease_expires_at = None
                    job.claimed_by = None
                    job.updated_at = session.updated_at.isoformat()
                await db.flush()
                return session_model_to_domain(row)

    async def delete(self, session_id: str) -> bool:
        """软删除会话并返回是否存在目标。"""
        deleted_at = datetime.now(timezone.utc).isoformat()
        async with self._session_factory() as db:
            async with db.begin():
                session_row = await db.scalar(
                    select(SessionModel)
                    .where(
                        SessionModel.id == session_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if session_row is None:
                    return False
                await db.execute(
                    update(SessionTitleJobModel)
                    .where(
                        SessionTitleJobModel.session_id == session_id,
                        SessionTitleJobModel.status.in_(("queued", "running")),
                    )
                    .values(
                        status="cancelled",
                        lease_expires_at=None,
                        claimed_by=None,
                        updated_at=deleted_at,
                    )
                )
                result = await db.execute(
                    update(SessionModel)
                    .where(
                        SessionModel.id == session_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .values(
                        deleted_time=deleted_at,
                    )
                    .returning(SessionModel.id)
                )
        return result.scalar_one_or_none() is not None


def _to_session(row: SessionModel, *, title_pending: bool) -> Session:
    value = session_model_to_domain(row)
    return Session(
        id=value.id,
        title=value.title,
        status=value.status,
        run_id=value.run_id,
        created_at=value.created_at,
        updated_at=value.updated_at,
        compression_summary=value.compression_summary,
        last_compressed_message_id=value.last_compressed_message_id,
        last_summarized_message_id=value.last_summarized_message_id,
        project_id=value.project_id,
        title_source=value.title_source,
        title_revision=value.title_revision,
        last_activity_at=value.last_activity_at,
        title_pending=title_pending,
    )


__all__ = ["PostgresSessionRepository"]
