"""File Intelligence 领域模型。

定义文件智能系统的核心数据结构，包括附件、处理任务、分块和适配器信息。
所有模型使用 Pydantic v2，支持 JSON 序列化和数据库映射。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AttachmentStatus(StrEnum):
    """附件生命周期状态。

    状态流转：UPLOADED → QUEUED → PROCESSING → READY / FAILED → DELETED
    """

    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class FileTaskType(StrEnum):
    """文件处理任务类型。

    - FILE_PARSE: 文件解析（提取内容、分块）
    - FILE_INDEX: 向量索引构建
    - FILE_SUMMARY: 文档摘要生成
    - CODE_ANALYSIS: 代码符号和依赖分析
    - EMBEDDING_GENERATE: 嵌入向量生成
    """

    FILE_PARSE = "FILE_PARSE"
    FILE_INDEX = "FILE_INDEX"
    FILE_SUMMARY = "FILE_SUMMARY"
    CODE_ANALYSIS = "CODE_ANALYSIS"
    EMBEDDING_GENERATE = "EMBEDDING_GENERATE"


class FileTaskStatus(StrEnum):
    """文件处理任务状态。

    状态流转：QUEUED → RUNNING → COMPLETED / FAILED / CANCELLED
    重试时从 FAILED 回退到 QUEUED。
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttachmentRef(BaseModel):
    """附件的轻量引用（用于消息关联，不暴露存储细节）。

    Attributes:
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

    Attributes:
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
    metadata: dict[str, Any] = Field(default_factory=dict)
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


class FileTask(BaseModel):
    """文件处理任务领域模型。

    表示一个异步文件处理任务（解析、索引、摘要等）。
    支持重试（``attempts`` / ``max_attempts``）和优先级调度。

    Attributes:
        id: 任务唯一标识。
        session_id: 所属会话 ID。
        attachment_id: 关联的附件 ID。
        task_type: 任务类型。
        status: 当前状态。
        progress: 进度（0.0-1.0）。
        stage: 当前阶段描述。
        priority: 调度优先级（数值越大越优先）。
        attempts: 已尝试次数。
        max_attempts: 最大重试次数。
        payload: 任务参数。
        result: 任务结果。
        error_message: 失败时的错误信息。
        available_at: 可执行时间（用于退避调度）。
        created_at: 创建时间。
        updated_at: 最后更新时间。
        started_at: 开始执行时间。
        completed_at: 完成时间。
    """

    id: str
    session_id: str
    attachment_id: str
    task_type: FileTaskType
    status: FileTaskStatus = FileTaskStatus.QUEUED
    progress: float = 0.0
    stage: str = "queued"
    priority: int = 0
    attempts: int = 0
    max_attempts: int = 3
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class FileChunk(BaseModel):
    """文件分块领域模型。

    文件解析后按大小分块存储，每个分块附带定位器和元数据，
    用于精准读取和搜索结果定位。

    Attributes:
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
    locator: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AdapterInfo(BaseModel):
    """适配器注册信息。

    描述适配器的名称、版本、支持的文件类型和能力列表，
    用于适配器选择和 DB 同步。

    Attributes:
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
