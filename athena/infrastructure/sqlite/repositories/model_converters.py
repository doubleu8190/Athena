"""SQLite ORM 行到领域模型的转换。"""

from __future__ import annotations

from datetime import datetime

from athena.infrastructure.sqlite.models import (
    ApprovalLogModel,
    AttachmentModel,
    MCPServerModel,
    MessageModel,
    SessionModel,
    ToolCallModel,
    ToolModel,
)
from athena.models import (
    ApprovalDecision,
    ApprovalLog,
    Attachment,
    AttachmentStatus,
    MCPServer,
    MCPServerConfig,
    Message,
    MessageRole,
    RiskLevel,
    Session,
    SessionStatus,
    ToolCallRecord,
    ToolCallStatus,
    ToolConfig,
    ToolExecutionMode,
)
from athena.models.json_models import FileMetadata, JsonSchema, ToolCall

from .repository_utils import _json_dumps, _json_loads, _json_loads_model


def _message_to_model(message: Message) -> MessageModel:
    """将领域消息转换为消息 ORM 行。"""
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


def _row_to_session(row: SessionModel) -> Session:
    """将会话 ORM 行转换为领域模型。"""
    return Session(
        id=row.id,
        title=row.title,
        status=SessionStatus(row.status),
        run_id=row.run_id,
        created_at=datetime.fromisoformat(row.created_at),
        updated_at=datetime.fromisoformat(row.updated_at),
        compression_summary=row.compression_summary,
        last_compressed_message_id=row.last_compressed_message_id,
        last_summarized_message_id=row.last_summarized_message_id,
    )


def _row_to_message(row: MessageModel) -> Message:
    """将消息 ORM 行转换为领域模型。"""
    return Message(
        id=row.id,
        session_id=row.session_id,
        role=MessageRole(row.role),
        content=row.content,
        tool_calls=[
            ToolCall.model_validate(item)
            for item in _json_loads(row.tool_calls_json, [])
            if isinstance(item, dict)
        ],
        tool_call_id=row.tool_call_id,
        run_id=row.run_id,
        tool_call_record_id=row.tool_call_record_id,
        tool_name=row.tool_name,
        type=row.type,
        timestamp=datetime.fromisoformat(row.timestamp),
    )


def _row_to_tool_call(row: ToolCallModel) -> ToolCallRecord:
    """将工具调用 ORM 行转换为领域模型。"""
    return ToolCallRecord(
        id=row.id,
        session_id=row.session_id,
        step_id=row.step_id,
        run_id=row.run_id,
        attempt_number=row.attempt_number,
        approval_id=row.approval_id,
        tool_name=row.tool_name,
        arguments=_json_loads(row.arguments_json, {}),
        raw_output=row.raw_output,
        status=ToolCallStatus(row.status),
        started_at=datetime.fromisoformat(row.started_at),
        completed_at=(
            datetime.fromisoformat(row.completed_at) if row.completed_at else None
        ),
        duration_ms=row.duration_ms,
        error_message=row.error_message,
        error_stack=row.error_stack,
    )


def _row_to_approval_log(row: ApprovalLogModel) -> ApprovalLog:
    """将审批日志 ORM 行转换为领域模型。"""
    return ApprovalLog(
        id=row.id,
        session_id=row.session_id,
        tool_call_id=row.tool_call_id,
        tool_name=row.tool_name,
        arguments=_json_loads(row.arguments_json, {}),
        risk_level=row.risk_level,
        decision=ApprovalDecision(row.decision),
        decision_time_ms=row.decision_time_ms,
        timestamp=datetime.fromisoformat(row.timestamp),
    )


def _row_to_mcp_server(row: MCPServerModel) -> MCPServer:
    """将 MCP 服务端 ORM 行转换为领域模型。"""
    return MCPServer(
        name=row.name,
        config=_json_loads_model(
            row.config_json, MCPServerConfig, MCPServerConfig(command="")
        ),
        created_at=datetime.fromisoformat(row.created_at),
        deleted_time=(
            datetime.fromisoformat(row.deleted_time) if row.deleted_time else None
        ),
    )


def _row_to_tool_config(row: ToolModel) -> ToolConfig:
    """将工具配置 ORM 行转换为领域模型。"""
    return ToolConfig(
        tool_name=row.tool_name,
        execution_mode=ToolExecutionMode(row.execution_mode),
        server_name=row.server_name,
        remote_name=row.remote_name,
        description=row.description,
        parameters=_json_loads_model(row.parameters_json, JsonSchema, JsonSchema()),
        risk_level=RiskLevel(row.risk_level),
        require_approval=bool(row.require_approval),
        enabled=bool(row.enabled),
        created_at=datetime.fromisoformat(row.created_at),
        updated_at=datetime.fromisoformat(row.updated_at),
    )


def _row_to_attachment(row: AttachmentModel) -> Attachment:
    """将附件 ORM 行转换为领域模型。"""
    return Attachment(
        id=row.id,
        session_id=row.session_id,
        knowledge_base_id=row.knowledge_base_id,
        message_id=row.message_id,
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        storage_key=row.storage_key,
        adapter_name=row.adapter_name,
        adapter_version=row.adapter_version,
        status=AttachmentStatus(row.status),
        capabilities=_json_loads(row.capabilities_json, []),
        metadata=_json_loads_model(row.metadata_json, FileMetadata, FileMetadata()),
        error_message=row.error_message,
        created_at=datetime.fromisoformat(row.created_at),
        updated_at=datetime.fromisoformat(row.updated_at),
        deleted_time=(
            datetime.fromisoformat(row.deleted_time) if row.deleted_time else None
        ),
    )
