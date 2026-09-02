"""消息 SQLite 仓库。"""

from __future__ import annotations

from sqlalchemy import select, update

from athena.infrastructure.sqlite.engine import get_session
from athena.infrastructure.sqlite.models import (
    AttachmentModel,
    MessageModel,
    SessionModel,
)
from athena.models import Message

from .converters import _row_to_message
from .repository_utils import _json_dumps, _now_iso


class MessageRepository:
    """消息表 CRUD 操作。"""

    @staticmethod
    def _model(message: Message) -> MessageModel:
        return MessageModel(
            id=message.id,
            session_id=message.session_id,
            role=message.role.value,
            content=message.content,
            tool_calls_json=_json_dumps(message.tool_calls),
            tool_call_id=message.tool_call_id,
            run_id=message.run_id,
            tool_call_record_id=message.tool_call_record_id,
            tool_name=message.tool_name,
            type=message.type,
            timestamp=message.timestamp.isoformat(),
        )

    async def save(self, message: Message) -> str:
        """保存消息并同步刷新会话的 updated_at。"""
        async with get_session() as session:
            async with session.begin():
                session.add(self._model(message))
                await session.execute(
                    update(SessionModel)
                    .where(
                        SessionModel.id == message.session_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .values(updated_at=_now_iso())
                )
            return message.id

    async def get(self, message_id: str) -> Message | None:
        """按 ID 获取一条未删除消息。"""
        async with get_session() as session:
            row = await session.get(MessageModel, message_id)
            if row is None or row.deleted_time is not None:
                return None
            messages = await self._attach_refs([_row_to_message(row)])
            return messages[0] if messages else None

    async def create_message_with_attachments(
        self, message: Message, attachment_ids: list[str]
    ) -> Message:
        """幂等创建用户消息并在同一事务内绑定附件。"""
        ids = list(dict.fromkeys(attachment_ids))
        async with get_session() as session:
            async with session.begin():
                existing = await session.get(MessageModel, message.id)
                if existing is None:
                    session.add(self._model(message))
                elif (
                    existing.session_id != message.session_id
                    or existing.role != message.role.value
                    or existing.content != message.content
                ):
                    raise ValueError("message_id 已关联其他消息")

                if ids:
                    attachments = (
                        (
                            await session.execute(
                                select(AttachmentModel).where(
                                    AttachmentModel.id.in_(ids),
                                    AttachmentModel.session_id == message.session_id,
                                    AttachmentModel.deleted_time.is_(None),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(attachments) != len(ids):
                        raise ValueError("附件不存在或不属于当前会话")
                    for attachment in attachments:
                        if attachment.message_id not in (None, message.id):
                            raise ValueError("附件已关联其他消息")
                        attachment.message_id = message.id
                await session.execute(
                    update(SessionModel)
                    .where(
                        SessionModel.id == message.session_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .values(updated_at=_now_iso())
                )

        from athena.infrastructure.sqlite.file_repository import FileRepository

        refs_by_message = await FileRepository().attachments_for_messages([message.id])
        return message.model_copy(
            update={
                "attachments": [
                    attachment.to_ref()
                    for attachment in refs_by_message.get(message.id, [])
                ]
            }
        )

    async def _attach_refs(self, messages: list[Message]) -> list[Message]:
        """填充轻量级附件引用，不暴露存储键。"""
        if not messages:
            return messages
        from athena.infrastructure.sqlite.file_repository import FileRepository

        refs = await FileRepository().attachments_for_messages(
            [item.id for item in messages]
        )
        for item in messages:
            item.attachments = [
                attachment.to_ref() for attachment in refs.get(item.id, [])
            ]
        return messages

    async def get_by_session(
        self, session_id: str, limit: int | None = None, include_deleted: bool = False
    ) -> list[Message]:
        """获取会话的消息列表。"""
        async with get_session() as session:
            stmt = (
                select(MessageModel)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.timestamp.asc())
            )
            if not include_deleted:
                stmt = stmt.where(MessageModel.deleted_time.is_(None))
            if limit:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return await self._attach_refs(
                [_row_to_message(row) for row in result.scalars().all()]
            )

    async def get_after_message(
        self, session_id: str, after_id: str, include_deleted: bool = False
    ) -> list[Message]:
        """获取指定消息之后的消息列表。"""
        async with get_session() as session:
            stmt = (
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.id > after_id,
                )
                .order_by(MessageModel.id.asc())
            )
            if not include_deleted:
                stmt = stmt.where(MessageModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            return await self._attach_refs(
                [_row_to_message(row) for row in result.scalars().all()]
            )
