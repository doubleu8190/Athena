"""Session context service extracted from LangGraphRuntime.

Provides session history loading, attachment validation, message persistence,
and Harness input preparation — the session-context operations of the agent runtime.
"""

from __future__ import annotations

from datetime import datetime
from athena.infrastructure.sqlite.database import Database
from athena.models import Message, MessageRole
from athena.models.file import (
    Attachment,
    AttachmentRef,
    AttachmentStatus,
)
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.ports import EventPublisherPort
from athena.utils.id_generation import generate_time_id
from athena.utils.logging import get_logger

from ..state import AgentState

logger = get_logger(__name__)


class SessionContextService:
    """Session context operations extracted from LangGraphRuntime.

    Handles session history loading, attachment validation, message
    persistence, and Harness input preparation so the graph runtime can focus on
    orchestration.
    """

    def __init__(self, db: Database, event_publisher: EventPublisherPort):
        self._db = db
        self._events = event_publisher

    async def load_and_validate_attachments(
        self,
        session_id: str,
        attachment_ids: list[str],
    ) -> list[Attachment]:
        """加载并校验请求附件是否属于当前会话。

        参数：
            session_id (str): 会话唯一标识。
            attachment_ids (list[str]): 待加载的附件 ID 列表。

        返回值：
            list[Attachment]: 按请求顺序返回属于当前会话的附件。

        异常：
            ValueError: 附件不存在、不属于当前会话，或首次提交时附件处理已失败。
            其他异常: 数据库读取失败时向上传播。
        """
        if not attachment_ids:
            return []
        attachments = await self._db.files.get_attachments(session_id, attachment_ids)
        attachments_by_id = {item.id: item for item in attachments}
        result: list[Attachment] = []
        for file_id in attachment_ids:
            attachment = attachments_by_id.get(file_id)
            if attachment is None:
                raise ValueError("附件不存在或不属于当前会话")
            if attachment.status == AttachmentStatus.FAILED:
                raise ValueError(f"附件 {attachment.filename} 处理失败，不能随消息提交")
            result.append(attachment)
        return result

    async def load_history(self, session_id: str) -> list[Message]:
        """加载会话历史，并在存在压缩摘要时拼接增量消息。

        参数：
            session_id (str): 会话唯一标识。

        返回值：
            list[Message]: 可供 Harness 使用的历史消息列表。

        异常：
            数据库读取失败时向上传播底层异常。
        """
        session = await self._db.sessions.get(session_id)
        if not session:
            raise ValueError(f"会话 {session_id} 不存在")
        if not (session.compression_summary and session.last_compressed_message_id):
            return await self._db.messages.get_by_session(session_id)

        history_after = await self._db.messages.get_after_message(
            session_id, session.last_compressed_message_id
        )
        summary = Message(
            id=generate_time_id(),
            session_id=session_id,
            role=MessageRole.SYSTEM,
            content=f"[对话历史摘要]\n{session.compression_summary}",
            run_id=f"summary:{session_id}",
            message_type="conversation_summary",
            timestamp=datetime.now(),
        )
        logger.info(
            "history_loaded_with_summary",
            session_id=session_id,
            incremental_count=len(history_after),
        )
        return [summary, *history_after]

    async def persist_message_and_attachments(self, state: AgentState):
        """幂等持久化当前用户消息及其附件关系。"""
        message_id = state.get("message_id", "")
        message = Message(
            id=message_id,
            session_id=state.get("session_id", ""),
            role=MessageRole.USER,
            content=state.get("user_message", ""),
            run_id=state.get("run_id", ""),
            timestamp=datetime.now(),
        )
        persisted = await self._db.messages.create_message_with_attachments(
            message, state.get("attachment_ids", [])
        )
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.MESSAGE_PERSISTED,
                durability=EventDurability.DURABLE,
                session_id=state.get("session_id", ""),
                run_id=state.get("run_id", ""),
                message_id=persisted.id,
                transition_id=f"message:{persisted.id}:persisted",
                payload={
                    "attachment_ids": state.get("attachment_ids", []),
                },
            )
        )

    async def prepare_harness_input(
        self,
        session_id: str,
        user_message: str,
        attachment_ids: list[str],
        history: list[Message],
        run_id: str,
        message_id: str,
    ) -> tuple[list[AttachmentRef], list[Message]]:
        """根据会话消息和附件构造 Harness 输入。

        参数：
            session_id (str): 会话唯一标识。
            user_message (str): 当前用户消息内容。
            attachment_ids (list[str]): 当前消息关联的附件 ID 列表。
            history (list[Message]): 已加载的会话历史消息。
            run_id (str): 当前运行唯一标识。
            message_id (str): 当前用户消息唯一标识。

        返回值：
            tuple[list[AttachmentRef], list[Message]]: 附件轻量引用和 Harness 消息列表。

        异常：
            底层消息或附件持久化失败时，传播对应异常。
        """
        message = await self._get_or_create_user_message(
            session_id,
            user_message,
            run_id,
            message_id=message_id,
        )
        attachment_refs = await self._bind_message_attachments(
            session_id, message, attachment_ids
        )
        messages = self._build_harness_messages(history, message)
        return attachment_refs, messages

    async def _get_or_create_user_message(
        self,
        session_id: str,
        content: str,
        run_id: str,
        message_id: str,
    ) -> Message:
        """

        参数：
            session_id (str): 会话唯一标识。
            content (str): 待保存或处理的内容。
            run_id (str): 运行唯一标识。

        返回值：
            Message: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        existing = await self._db.messages.get(message_id)
        if existing is not None:
            return existing

        message = Message(
            id=message_id,
            session_id=session_id,
            role=MessageRole.USER,
            content=content,
            run_id=run_id,
            timestamp=datetime.now(),
        )
        await self._db.messages.save(message)
        return message

    async def _bind_message_attachments(
        self,
        session_id: str,
        message: Message,
        attachment_ids: list[str],
    ) -> list[AttachmentRef]:
        """

        参数：
            session_id (str): 会话唯一标识。
            message (Message): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            attachment_ids (list[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[AttachmentRef]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if not attachment_ids:
            return []
        attachments = await self._db.files.bind_message(
            session_id, message.id, attachment_ids
        )
        refs = [item.to_reference() for item in attachments]
        message.attachments = refs
        return refs

    @staticmethod
    def _build_harness_messages(
        history: list[Message],
        user_message: Message,
    ) -> list[Message]:
        """

        参数：
            history (list[Message]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            user_message (Message): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            list[Message]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        base_messages = [*history]
        if not any(item.id == user_message.id for item in base_messages):
            base_messages.append(user_message)

        messages: list[Message] = []
        for message in base_messages:
            if message.role != MessageRole.USER or not message.attachments:
                messages.append(message)
                continue
            refs = "\n".join(
                f"- file_id={ref.id}; name={ref.filename}; status={ref.status.value}; "
                f"mime={ref.mime_type}; size={ref.size_bytes}"
                for ref in message.attachments
            )
            context = (
                "\n\n[该用户消息关联的文件资产]\n"
                f"{refs}\n文件正文不会自动注入上下文。"
                "需要内容时，必须使用正式文件能力工具并传入上述 file_id。"
            )
            messages.append(
                message.model_copy(update={"content": message.content + context})
            )
        return messages
