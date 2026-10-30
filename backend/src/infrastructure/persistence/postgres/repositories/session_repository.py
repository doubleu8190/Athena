"""新 Session PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.sessions import Session, SessionRepository

from ..models import ProjectModel, SessionModel
from ..mappers.converters import session_domain_to_model, session_model_to_domain
from ..idempotency import execute_idempotent


def _session_to_json(value: Session) -> dict[str, Any]:
    return {
        "id": value.id,
        "project_id": value.project_id,
        "title": value.title,
        "active_run_id": value.active_run_id,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
        "last_activity_at": value.last_activity_at.isoformat() if value.last_activity_at else None,
        "compression_summary": value.compression_summary,
        "last_compressed_message_id": value.last_compressed_message_id,
        "last_summarized_message_id": value.last_summarized_message_id,
    }


def _session_from_json(value: dict[str, Any]) -> Session:
    return Session(
        id=str(value["id"]),
        project_id=str(value["project_id"]) if value.get("project_id") is not None else None,
        title=str(value["title"]),
        active_run_id=str(value["active_run_id"]) if value.get("active_run_id") is not None else None,
        created_at=datetime.fromisoformat(str(value["created_at"])),
        updated_at=datetime.fromisoformat(str(value["updated_at"])),
        last_activity_at=(
            datetime.fromisoformat(str(value["last_activity_at"]))
            if value.get("last_activity_at") is not None
            else None
        ),
        compression_summary=value.get("compression_summary"),
        last_compressed_message_id=value.get("last_compressed_message_id"),
        last_summarized_message_id=value.get("last_summarized_message_id"),
    )


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
        self,
        session: Session,
        *,
        request_id: str,
        parameters: dict[str, object],
    ) -> tuple[Session, bool]:
        """Create a session and its stable response under a shared receipt."""
        if session.project_id is None:
            raise ValueError("session_project_required")
        async with self._session_factory() as db:
            async with db.begin():
                async def create() -> Session:
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
                    db.add(session_domain_to_model(session))
                    await db.flush()
                    return session

                return await execute_idempotent(
                    db,
                    scope="sessions.create",
                    request_id=request_id,
                    parameters=parameters,
                    action=create,
                    encode=_session_to_json,
                    decode=_session_from_json,
                )

    async def get(self, session_id: str) -> Session | None:
        """查询未删除会话。"""
        async with self._session_factory() as db:
            row = await db.scalar(
                select(SessionModel).where(
                    SessionModel.id == session_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
        return None if row is None else session_model_to_domain(row)

    async def list_sessions(
        self, *, project_id: str | None = None
    ) -> list[Session]:
        """按项目及最近聊天活动列出未删除会话。"""
        async with self._session_factory() as db:
            statement = select(SessionModel).where(SessionModel.deleted_time.is_(None))
            if project_id is not None:
                statement = statement.where(SessionModel.project_id == project_id)
            result = await db.execute(
                statement.order_by(
                    SessionModel.last_activity_at.desc().nullslast(),
                    SessionModel.id.desc(),
                )
            )
            rows = result.scalars().all()
        return [session_model_to_domain(row) for row in rows]

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
                        updated_at=session.updated_at,
                        compression_summary=session.compression_summary,
                        last_compressed_message_id=session.last_compressed_message_id,
                        last_summarized_message_id=session.last_summarized_message_id,
                    )
                )
        return session

    async def rename(self, session: Session) -> Session:
        """只更新会话标题，避免旧快照覆盖并发的 Run 状态。"""
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
                row.updated_at = session.updated_at
                await db.flush()
                return session_model_to_domain(row)

    async def delete(self, session_id: str) -> bool:
        """软删除会话并返回是否存在目标。"""
        deleted_at = datetime.now(timezone.utc)
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


__all__ = ["PostgresSessionRepository"]
