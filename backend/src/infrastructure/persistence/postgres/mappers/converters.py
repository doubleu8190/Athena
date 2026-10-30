"""Session/Message ORM 与领域类型之间的显式转换。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from domain.sessions import (
    AttachmentRef,
    Message,
    MessageRole,
    Session,
    ToolCall,
)
from domain.files import (
    AdapterInfo,
    Attachment,
    AttachmentStatus,
    FileArtifact,
    FileChunk,
    FileLocator,
    FileMetadata,
)
from domain.knowledge import KnowledgeBase
from domain.tools import (
    JsonSchema,
    MCPServer,
    MCPServerConfig,
    RiskLevel,
    ToolConfig,
    ToolExecutionMode,
)

from ..models import (
    AdapterRegistryModel,
    AttachmentModel,
    FileArtifactModel,
    FileChunkModel,
    KnowledgeBaseModel,
    MCPServerModel,
    MessageModel,
    SessionModel,
    ToolModel,
)


def session_model_to_domain(row: SessionModel) -> Session:
    """把 Session ORM 行转换为领域实体。"""
    return Session(
        id=row.id,
        project_id=row.project_id,
        title=row.title,
        active_run_id=row.active_run_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_activity_at=row.last_activity_at,
        compression_summary=row.compression_summary,
        last_compressed_message_id=row.last_compressed_message_id,
        last_summarized_message_id=row.last_summarized_message_id,
    )


def session_domain_to_model(value: Session) -> SessionModel:
    """把领域会话转换为 Session ORM 行。"""
    return SessionModel(
        id=value.id,
        project_id=value.project_id,
        title=value.title,
        active_run_id=value.active_run_id,
        created_at=value.created_at,
        updated_at=value.updated_at,
        last_activity_at=value.last_activity_at,
        compression_summary=value.compression_summary,
        last_compressed_message_id=value.last_compressed_message_id,
        last_summarized_message_id=value.last_summarized_message_id,
    )


def message_model_to_domain(
    row: MessageModel,
    *,
    attachments: Sequence[AttachmentRef] = (),
) -> Message:
    """把 Message ORM 行和已加载附件引用转换为领域消息。"""
    raw_tool_calls = _load_tool_calls(row.tool_calls_json)
    return Message(
        id=row.id,
        session_id=row.session_id,
        role=MessageRole(row.role),
        content=row.content,
        timestamp=row.timestamp,
        tool_calls=tuple(
            ToolCall(
                id=item.get("id", ""),
                name=item.get("name", ""),
                args=(
                    dict(item["args"])
                    if isinstance(item.get("args"), dict)
                    else {}
                ),
            )
            for item in raw_tool_calls
        ),
        tool_call_id=row.tool_call_id,
        run_id=row.run_id,
        tool_call_record_id=row.tool_call_record_id,
        tool_name=row.tool_name,
        message_type=row.message_type,
        attachments=tuple(attachments),
    )


def message_domain_to_model(value: Message) -> MessageModel:
    """把领域消息转换为 Message ORM 行。"""
    tool_calls = [
        {"id": item.id, "name": item.name, "args": item.args}
        for item in value.tool_calls
    ]
    return MessageModel(
        id=value.id,
        session_id=value.session_id,
        role=value.role.value,
        content=value.content,
        tool_calls_json=tool_calls,
        tool_call_id=value.tool_call_id,
        run_id=value.run_id,
        tool_call_record_id=value.tool_call_record_id,
        tool_name=value.tool_name,
        message_type=value.message_type,
        timestamp=value.timestamp,
    )


def _load_tool_calls(value: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """解析消息工具调用 JSON，异常或非列表内容按空列表处理。"""
    return [item for item in value if isinstance(item, dict)] if value else []


def attachment_model_to_domain(row: AttachmentModel) -> Attachment:
    """把 Attachment ORM 行转换为领域实体。"""
    return Attachment(
        id=row.id,
        logical_document_id=row.logical_document_id,
        document_version=row.document_version,
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
        capabilities=tuple(_load_json_list(row.capabilities_json)),
        metadata=FileMetadata(_load_json_dict(row.parsed_metadata_json)),
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_time=row.deleted_time,
        active_index_revision_id=getattr(row, "active_index_revision_id", None),
    )


def attachment_domain_to_model(value: Attachment) -> AttachmentModel:
    """把附件领域实体转换为 ORM 行。"""
    return AttachmentModel(
        id=value.id,
        logical_document_id=value.logical_document_id,
        document_version=value.document_version,
        session_id=value.session_id,
        knowledge_base_id=value.knowledge_base_id,
        message_id=value.message_id,
        filename=value.filename,
        mime_type=value.mime_type,
        size_bytes=value.size_bytes,
        sha256=value.sha256,
        storage_key=value.storage_key,
        adapter_name=value.adapter_name,
        adapter_version=value.adapter_version,
        status=value.status.value,
        capabilities_json=list(value.capabilities),
        parsed_metadata_json=value.metadata.values,
        error_message=value.error_message,
        created_at=value.created_at,
        updated_at=value.updated_at,
        deleted_time=value.deleted_time,
        active_index_revision_id=value.active_index_revision_id,
    )


def file_chunk_model_to_domain(row: FileChunkModel) -> FileChunk:
    """把 FileChunk ORM 行转换为领域实体。"""
    return FileChunk(
        id=row.id,
        attachment_id=row.attachment_id,
        ordinal=row.ordinal,
        content=row.content,
        token_count=row.token_count,
        locator=FileLocator(_load_json_dict(row.locator_json)),
        metadata=FileMetadata(_load_json_dict(row.metadata_json)),
        native_score=row.native_score,
        index_revision_id=getattr(row, "index_revision_id", None),
    )


def file_chunk_domain_to_model(value: FileChunk) -> FileChunkModel:
    """把文件分块领域实体转换为 ORM 行。"""
    return FileChunkModel(
        id=value.id,
        attachment_id=value.attachment_id,
        ordinal=value.ordinal,
        content=value.content,
        token_count=value.token_count,
        locator_json=value.locator.values,
        metadata_json=value.metadata.values,
        native_score=value.native_score,
        index_revision_id=value.index_revision_id,
    )


def file_artifact_model_to_domain(row: FileArtifactModel) -> FileArtifact:
    """把文件产物 ORM 行转换为领域实体。"""
    return FileArtifact(
        id=row.id,
        attachment_id=row.attachment_id,
        kind=row.kind,
        cache_key=row.cache_key,
        content=row.content,
        storage_key=row.storage_key,
        metadata=FileMetadata(_load_json_dict(row.metadata_json)),
        created_at=row.created_at,
    )


def file_artifact_domain_to_model(value: FileArtifact) -> FileArtifactModel:
    """把文件产物领域实体转换为 ORM 行。"""
    return FileArtifactModel(
        id=value.id,
        attachment_id=value.attachment_id,
        kind=value.kind,
        cache_key=value.cache_key,
        content=value.content,
        storage_key=value.storage_key,
        metadata_json=value.metadata.values,
        created_at=value.created_at,
    )


def adapter_model_to_domain(row: AdapterRegistryModel) -> AdapterInfo:
    """把适配器注册 ORM 行转换为领域值对象。"""
    return AdapterInfo(
        name=row.name,
        version=row.version,
        mime_types=tuple(str(item) for item in row.mime_types_json),
        extensions=tuple(str(item) for item in row.extensions_json),
        capabilities=tuple(str(item) for item in row.capabilities_json),
        enabled=row.enabled,
    )


def adapter_domain_to_model(
    value: AdapterInfo, *, updated_at: datetime
) -> AdapterRegistryModel:
    """把适配器注册值对象转换为 ORM 行。"""
    return AdapterRegistryModel(
        name=value.name,
        version=value.version,
        mime_types_json=list(value.mime_types),
        extensions_json=list(value.extensions),
        capabilities_json=list(value.capabilities),
        enabled=value.enabled,
        updated_at=updated_at,
    )


def knowledge_base_model_to_domain(
    row: KnowledgeBaseModel,
    *,
    documents: Sequence[AttachmentModel] = (),
) -> KnowledgeBase:
    """把知识库 ORM 行和文档统计转换为领域实体。"""
    ready = [item for item in documents if item.status == AttachmentStatus.READY.value]
    return KnowledgeBase(
        id=row.id,
        name=row.name,
        description=row.description,
        document_count=len(documents),
        ready_document_count=len(ready),
        total_size_bytes=sum(item.size_bytes for item in documents),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def knowledge_base_domain_to_model(value: KnowledgeBase) -> KnowledgeBaseModel:
    """把知识库领域实体转换为 ORM 行。"""
    return KnowledgeBaseModel(
        id=value.id,
        name=value.name,
        description=value.description,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def tool_model_to_domain(row: ToolModel) -> ToolConfig:
    """把工具 ORM 行转换为领域配置。"""
    return ToolConfig(
        tool_name=row.tool_name,
        execution_mode=ToolExecutionMode(row.execution_mode),
        server_name=row.server_name,
        remote_name=row.remote_name,
        description=row.description,
        parameters=JsonSchema(_load_json_dict(row.parameters_schema_json)),
        risk_level=RiskLevel(row.risk_level),
        require_approval=row.require_approval,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def tool_domain_to_model(value: ToolConfig) -> ToolModel:
    """把工具领域配置转换为 ORM 行。"""
    return ToolModel(
        tool_name=value.tool_name,
        execution_mode=value.execution_mode.value,
        server_name=value.server_name,
        remote_name=value.remote_name,
        description=value.description,
        parameters_schema_json=value.parameters.values,
        risk_level=value.risk_level.value,
        require_approval=value.require_approval,
        enabled=value.enabled,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def mcp_server_model_to_domain(row: MCPServerModel) -> MCPServer:
    """把 MCP ORM 行转换为领域实体。"""
    value = _load_json_dict(row.config_json)
    command = value.get("command", "")
    return MCPServer(
        name=row.name,
        config=MCPServerConfig(
            command=tuple(command) if isinstance(command, list) else command,
            args=tuple(item for item in value.get("args", []) if isinstance(item, str)),
            env={str(key): str(item) for key, item in value.get("env", {}).items()},
            image_id=value.get("image_id"),
            network_policy=str(value.get("network_policy", "none")),
            enabled=bool(value.get("enabled", True)),
        ),
        created_at=row.created_at,
        deleted_time=row.deleted_time,
    )


def mcp_server_domain_to_model(value: MCPServer) -> MCPServerModel:
    """把 MCP 领域实体转换为 ORM 行。"""
    command = value.config.command
    return MCPServerModel(
        name=value.name,
        config_json={
            "command": list(command) if isinstance(command, tuple) else command,
            "args": list(value.config.args),
            "env": value.config.env,
            "image_id": value.config.image_id,
            "network_policy": value.config.network_policy,
            "enabled": value.config.enabled,
        },
        created_at=value.created_at,
        deleted_time=value.deleted_time,
    )


def _load_json_dict(value: dict[str, Any]) -> dict[str, Any]:
    """读取 JSONB 对象，复制为领域值对象使用的字典。"""
    return dict(value) if isinstance(value, dict) else {}


def _load_json_list(value: list[Any]) -> list[Any]:
    """读取 JSONB 列表，复制为领域值对象使用的列表。"""
    return list(value) if isinstance(value, list) else []
