"""知识库核心模型；不依赖 HTTP、ORM 或其他业务域。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_PROJECT_KNOWLEDGE_BASES = 20
MAX_ORIGINAL_BYTES = 20 * 1024 * 1024


class OriginalStorageError(RuntimeError):
    """Safe failure raised by the original-object storage adapter."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OriginalStorageUnavailableError(OriginalStorageError):
    """The object store is temporarily unreachable or overloaded."""

    def __init__(self) -> None:
        super().__init__("knowledge_storage_unavailable")


class OriginalStorageNotFoundError(OriginalStorageError):
    """The requested original object does not exist."""

    def __init__(self) -> None:
        super().__init__("knowledge_storage_object_not_found")


class OriginalStorageAccessDeniedError(OriginalStorageError):
    """The deployment credentials cannot perform the requested operation."""

    def __init__(self) -> None:
        super().__init__("knowledge_storage_access_denied")


class OriginalStorageIntegrityError(OriginalStorageError):
    """Stored bytes violate the content-addressed original contract."""

    def __init__(self, code: str = "knowledge_storage_integrity_error") -> None:
        super().__init__(code)


class OriginalStorageConfigurationError(OriginalStorageError):
    """MinIO configuration is missing, invalid, or points at an absent bucket."""

    def __init__(self, code: str = "knowledge_storage_config_invalid") -> None:
        super().__init__(code)


class ProjectKnowledgeLimitReachedError(ValueError):
    """A project already has the maximum number of active knowledge bases."""


class DocumentState(StrEnum):
    """文档版本处理状态。"""

    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ChunkLocator:
    """知识片段在原始资料中的位置。字段按来源类型解释。"""

    kind: str
    page: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    heading: str | None = None
    sheet: str | None = None
    cell_range: str | None = None
    slide: int | None = None
    shape: str | None = None
    media_id: str | None = None


@dataclass(frozen=True, slots=True)
class TextBlock:
    """解析器输出的有序文本块。"""

    text: str
    locator: ChunkLocator
    kind: str = "text"


@dataclass(frozen=True, slots=True)
class KnowledgeBase:
    """用户私有知识库。"""

    id: str
    owner_id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    deleted_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    """文档历史版本的可见状态。"""

    id: str
    document_id: str
    state: DocumentState
    error_code: str | None
    warnings: tuple[str, ...]
    chunk_count: int
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class Document:
    """稳定文档身份及当前生效/待处理版本。"""

    id: str
    owner_id: str
    knowledge_base_id: str
    title: str
    active_version_id: str | None
    pending_version_id: str | None
    created_at: datetime
    updated_at: datetime
    deleted_time: datetime | None = None
    # 文档列表或详情按需装配的最新版本摘要；保持默认 None 以兼容既有位置参数构造。
    latest_version: DocumentVersion | None = None


@dataclass(frozen=True, slots=True)
class SourceReference:
    """已通过 owner 和版本范围检查的原件引用。"""

    filename: str
    media_type: str
    storage_key: str | None
    text: str | None


@dataclass(frozen=True, slots=True)
class SearchHit:
    """检索命中的带来源片段。"""

    chunk_id: str
    document_id: str
    version_id: str
    title: str
    text: str
    locators: tuple[ChunkLocator, ...]
    channels: tuple[str, ...]
    rank: float
    chunk_ordinal: int = 0
    chunk_ids: tuple[str, ...] = ()
    knowledge_base_id: str | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """混合检索结果和降级信息。"""

    hits: tuple[SearchHit, ...]
    channels: tuple[str, ...]
    degraded_reasons: tuple[str, ...] = ()
