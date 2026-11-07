"""知识库核心模型；不依赖 HTTP、ORM 或其他业务域。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


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


@dataclass(frozen=True, slots=True)
class SearchResult:
    """混合检索结果和降级信息。"""

    hits: tuple[SearchHit, ...]
    channels: tuple[str, ...]
    degraded_reasons: tuple[str, ...] = ()
