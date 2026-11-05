"""消息写入与历史查询用例。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from app.message.converter.result import (
    conversation_message_to_result,
    conversation_page_to_result,
)
from app.message.model.result import (
    ConversationMessagePageResult,
    ConversationMessageResult,
)
from common.ids import now_utc
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    FileRefPart,
    InvalidMessageError,
    MessageMetadata,
    MessagePart,
    MessageRepository,
    MessageStatus,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)

MAX_PAGE_SIZE = 100
MAX_IDENTIFIER_LENGTH = 128
MAX_MESSAGE_ID_LENGTH = 64
_FINAL_STATUSES = {
    MessageStatus.COMPLETED,
    MessageStatus.FAILED,
    MessageStatus.CANCELLED,
}


def _default_message_id() -> str:
    """生成消息主键。"""
    return f"msg_{uuid4().hex}"


def _utc_value(value: datetime) -> datetime:
    """把时钟输出规范化为 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_identifier(
    value: str,
    error: str,
    *,
    max_length: int = MAX_IDENTIFIER_LENGTH,
) -> str:
    """校验必填标识符。"""
    if not isinstance(value, str) or not value or not value.strip() or len(value) > max_length:
        raise InvalidMessageError(error)
    return value


def _optional_identifier(
    value: str | None,
    error: str,
    *,
    max_length: int = MAX_IDENTIFIER_LENGTH,
) -> str | None:
    """校验可选标识符。"""
    if value is None:
        return None
    return _require_identifier(value, error, max_length=max_length)


def _validate_file_part(part: FileRefPart) -> None:
    """校验文件引用字段。"""
    _require_identifier(part.file_id, "invalid_file_reference")
    _require_identifier(part.filename, "invalid_file_reference")
    if part.mime_type is not None and not part.mime_type.strip():
        raise InvalidMessageError("invalid_file_reference")
    if part.size_bytes is not None and (isinstance(part.size_bytes, bool) or part.size_bytes < 0):
        raise InvalidMessageError("invalid_file_reference")


def _validate_tool_call_part(part: ToolCallPart) -> None:
    """校验工具调用字段。"""
    _require_identifier(part.tool_call_id, "invalid_tool_call")
    _require_identifier(part.name, "invalid_tool_call")
    if not isinstance(part.arguments, dict):
        raise InvalidMessageError("invalid_tool_call")
    try:
        json.dumps(part.arguments, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise InvalidMessageError("invalid_tool_call") from error


def _validate_tool_result_part(part: ToolResultPart) -> None:
    """校验工具结果字段及输出边界。"""
    _require_identifier(part.tool_call_id, "invalid_tool_result")
    if not isinstance(part.status, ToolResultStatus):
        raise InvalidMessageError("invalid_tool_result")
    if part.output is not None and not isinstance(part.output, str):
        raise InvalidMessageError("invalid_tool_result")
    if part.artifact_id is not None:
        _require_identifier(part.artifact_id, "invalid_tool_result")
    has_output = isinstance(part.output, str) and bool(part.output.strip())
    if not has_output and part.artifact_id is None:
        raise InvalidMessageError("invalid_tool_result")


def _validate_conversation_parts(parts: tuple[MessagePart, ...]) -> None:
    """限制公开会话只包含文本和文件引用。"""
    if not parts:
        raise InvalidMessageError("message_empty")
    has_content = False
    for part in parts:
        if isinstance(part, TextPart):
            if not part.text.strip():
                raise InvalidMessageError("message_text_empty")
            has_content = True
        elif isinstance(part, FileRefPart):
            _validate_file_part(part)
            has_content = True
        else:
            raise InvalidMessageError("conversation_part_not_allowed")
    if not has_content:
        raise InvalidMessageError("message_empty")


def _validate_execution_parts(
    role: ExecutionRole,
    parts: tuple[MessagePart, ...],
) -> str | None:
    """按执行角色校验内容并返回 tool 消息的关联 ID。"""
    if not parts:
        raise InvalidMessageError("message_empty")

    if role is ExecutionRole.TOOL:
        if len(parts) != 1 or not isinstance(parts[0], ToolResultPart):
            raise InvalidMessageError("invalid_tool_message")
        result = parts[0]
        _validate_tool_result_part(result)
        return result.tool_call_id

    if any(isinstance(part, ToolResultPart) for part in parts):
        raise InvalidMessageError("tool_result_role_invalid")

    seen_call_ids: set[str] = set()
    has_content = False
    for part in parts:
        if isinstance(part, TextPart):
            if not part.text.strip():
                raise InvalidMessageError("message_text_empty")
            has_content = True
        elif isinstance(part, FileRefPart):
            _validate_file_part(part)
            has_content = True
        elif isinstance(part, ToolCallPart):
            if role is not ExecutionRole.ASSISTANT:
                raise InvalidMessageError("execution_part_not_allowed")
            _validate_tool_call_part(part)
            if part.tool_call_id in seen_call_ids:
                raise InvalidMessageError("duplicate_tool_call_id")
            seen_call_ids.add(part.tool_call_id)
            has_content = True
        else:
            raise InvalidMessageError("execution_part_not_allowed")

    if not has_content:
        raise InvalidMessageError("message_empty")
    return None


class MessageService:
    """为 Run 写入稳定消息，并提供只读会话历史。"""

    def __init__(
        self,
        repository: MessageRepository,
        *,
        id_factory: Callable[[], str] = _default_message_id,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存仓储及可替换的 ID、时钟来源。"""
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    async def append_user_message(
        self,
        session_id: str,
        parts: tuple[MessagePart, ...],
        *,
        owner_id: str,
        run_id: str,
        client_message_id: str,
        metadata: MessageMetadata | None = None,
    ) -> ConversationMessageResult:
        """由 Run 提交流程幂等写入当前用户消息。"""
        _require_identifier(
            session_id,
            "invalid_session_id",
            max_length=MAX_MESSAGE_ID_LENGTH,
        )
        owner = _require_identifier(owner_id, "invalid_owner_id")
        _require_identifier(run_id, "invalid_run_id")
        _require_identifier(client_message_id, "invalid_client_message_id")
        _validate_conversation_parts(parts)
        current = _utc_value(self._clock())
        message = await self._repository.append_conversation(
            ConversationMessageDraft(
                id=self._id_factory(),
                session_id=session_id,
                owner_id=owner,
                role=ConversationRole.USER,
                status=MessageStatus.COMPLETED,
                parts=parts,
                reply_to_message_id=None,
                run_id=run_id,
                client_message_id=client_message_id,
                created_at=current,
                completed_at=current,
                metadata=metadata or MessageMetadata(),
            )
        )
        return conversation_message_to_result(message)

    async def append_assistant_message(
        self,
        session_id: str,
        parts: tuple[MessagePart, ...],
        *,
        owner_id: str,
        run_id: str,
        reply_to_message_id: str | None,
        status: MessageStatus = MessageStatus.COMPLETED,
        metadata: MessageMetadata | None = None,
    ) -> ConversationMessageResult:
        """由 Run 完成流程写入最终助手消息。"""
        _require_identifier(
            session_id,
            "invalid_session_id",
            max_length=MAX_MESSAGE_ID_LENGTH,
        )
        owner = _require_identifier(owner_id, "invalid_owner_id")
        _require_identifier(run_id, "invalid_run_id")
        _optional_identifier(
            reply_to_message_id,
            "invalid_reply_to_message_id",
            max_length=MAX_MESSAGE_ID_LENGTH,
        )
        if status not in _FINAL_STATUSES:
            raise InvalidMessageError("message_status_not_supported")
        _validate_conversation_parts(parts)
        current = _utc_value(self._clock())
        message = await self._repository.append_conversation(
            ConversationMessageDraft(
                id=self._id_factory(),
                session_id=session_id,
                owner_id=owner,
                role=ConversationRole.ASSISTANT,
                status=status,
                parts=parts,
                reply_to_message_id=reply_to_message_id,
                run_id=run_id,
                client_message_id=None,
                created_at=current,
                completed_at=current,
                metadata=metadata or MessageMetadata(),
            )
        )
        return conversation_message_to_result(message)

    async def append_execution_message(
        self,
        session_id: str,
        *,
        owner_id: str,
        run_id: str,
        thread_id: str,
        role: ExecutionRole,
        parts: tuple[MessagePart, ...],
        source_message_id: str | None = None,
        metadata: MessageMetadata | None = None,
    ) -> None:
        """由执行器写入 system/user/assistant/tool 轨迹。"""
        _require_identifier(
            session_id,
            "invalid_session_id",
            max_length=MAX_MESSAGE_ID_LENGTH,
        )
        owner = _require_identifier(owner_id, "invalid_owner_id")
        _require_identifier(run_id, "invalid_run_id")
        _require_identifier(thread_id, "invalid_thread_id")
        _optional_identifier(
            source_message_id,
            "invalid_source_message_id",
            max_length=MAX_MESSAGE_ID_LENGTH,
        )
        if not isinstance(role, ExecutionRole):
            raise InvalidMessageError("execution_role_invalid")
        tool_call_id = _validate_execution_parts(role, parts)
        await self._repository.append_execution(
            ExecutionMessageDraft(
                id=self._id_factory(),
                session_id=session_id,
                owner_id=owner,
                run_id=run_id,
                thread_id=thread_id,
                role=role,
                parts=parts,
                tool_call_id=tool_call_id,
                source_message_id=source_message_id,
                created_at=_utc_value(self._clock()),
                metadata=metadata or MessageMetadata(),
            )
        )

    async def list_conversation(
        self,
        session_id: str,
        *,
        viewer_id: str,
        before_sequence: int | None = None,
        limit: int = 20,
    ) -> ConversationMessagePageResult:
        """按所有者读取会话历史，不返回执行轨迹。"""
        _require_identifier(
            session_id,
            "invalid_session_id",
            max_length=MAX_MESSAGE_ID_LENGTH,
        )
        _require_identifier(viewer_id, "invalid_viewer_id")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise InvalidMessageError("invalid_pagination")
        if before_sequence is not None and before_sequence < 1:
            raise InvalidMessageError("invalid_before_sequence")
        page = await self._repository.list_conversation_for_owner(
            session_id,
            viewer_id,
            before_sequence=before_sequence,
            limit=limit,
        )
        if page is None:
            raise SessionNotFoundError("session_not_found")
        return conversation_page_to_result(page)


__all__ = ["MAX_PAGE_SIZE", "MessageService"]
