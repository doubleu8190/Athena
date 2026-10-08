"""运行请求准备和上下文加载用例。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone

from application.sessions import SessionNotFoundError
from domain.files import AttachmentRepository, AttachmentStatus, KnowledgeBaseRepository
from domain.sessions import Message, MessageRepository, MessageRole, SessionRepository


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
    ) -> None:
        self._sessions = sessions
        self._messages = messages
        self._attachments = attachments
        self._knowledge_bases = knowledge_bases
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def prepare(
        self,
        *,
        request: Mapping[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        """校验附件并幂等持久化当前用户消息。

        参数：
            request: 包含会话、运行、消息和附件标识的运行请求。
        返回值：
            dict[str, object]: 更新后的请求和附件引用。
        异常：
            ValueError: 附件不存在、未完成处理或会话标识为空时抛出。
            SessionNotFoundError: 会话不存在时抛出。
        """
        session_id = str(request.get("session_id", ""))
        if not session_id:
            raise ValueError("session_id is required")
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)

        message_id = str(request.get("message_id") or self._id_factory())
        attachment_refs: list[dict[str, object]] = []
        attachment_values = []

        # 运行只能使用当前会话中已完成处理的附件，并把同一份引用写入消息。
        for attachment_id in request.get("attachment_ids", ()) or ():
            attachment = await self._attachments.get(
                str(attachment_id), session_id=session_id
            )
            if attachment is None:
                raise ValueError(f"attachment_not_found: {attachment_id}")
            status = getattr(attachment.status, "value", attachment.status)
            if status != AttachmentStatus.READY.value:
                raise ValueError(f"attachment_not_ready: {attachment_id}")
            attachment_values.append(attachment.to_reference())
            attachment_refs.append(
                {
                    "id": attachment.id,
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "size_bytes": attachment.size_bytes,
                    "status": status,
                }
            )

        if await self._messages.get(message_id) is None:
            await self._messages.save(
                Message(
                    id=message_id,
                    session_id=session_id,
                    role=MessageRole.USER,
                    content=str(request.get("user_message", "")),
                    timestamp=self._clock(),
                    run_id=str(request.get("run_id") or "") or None,
                    attachments=tuple(attachment_values),
                )
            )

        return {
            "request": {
                **dict(request),
                "message_id": message_id,
                "attachment_refs": attachment_refs,
            }
        }

    async def load_history(
        self,
        *,
        session_id: str,
        request: Mapping[str, object],
        **_kwargs: object,
    ) -> list[dict[str, object]]:
        """加载当前消息之前的会话历史。

        参数：
            session_id: 要查询的会话标识。
            request: 当前运行请求，用于排除当前消息。
        返回值：
            list[dict[str, object]]: 可直接提供给模型的消息列表。
        异常：
            不主动抛出业务异常。
        """
        current_message_id = str(request.get("message_id") or "")
        values = await self._messages.list_by_session(session_id, limit=100)
        return [
            {
                "role": message.role.value,
                "content": message.content,
                "message_id": message.id,
                "timestamp": message.timestamp.isoformat(),
            }
            for message in values
            if message.id != current_message_id
        ]

    async def load_attachments(
        self,
        *,
        session_id: str,
        attachment_ids: Sequence[str],
        **_kwargs: object,
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
                        "status": getattr(
                            attachment.status, "value", attachment.status
                        ),
                    }
                )
        return values

    async def load_knowledge_bases(
        self,
        request: Mapping[str, object] | None = None,
        **_kwargs: object,
    ) -> list[dict[str, object]]:
        """加载意图识别使用的知识库元数据。

        参数：
            request: 可选运行请求；当前查询不依赖其内容。
        返回值：
            list[dict[str, object]]: 知识库统计信息列表。
        异常：
            不主动抛出业务异常。
        """
        del request
        return [
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "document_count": item.document_count,
                "ready_document_count": item.ready_document_count,
            }
            for item in await self._knowledge_bases.list_all()
        ]


__all__ = ["RunRequestPreparationService"]
