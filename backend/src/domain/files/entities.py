"""文件与附件领域实体。"""

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
class AttachmentRef:
    """供消息引用的轻量附件值对象。"""

    id: str
    filename: str
    mime_type: str
    size_bytes: int
    status: AttachmentStatus


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
    """可属于会话或知识库的附件实体。"""

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

    def to_attachment_reference(self) -> AttachmentRef:
        """生成供消息使用的轻量附件引用。"""
        return AttachmentRef(
            id=self.id,
            filename=self.filename,
            mime_type=self.mime_type,
            size_bytes=self.size_bytes,
            status=self.status,
        )

    def to_reference(self) -> AttachmentRef:
        """兼容旧调用方；新代码使用 ``to_attachment_reference``。"""
        return self.to_attachment_reference()


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
