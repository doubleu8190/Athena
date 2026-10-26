"""运行请求准备和上下文加载用例。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from application.runs.request_contracts import (
    RootAttachment,
    RootKnowledgeBase,
    RootRequestHistoryMessage,
    RootRequestState,
)
from domain.events import ApplicationEvent, EventDurability, EventPort, EventType
from application.sessions import SessionNotFoundError
from domain.files import AttachmentRepository, AttachmentStatus
from domain.knowledge import KnowledgeBaseRepository
from domain.sessions import Message, MessageRepository, MessageRole, SessionRepository
from shared import get_logger

logger = get_logger(__name__)


class RunRequestPreparationService:
    """准备运行请求并提供 Root Graph 所需的持久化查询。"""

    def __init__(
        self,
        sessions: SessionRepository,
        messages: MessageRepository,
        attachments: AttachmentRepository,
        knowledge_bases: KnowledgeBaseRepository,
        *,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
        events: EventPort | None = None,
    ) -> None:
        self._sessions = sessions
        self._messages = messages
        self._attachments = attachments
        self._knowledge_bases = knowledge_bases
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._events = events

    async def prepare(
        self,
        *,
        request: RootRequestState,
    ) -> RootRequestState:
        """校验附件并幂等持久化当前用户消息。

        参数：
            request: 包含会话、运行、消息和附件标识的运行请求。
        返回值：
            RootRequestState: 更新后的请求和附件引用。
        异常：
            ValueError: 附件不存在、未完成处理或会话标识为空时抛出。
            SessionNotFoundError: 会话不存在时抛出。
        """
        session_id = request.session_id
        if not session_id:
            raise ValueError("session_id is required")
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)

        message_id = request.message_id or self._id_factory()
        attachment_refs: list[RootAttachment] = []
        attachment_values = []

        # 运行只能使用当前会话中已完成处理的附件，并把同一份引用写入消息。
        for attachment_id in request.attachment_ids or ():
            attachment = await self._attachments.get(
                str(attachment_id), session_id=session_id
            )
            if attachment is None:
                raise ValueError(f"attachment_not_found: {attachment_id}")
            if attachment.status != AttachmentStatus.READY:
                raise ValueError(f"attachment_not_ready: {attachment_id}")
            status = attachment.status.value
            attachment_values.append(attachment.to_attachment_reference())
            attachment_refs.append(
                RootAttachment(
                    id=attachment.id,
                    filename=attachment.filename,
                    mime_type=attachment.mime_type,
                    size_bytes=attachment.size_bytes,
                    status=status,
                )
            )
        prepared_request = request.model_copy(
            update={"message_id": message_id, "attachment_refs": attachment_refs}
        )

        user_message = await self._messages.get(message_id)
        if user_message is None:
            user_message = Message(
                id=message_id,
                session_id=session_id,
                role=MessageRole.USER,
                content=request.user_message,
                timestamp=self._clock(),
                run_id=request.run_id or None,
                attachments=tuple(attachment_values),
            )
            await self._messages.save(user_message)
            logger.info(f"用户消息保存成功。", user_message=user_message.content)
        if self._events is not None:
            await self._events.publish(
                ApplicationEvent(
                    event_type=EventType.MESSAGE_PERSISTED,
                    durability=EventDurability.DURABLE,
                    session_id=session_id,
                    run_id=user_message.run_id,
                    message_id=user_message.id,
                    transition_id=f"message:{user_message.id}:persisted",
                    occurred_at=user_message.timestamp,
                    payload={
                        "role": user_message.role.value,
                        "timestamp": user_message.timestamp.isoformat(),
                    },
                )
            )
        return prepared_request

    async def load_history(
        self,
        *,
        session_id: str,
        exclude_message_id: str,
    ) -> list[dict[str, object]]:
        """加载当前消息之前的会话历史。

        参数：
            session_id: 要查询的会话标识。
            exclude_message_id: 当前用户消息标识，避免历史中重复注入。
        返回值：
            list[dict[str, object]]: 可直接提供给模型的消息列表。
        异常：
            不主动抛出业务异常。
        """
        values = await self._messages.list_recent_conversation(session_id, limit=100)
        return [
            {
                "role": message.role.value,
                "content": message.content,
                "message_id": message.id,
                "timestamp": message.timestamp.isoformat(),
            }
            for message in values
            if message.id != exclude_message_id
            and message.role in {MessageRole.USER, MessageRole.ASSISTANT}
        ]

    async def load_attachments(
        self,
        *,
        session_id: str,
        attachment_ids: Sequence[str],
    ) -> list[dict[str, object]]:
        """加载属于当前会话的附件引用。

        参数：
            session_id: 当前会话标识。
            attachment_ids: 请求中的附件标识序列。
        返回值：
            list[dict[str, object]]: 附件的安全元数据列表。
        异常：
            不主动抛出业务异常；不存在的附件会被跳过。
        """
        values: list[dict[str, object]] = []
        for attachment_id in attachment_ids:
            attachment = await self._attachments.get(
                str(attachment_id), session_id=session_id
            )
            if attachment is not None:
                values.append(
                    {
                        "id": attachment.id,
                        "filename": attachment.filename,
                        "mime_type": attachment.mime_type,
                        "size_bytes": attachment.size_bytes,
                        "status": attachment.status.value,
                    }
                )
        return values

    async def load_knowledge_bases(self) -> list[RootKnowledgeBase]:
        """加载 Root 决策使用的知识库元数据。

        返回值：
            list[RootKnowledgeBase]: 知识库统计信息列表。
        异常：
            不主动抛出业务异常。
        """
        return [
            RootKnowledgeBase(
                id=item.id,
                name=item.name,
                description=item.description,
                document_count=item.document_count,
                ready_document_count=item.ready_document_count,
            )
            for item in await self._knowledge_bases.list_knowledge_bases()
        ]


__all__ = ["RunRequestPreparationService"]
