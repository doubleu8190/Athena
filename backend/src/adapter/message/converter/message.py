"""消息 ORM 行、parts 与 metadata 的纯转换。"""

from __future__ import annotations

from datetime import UTC, datetime

from adapter.message.model.conversation import ConversationMessageModel
from adapter.message.model.execution import ExecutionMessageModel
from domain.message import (
    ConversationMessage,
    ConversationRole,
    ExecutionMessage,
    ExecutionRole,
    FileRefPart,
    InvalidMessageError,
    MessageMetadata,
    MessagePart,
    MessageStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)


def _as_utc(value: datetime) -> datetime:
    """补齐 SQLite 等测试驱动丢失的 UTC 时区信息。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def serialize_metadata(metadata: MessageMetadata) -> dict[str, object]:
    """只序列化白名单元数据字段。"""
    values: dict[str, object] = {}
    if metadata.provider is not None:
        values["provider"] = metadata.provider
    if metadata.model is not None:
        values["model"] = metadata.model
    if metadata.finish_reason is not None:
        values["finish_reason"] = metadata.finish_reason
    if metadata.trace_id is not None:
        values["trace_id"] = metadata.trace_id
    return values


def _optional_string(values: dict[str, object], key: str) -> str | None:
    """读取可选字符串元数据。"""
    value = values.get(key)
    return value if isinstance(value, str) else None


def deserialize_metadata(values: dict[str, object]) -> MessageMetadata:
    """从白名单字段恢复元数据，忽略未来新增字段。"""
    return MessageMetadata(
        provider=_optional_string(values, "provider"),
        model=_optional_string(values, "model"),
        finish_reason=_optional_string(values, "finish_reason"),
        trace_id=_optional_string(values, "trace_id"),
    )


def serialize_parts(parts: tuple[MessagePart, ...]) -> list[dict[str, object]]:
    """把类型化内容块转换为 JSON 安全结构。"""
    values: list[dict[str, object]] = []
    for part in parts:
        if isinstance(part, TextPart):
            values.append({"type": "text", "text": part.text})
        elif isinstance(part, FileRefPart):
            item: dict[str, object] = {
                "type": "file_ref",
                "file_id": part.file_id,
                "filename": part.filename,
            }
            if part.mime_type is not None:
                item["mime_type"] = part.mime_type
            if part.size_bytes is not None:
                item["size_bytes"] = part.size_bytes
            values.append(item)
        elif isinstance(part, ToolCallPart):
            values.append(
                {
                    "type": "tool_call",
                    "tool_call_id": part.tool_call_id,
                    "name": part.name,
                    "arguments": part.arguments,
                }
            )
        elif isinstance(part, ToolResultPart):
            item = {
                "type": "tool_result",
                "tool_call_id": part.tool_call_id,
                "status": part.status.value,
            }
            if part.output is not None:
                item["output"] = part.output
            if part.artifact_id is not None:
                item["artifact_id"] = part.artifact_id
            values.append(item)
    return values


def _required_string(values: dict[str, object], key: str) -> str:
    """读取必填字符串内容块字段。"""
    value = values.get(key)
    if not isinstance(value, str):
        raise InvalidMessageError("stored_message_invalid")
    return value


def _object_dict(value: object) -> dict[str, object]:
    """校验并复制 JSON 对象。"""
    if not isinstance(value, dict):
        raise InvalidMessageError("stored_message_invalid")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise InvalidMessageError("stored_message_invalid")
        result[key] = item
    return result


def deserialize_parts(values: list[dict[str, object]]) -> tuple[MessagePart, ...]:
    """把 JSON 内容块恢复为类型化领域对象。"""
    parts: list[MessagePart] = []
    for value in values:
        part_type = _required_string(value, "type")
        if part_type == "text":
            parts.append(TextPart(text=_required_string(value, "text")))
        elif part_type == "file_ref":
            raw_size = value.get("size_bytes")
            if raw_size is not None and not isinstance(raw_size, int):
                raise InvalidMessageError("stored_message_invalid")
            parts.append(
                FileRefPart(
                    file_id=_required_string(value, "file_id"),
                    filename=_required_string(value, "filename"),
                    mime_type=_optional_string(value, "mime_type"),
                    size_bytes=raw_size,
                )
            )
        elif part_type == "tool_call":
            parts.append(
                ToolCallPart(
                    tool_call_id=_required_string(value, "tool_call_id"),
                    name=_required_string(value, "name"),
                    arguments=_object_dict(value.get("arguments")),
                )
            )
        elif part_type == "tool_result":
            raw_status = _required_string(value, "status")
            try:
                status = ToolResultStatus(raw_status)
            except ValueError as error:
                raise InvalidMessageError("stored_message_invalid") from error
            raw_output = value.get("output")
            if raw_output is not None and not isinstance(raw_output, str):
                raise InvalidMessageError("stored_message_invalid")
            parts.append(
                ToolResultPart(
                    tool_call_id=_required_string(value, "tool_call_id"),
                    status=status,
                    output=raw_output,
                    artifact_id=_optional_string(value, "artifact_id"),
                )
            )
        else:
            raise InvalidMessageError("stored_message_invalid")
    return tuple(parts)


def conversation_to_domain(row: ConversationMessageModel) -> ConversationMessage:
    """把会话消息 ORM 行转换为领域对象。"""
    return ConversationMessage(
        id=row.id,
        session_id=row.session_id,
        role=ConversationRole(row.role),
        status=MessageStatus(row.status),
        sequence=row.sequence,
        parts=deserialize_parts(row.parts),
        reply_to_message_id=row.reply_to_message_id,
        run_id=row.run_id,
        client_message_id=row.client_message_id,
        created_at=_as_utc(row.created_at),
        completed_at=_as_utc(row.completed_at) if row.completed_at is not None else None,
        metadata=deserialize_metadata(row.metadata_json),
    )


def execution_to_domain(row: ExecutionMessageModel) -> ExecutionMessage:
    """把执行轨迹 ORM 行转换为领域对象。"""
    return ExecutionMessage(
        id=row.id,
        session_id=row.session_id,
        run_id=row.run_id,
        thread_id=row.thread_id,
        sequence=row.sequence,
        role=ExecutionRole(row.role),
        parts=deserialize_parts(row.parts),
        tool_call_id=row.tool_call_id,
        source_message_id=row.source_message_id,
        created_at=_as_utc(row.created_at),
        metadata=deserialize_metadata(row.metadata_json),
    )


__all__ = [
    "conversation_to_domain",
    "deserialize_metadata",
    "deserialize_parts",
    "execution_to_domain",
    "serialize_metadata",
    "serialize_parts",
]
