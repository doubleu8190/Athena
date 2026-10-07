"""文件与知识库领域实体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class AttachmentStatus(StrEnum):
    """附件生命周期状态。"""

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class FileLocator:
    """文件内容在原始文档中的位置。"""

    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """文件解析产生的结构化元数据。"""

    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Attachment:
    """附件或知识库文档实体。"""

    id: str
    logical_document_id: str
    document_version: int
    session_id: str | None
    knowledge_base_id: str | None
    message_id: str | None
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_key: str
    adapter_name: str | None
    adapter_version: str | None
    status: AttachmentStatus
    capabilities: tuple[str, ...]
    metadata: FileMetadata
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    deleted_time: datetime | None = None

    def to_reference(self):
        """生成消息使用的轻量附件引用。"""
        from domain.sessions import AttachmentRef

        return AttachmentRef(
            id=self.id,
            filename=self.filename,
            mime_type=self.mime_type,
            size_bytes=self.size_bytes,
            status=self.status,
        )


@dataclass(frozen=True, slots=True)
class FileChunk:
    """附件解析后的文本分块。"""

    id: str
    attachment_id: str
    ordinal: int
    content: str
    token_count: int
    locator: FileLocator
    metadata: FileMetadata
    native_score: float | None = None


@dataclass(frozen=True, slots=True)
class FileArtifact:
    """文件解析产生的可缓存产物。"""

    id: str
    attachment_id: str
    kind: str
    cache_key: str
    content: str | None
    storage_key: str | None
    metadata: FileMetadata
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    """文件解析适配器注册信息。"""

    name: str
    version: str
    mime_types: tuple[str, ...]
    extensions: tuple[str, ...]
    capabilities: tuple[str, ...]
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class KnowledgeBase:
    """可跨会话复用的知识库。"""

    id: str
    name: str
    description: str
    document_count: int
    ready_document_count: int
    total_size_bytes: int
    created_at: datetime
    updated_at: datetime
