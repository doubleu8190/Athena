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
from domain.events import EventPort
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
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `sessions`（SessionRepository）：读取会话身份和状态的仓储依赖；约束：无额外运行时限制。
        - `messages`（MessageRepository）：按顺序排列的模型对话消息；约束：无额外运行时限制。
        - `attachments`（AttachmentRepository）：当前请求关联或已校验的附件集合；约束：无额外运行时限制。
        - `knowledge_bases`（KnowledgeBaseRepository）：当前请求已校验的知识库集合；约束：无额外运行时限制。
        - `id_factory`（Callable[[], str]）：生成稳定领域标识的可注入函数；约束：无额外运行时限制。
        - `clock`（Callable[[], datetime] | None）：生成当前时间的可注入函数，可用于控制时间来源；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `events`（EventPort | None）：用于读取、写入或推送的事件存储依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
        - `request`（RootRequestState）：包含会话、运行、消息和附件标识的运行请求；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - RootRequestState：记录当前流程数据和阶段信息的 `RootRequestState` 状态对象。

        异常：
        - ValueError：校验条件 `not session_id` 成立时（异常说明：session_id is required）。
        - SessionNotFoundError：校验条件 `await self._sessions.get(session_id) is None` 成立时。
        - ValueError：校验条件 `not message_id or not request.run_id` 成立时（异常说明：run_source_message_required）。
        - ValueError：校验条件 `attachment is None` 成立时。
        - ValueError：校验条件 `attachment.status != AttachmentStatus.READY` 成立时。
        - ValueError：校验条件 `user_message is None` 成立时（异常说明：source_message_not_found）。
        - ValueError：校验条件 `user_message.session_id != session_id or user_message.run_id != request.run_id or user_message.role != MessageRole.USER or (user_message.content != request.user_message)` 成立时（异常说明：source_message_mismatch）。
        - 未列出的依赖异常按当前实现传播。
        """
        session_id = request.session_id
        if not session_id:
            raise ValueError("session_id is required")
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)

        message_id = request.message_id
        if not message_id or not request.run_id:
            raise ValueError("run_source_message_required")
        attachment_refs: list[RootAttachment] = []
        attachment_values = []

        # Run 只能使用当前会话中已完成处理的附件，并将同一份引用写入消息。
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
        user_message = await self._messages.get(message_id)
        if user_message is None:
            raise ValueError("source_message_not_found")
        if (
            user_message.session_id != session_id
            or user_message.run_id != request.run_id
            or user_message.role != MessageRole.USER
            or user_message.content != request.user_message
        ):
            raise ValueError("source_message_mismatch")
        prepared_request = request.model_copy(
            update={"attachment_refs": attachment_refs}
        )
        return prepared_request

    async def load_history(
        self,
        *,
        session_id: str,
        exclude_message_id: str,
        before_sequence: int | None = None,
    ) -> list[RootRequestHistoryMessage]:
        """加载当前消息之前的会话历史。

        参数：
        - `session_id`（str）：要查询的会话标识；约束：无额外运行时限制。
        - `exclude_message_id`（str）：当前用户消息标识，避免历史中重复注入；约束：无额外运行时限制。
        - `before_sequence`（int | None）：只读取此会话序号之前数据的边界；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - list[RootRequestHistoryMessage]：按时间顺序排列并限制数量的 Root 对话历史。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if before_sequence is None:
            values = await self._messages.list_recent_conversation(session_id, limit=100)
        else:
            values = await self._messages.list_recent_conversation_before_sequence(
                session_id,
                before_sequence=before_sequence,
                limit=100,
            )
        return [
            RootRequestHistoryMessage(
                role=message.role.value,
                content=message.content,
                message_id=message.id,
                timestamp=message.timestamp.isoformat(),
            )
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
        - `session_id`（str）：当前会话标识；约束：无额外运行时限制。
        - `attachment_ids`（Sequence[str]）：请求中的附件标识序列；约束：无额外运行时限制。

        返回：
        - list[dict[str, object]]：通过会话和访问范围校验的附件元数据列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
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

        参数：
        - 无参数。

        返回：
        - list[RootKnowledgeBase]：知识库统计信息列表。 符合条件的有序结果集合；集合成员。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
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
