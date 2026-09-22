"""会话 SQLite 仓库。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from athena.infrastructure.postgre.engine import get_session
from athena.infrastructure.postgre.models import (
    MessageModel,
    SessionModel,
    ToolCallModel,
)
from athena.models import Session, SessionStatus

from .model_converters import _row_to_session
from .repository_utils import _SENTINEL, _now_iso


class SessionRepository:
    """会话表 CRUD 操作。"""

    async def create(self, session_id: str, title: str = "New Session") -> Session:
        """创建新会话。"""
        from datetime import datetime

        now = datetime.now()
        async with get_session() as session:
            async with session.begin():
                session.add(
                    SessionModel(
                        id=session_id,
                        title=title,
                        status=SessionStatus.IDLE.value,
                        created_at=now.isoformat(),
                        updated_at=now.isoformat(),
                    )
                )
            return Session(
                id=session_id,
                title=title,
                status=SessionStatus.IDLE,
                created_at=now,
                updated_at=now,
            )

    async def get(
        self, session_id: str, include_deleted: bool = False
    ) -> Session | None:
        """获取单个会话。"""
        async with get_session() as session:
            stmt = select(SessionModel).where(SessionModel.id == session_id)
            if not include_deleted:
                stmt = stmt.where(SessionModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return None if row is None else _row_to_session(row)

    async def list_all(self, include_deleted: bool = False) -> list[Session]:
        """列出所有会话。"""
        async with get_session() as session:
            stmt = select(SessionModel).order_by(SessionModel.updated_at.desc())
            if not include_deleted:
                stmt = stmt.where(SessionModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            return [_row_to_session(row) for row in result.scalars().all()]

    async def update(
        self,
        session_id: str,
        *,
        status: str | None = None,
        run_id: str | None = None,
        title: str | None = None,
        compression_summary: str | None = _SENTINEL,
        last_compressed_message_id: str | None = _SENTINEL,
        last_summarized_message_id: str | None = _SENTINEL,
    ) -> None:
        """更新会话字段。None 表示清空，哨兵值表示不更新。"""
        values: dict[str, Any] = {"updated_at": _now_iso()}
        if status is not None:
            values["status"] = status
        if run_id is not None:
            values["run_id"] = run_id
        if title is not None:
            values["title"] = title
        if compression_summary is not _SENTINEL:
            values["compression_summary"] = compression_summary
        if last_compressed_message_id is not _SENTINEL:
            values["last_compressed_message_id"] = last_compressed_message_id
        if last_summarized_message_id is not _SENTINEL:
            values["last_summarized_message_id"] = last_summarized_message_id

        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(SessionModel)
                    .where(
                        SessionModel.id == session_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .values(**values)
                )

    async def delete(self, session_id: str) -> None:
        """软删除会话及其所有关联数据。"""
        now = _now_iso()
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(SessionModel)
                    .where(
                        SessionModel.id == session_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .values(deleted_time=now)
                )
                await session.execute(
                    update(MessageModel)
                    .where(
                        MessageModel.session_id == session_id,
                        MessageModel.deleted_time.is_(None),
                    )
                    .values(deleted_time=now)
                )
                await session.execute(
                    update(ToolCallModel)
                    .where(
                        ToolCallModel.session_id == session_id,
                        ToolCallModel.deleted_time.is_(None),
                    )
                    .values(deleted_time=now)
                )
