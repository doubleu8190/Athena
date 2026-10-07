"""新 Session PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.sessions import Session

from ..models import SessionModel
from .converters import session_domain_to_model, session_model_to_domain


class PostgresSessionRepository:
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

    async def get(self, session_id: str) -> Session | None:
        """查询未删除会话。"""
        async with self._session_factory() as db:
            result = await db.execute(
                select(SessionModel).where(
                    SessionModel.id == session_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            row = result.scalar_one_or_none()
        return None if row is None else session_model_to_domain(row)

    async def list_all(self) -> list[Session]:
        """按更新时间倒序查询未删除会话。"""
        async with self._session_factory() as db:
            result = await db.execute(
                select(SessionModel)
                .where(SessionModel.deleted_time.is_(None))
                .order_by(SessionModel.updated_at.desc())
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
                        title=session.title,
                        status=session.status.value,
                        run_id=session.run_id,
                        updated_at=session.updated_at.isoformat(),
                        compression_summary=session.compression_summary,
                        last_compressed_message_id=session.last_compressed_message_id,
                        last_summarized_message_id=session.last_summarized_message_id,
                    )
                )
        return session

    async def delete(self, session_id: str) -> bool:
        """软删除会话并返回是否存在目标。"""
        deleted_at = datetime.now(timezone.utc).isoformat()
        async with self._session_factory() as db:
            async with db.begin():
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
