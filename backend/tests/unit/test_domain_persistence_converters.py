"""Files/Knowledge/Tools/MCP 持久化转换测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from domain.files import (
    AdapterInfo,
    Attachment,
    AttachmentStatus,
    FileChunk,
    FileArtifact,
    FileLocator,
    FileMetadata,
    KnowledgeBase,
)
from domain.tools import (
    JsonSchema,
    MCPServer,
    MCPServerConfig,
    RiskLevel,
    ToolConfig,
    ToolExecutionMode,
)
from infrastructure.persistence.postgres.repositories.converters import (
    adapter_domain_to_model,
    adapter_model_to_domain,
    attachment_domain_to_model,
    attachment_model_to_domain,
    file_chunk_domain_to_model,
    file_chunk_model_to_domain,
    file_artifact_domain_to_model,
    file_artifact_model_to_domain,
    knowledge_base_domain_to_model,
    knowledge_base_model_to_domain,
    mcp_server_domain_to_model,
    mcp_server_model_to_domain,
    tool_domain_to_model,
    tool_model_to_domain,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_file_and_knowledge_converters_preserve_domain_values() -> None:
    """文件和知识库转换不丢失所有权、元数据和版本字段。"""
    attachment = Attachment(
        id="file-1",
        logical_document_id="doc-1",
        document_version=2,
        session_id=None,
        knowledge_base_id="kb-1",
        message_id=None,
        filename="a.txt",
        mime_type="text/plain",
        size_bytes=3,
        sha256="hash",
        storage_key="blob/a",
        adapter_name="text",
        adapter_version="1",
        status=AttachmentStatus.READY,
        capabilities=("read", "search"),
        metadata=FileMetadata({"language": "zh"}),
        error_message=None,
        created_at=NOW,
        updated_at=NOW,
    )
    chunk = FileChunk(
        id="chunk-1",
        attachment_id="file-1",
        ordinal=0,
        content="text",
        token_count=1,
        locator=FileLocator({"page": 1}),
        metadata=FileMetadata({"kind": "paragraph"}),
        native_score=0.5,
    )
    knowledge_base = KnowledgeBase(
        id="kb-1",
        name="KB",
        description="desc",
        document_count=1,
        ready_document_count=1,
        total_size_bytes=3,
        created_at=NOW,
        updated_at=NOW,
    )

    assert attachment_model_to_domain(attachment_domain_to_model(attachment)) == attachment
    assert file_chunk_model_to_domain(file_chunk_domain_to_model(chunk)) == chunk
    restored_knowledge_base = knowledge_base_model_to_domain(
        knowledge_base_domain_to_model(knowledge_base),
        documents=[attachment_domain_to_model(attachment)],
    )
    assert restored_knowledge_base == knowledge_base


def test_tool_and_mcp_converters_preserve_json_boundaries() -> None:
    """工具 Schema 和 MCP 环境变量在 JSON 边界保持结构。"""
    tool = ToolConfig(
        tool_name="search",
        execution_mode=ToolExecutionMode.NATIVE,
        server_name=None,
        remote_name=None,
        description="Search",
        parameters=JsonSchema({"type": "object"}),
        risk_level=RiskLevel.LOW,
        require_approval=False,
        enabled=True,
        created_at=NOW,
        updated_at=NOW,
    )
    server = MCPServer(
        name="local",
        config=MCPServerConfig(
            command=("python", "-m"),
            args=("server",),
            env={"TOKEN": "secret"},
            image_id=None,
            network_policy="none",
            enabled=True,
        ),
        created_at=NOW,
    )

    assert tool_model_to_domain(tool_domain_to_model(tool)) == tool
    assert mcp_server_model_to_domain(mcp_server_domain_to_model(server)) == server


def test_file_artifact_and_adapter_converters_preserve_cache_contract() -> None:
    """文件产物缓存键和适配器能力列表在 JSON 边界保持不变。"""
    artifact = FileArtifact(
        id="artifact-1",
        attachment_id="file-1",
        kind="text",
        cache_key="cache-1",
        content="content",
        storage_key=None,
        metadata=FileMetadata({"pages": 1}),
        created_at=NOW,
    )
    adapter = AdapterInfo(
        name="text",
        version="1",
        mime_types=("text/plain",),
        extensions=(".txt",),
        capabilities=("read", "search"),
    )

    assert file_artifact_model_to_domain(file_artifact_domain_to_model(artifact)) == artifact
    assert adapter_model_to_domain(
        adapter_domain_to_model(adapter, updated_at=NOW.isoformat())
    ) == adapter
