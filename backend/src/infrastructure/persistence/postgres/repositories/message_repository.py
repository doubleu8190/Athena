"""新 Message PostgreSQL repository。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.sessions import AttachmentRef, Message, MessageRepository

from ..models import MessageModel, SessionModel, SessionTitleJobModel
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

    async def save_user_message_with_title(
        self, message: Message, *, fallback_title: str
    ) -> bool:
        """原子写入用户消息、最近活动和第一条消息的标题任务。"""
        if message.role.value != "user":
            raise ValueError("title_source_message_must_be_user")
        timestamp = message.timestamp.isoformat()
        async with self._session_factory() as db:
            async with db.begin():
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

                existing = await db.scalar(
                    select(MessageModel).where(MessageModel.id == message.id)
                )
                if existing is not None:
                    if existing.session_id != message.session_id:
                        raise ValueError("message_id_conflict")
                    return False

                has_user_message = await db.scalar(
                    select(MessageModel.id)
                    .where(
                        MessageModel.session_id == message.session_id,
                        MessageModel.role == "user",
                        MessageModel.deleted_time.is_(None),
                    )
                    .limit(1)
                )
                db.add(message_domain_to_model(message))
                session_row.updated_at = timestamp
                if (
                    session_row.last_activity_at is None
                    or timestamp > session_row.last_activity_at
                ):
                    session_row.last_activity_at = timestamp

                if has_user_message is None and session_row.title_source == "placeholder":
                    session_row.title = fallback_title[:48] or "新聊天"
                    session_row.title_source = "fallback"
                    session_row.title_revision += 1
                    db.add(
                        SessionTitleJobModel(
                            session_id=message.session_id,
                            source_message_id=message.id,
                            expected_title_revision=session_row.title_revision,
                            status="queued",
                            attempt=0,
                            attachment_names_json=json.dumps(
                                [item.filename for item in message.attachments],
                                ensure_ascii=False,
                            ),
                            available_at=timestamp,
                            created_at=timestamp,
                            updated_at=timestamp,
                        )
                    )
        return has_user_message is None

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
