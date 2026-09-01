"""File Intelligence 领域模型。

定义文件智能系统的核心数据结构，包括附件、分块和适配器信息。
所有模型使用 Pydantic v2，支持 JSON 序列化和数据库映射。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, Field

from athena.models.json_models import (
    FileLocator,
    FileMetadata,
)


class AttachmentStatus(StrEnum):
    """附件生命周期状态。

    状态流转：UPLOADED → PROCESSING → READY / FAILED → DELETED
    """

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class AttachmentRef(BaseModel):
    """附件的轻量引用（用于消息关联，不暴露存储细节）。

    属性：
        id: 附件 ID。
        filename: 原始文件名。
        mime_type: MIME 类型。
        size_bytes: 文件字节数。
        status: 当前状态。
    """

    id: str
    filename: str
    mime_type: str
    size_bytes: int
    status: AttachmentStatus


class Attachment(BaseModel):
    """附件完整领域模型。

    表示用户上传的文件资产，关联到特定会话。``storage_key`` 字段
    排除在 JSON 序列化之外（``exclude=True``），不暴露给前端。

    属性：
        id: 附件唯一标识（时间戳 ID）。
        session_id: 所属会话 ID。
        message_id: 关联的消息 ID（可选）。
        filename: 原始文件名。
        mime_type: MIME 类型。
        size_bytes: 文件字节数。
        sha256: 文件内容的 SHA-256 哈希。
        storage_key: 存储层的 blob 键（内部字段，不序列化）。
        adapter_name: 处理该文件的适配器名称。
        adapter_version: 适配器版本。
        status: 当前状态。
        capabilities: 适配器支持的能力列表（read/search/summarize 等）。
        metadata: 解析产生的元数据（页数、语言、符号数等）。
        error_message: 失败时的错误信息。
        created_at: 创建时间。
        updated_at: 最后更新时间。
        deleted_time: 软删除时间（``None`` 表示未删除）。
    """

    id: str
    session_id: str
    message_id: str | None = None
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_key: str = Field(exclude=True)
    adapter_name: str | None = None
    adapter_version: str | None = None
    status: AttachmentStatus = AttachmentStatus.UPLOADED
    capabilities: list[str] = Field(default_factory=list)
    metadata: FileMetadata = Field(default_factory=FileMetadata)
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    deleted_time: datetime | None = None

    def to_ref(self) -> AttachmentRef:
        """转换为轻量引用（用于消息关联）。"""
        return AttachmentRef(
            id=self.id,
            filename=self.filename,
            mime_type=self.mime_type,
            size_bytes=self.size_bytes,
            status=self.status,
        )


class FileChunk(BaseModel):
    """文件分块领域模型。

    文件解析后按大小分块存储，每个分块附带定位器和元数据，
    用于精准读取和搜索结果定位。

    属性：
        id: 分块唯一标识。
        attachment_id: 所属附件 ID。
        ordinal: 分块序号（从 0 开始）。
        content: 分块文本内容。
        token_count: 估算的 token 数量。
        locator: 定位信息（页码、行范围、字符偏移等）。
        metadata: 语义元数据（语言、样式等）。
    """

    id: str
    attachment_id: str
    ordinal: int
    content: str
    token_count: int = 0
    locator: FileLocator = Field(default_factory=FileLocator)
    metadata: FileMetadata = Field(default_factory=FileMetadata)
    # SQLite FTS5 的 BM25 是底层原生排序值，与文件检索对外暴露的融合分数保持分离。
    native_score: float | None = None


class AdapterInfo(BaseModel):
    """适配器注册信息。

    描述适配器的名称、版本、支持的文件类型和能力列表，
    用于适配器选择和 DB 同步。

    属性：
        name: 适配器名称（如 ``"text"``、``"pdf"``）。
        version: 适配器版本号。
        mime_types: 支持的 MIME 类型列表。
        extensions: 支持的文件扩展名列表。
        capabilities: 支持的能力列表（read/search/summarize/analyze 等）。
        enabled: 是否启用。
    """

    name: str
    version: str
    mime_types: list[str]
    extensions: list[str]
    capabilities: list[str]
    enabled: bool = True
