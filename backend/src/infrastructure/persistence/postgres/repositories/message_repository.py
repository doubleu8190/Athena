"""新 Message PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.sessions import AttachmentRef, Message, MessageRepository

from ..models import MessageModel, SessionModel
from ..mappers.converters import message_domain_to_model, message_model_to_domain

AttachmentReader = Callable[[list[str]], Awaitable[dict[str, list[AttachmentRef]]]]


class PostgresMessageRepository(MessageRepository):
    """通过注入的异步会话工厂实现 MessageRepository。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        attachment_reader: AttachmentReader | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._attachment_reader = attachment_reader

    async def save(self, message: Message) -> str:
        """保存消息并返回消息 ID。"""
        async with self._session_factory() as db:
            async with db.begin():
                session_row = None
                if message.role.value in {"user", "assistant"}:
                    session_row = await db.scalar(
                        select(SessionModel)
                        .where(
                            SessionModel.id == message.session_id,
                            SessionModel.deleted_time.is_(None),
                        )
                        .with_for_update()
                    )
                    if session_row is None:
                        raise LookupError("session_not_found")
                db.add(message_domain_to_model(message))
                if session_row is not None:
                    timestamp = message.timestamp.isoformat()
                    session_row.updated_at = timestamp
                    if (
                        session_row.last_activity_at is None
                        or timestamp > session_row.last_activity_at
                    ):
                        session_row.last_activity_at = timestamp
        return message.id

    async def get(self, message_id: str) -> Message | None:
        """查询未删除消息。"""
        async with self._session_factory() as db:
            result = await db.execute(
                select(MessageModel).where(
                    MessageModel.id == message_id,
                    MessageModel.deleted_time.is_(None),
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return (await self._convert_rows([row]))[0]

    async def list_by_session(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[Message]:
        """按时间顺序查询会话消息。"""
        async with self._session_factory() as db:
            statement = (
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.deleted_time.is_(None),
                )
                .order_by(MessageModel.timestamp.asc(), MessageModel.id.asc())
            )
            if limit is not None:
                statement = statement.limit(limit)
            result = await db.execute(statement)
            rows = result.scalars().all()
        return await self._convert_rows(rows)

    async def list_after(self, session_id: str, after_id: str) -> list[Message]:
        """查询指定消息之后的消息。"""
        async with self._session_factory() as db:
            result = await db.execute(
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.id > after_id,
                    MessageModel.deleted_time.is_(None),
                )
                .order_by(MessageModel.id.asc())
            )
            rows = result.scalars().all()
        return await self._convert_rows(rows)

    async def list_recent_conversation(
        self, session_id: str, *, limit: int
    ) -> list[Message]:
        """先按角色筛选，再取最近 N 条并恢复成时间升序。"""
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._session_factory() as db:
            result = await db.execute(
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.deleted_time.is_(None),
                    MessageModel.role.in_(("user", "assistant")),
                )
                .order_by(MessageModel.timestamp.desc(), MessageModel.id.desc())
                .limit(limit)
            )
            rows = list(result.scalars().all())
        rows.reverse()
        return await self._convert_rows(rows)

    async def _convert_rows(self, rows) -> list[Message]:
        """转换消息行，并在注入时加载附件引用。"""
        references: dict[str, list[AttachmentRef]] = {}
        if self._attachment_reader is not None:
            references = await self._attachment_reader([row.id for row in rows])
        return [
            message_model_to_domain(row, attachments=references.get(row.id, []))
            for row in rows
        ]


__all__ = ["PostgresMessageRepository"]
