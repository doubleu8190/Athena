"""长期记忆领域实体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from .contracts import MemoryExecutionEvidence, MemoryFact


class MemoryStatus(StrEnum):
    """记忆版本的生命周期状态。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class MemoryValidity(StrEnum):
    """事实记忆的有效性状态。"""

    VALID = "valid"
    UNCERTAIN = "uncertain"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """与存储无关的记忆记录或检索结果。"""

    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float | None = None
    source: str | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    created_at: str = ""
    expires_at: str | None = None
    last_accessed_at: str | None = None
    access_count: int = 0
    last_observed_at: str | None = None
    valid_until: str | None = None
    validity_status: str = "valid"


@dataclass(frozen=True, slots=True)
class MemorySearchRequest:
    """记忆检索用例的输入。"""

    query: str
    limit: int = 5
    filters: dict[str, Any] | None = None
    record_access: bool = False
    terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验检索参数，避免无效分页值进入基础设施。"""
        if not self.query.strip():
            raise ValueError("memory search query must not be empty")
        if self.limit < 1:
            raise ValueError("memory search limit must be positive")
        if len(self.terms) > 16 or any(
            not term.strip() or len(term) > 80 for term in self.terms
        ):
            raise ValueError("memory search terms are invalid")


@dataclass(frozen=True, slots=True)
class MemoryEpisodeSearchRequest:
    """当前会话任务回顾的有界词法搜索请求。"""

    query: str
    terms: tuple[str, ...]
    limit: int = 8
    project_id: str | None = None

    def __post_init__(self) -> None:
        """拒绝空查询、空词项和无效候选上限。

        参数：query 是原始问题，terms 是应用层生成的词法匹配项。
        返回：无。
        异常：查询或限制不合法时抛出 ValueError。
        """
        if not self.query.strip() or len(self.query) > 500 or not self.terms:
            raise ValueError("memory_episode_search_query_invalid")
        if self.limit < 1:
            raise ValueError("memory episode search limit must be positive")
        if len(self.terms) > 16 or any(
            not term.strip() or len(term) > 80 for term in self.terms
        ):
            raise ValueError("memory_episode_search_terms_invalid")


@dataclass(frozen=True, slots=True)
class MemoryListRequest:
    """管理端记忆列表查询的输入。"""

    limit: int = 50
    offset: int = 0
    expired_only: bool = False
    session_id: str | None = None

    def __post_init__(self) -> None:
        """校验列表分页参数。"""
        if self.limit < 1:
            raise ValueError("memory list limit must be positive")
        if self.offset < 0:
            raise ValueError("memory list offset must not be negative")


@dataclass(frozen=True, slots=True)
class MemoryProjectSpace:
    """用户显式创建并绑定的可跨会话记忆空间。"""

    id: str
    name: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        """校验稳定 ID 和项目名称。"""
        if not self.id.strip():
            raise ValueError("memory_project_id_required")
        if not self.name.strip() or len(self.name.strip()) > 120:
            raise ValueError("memory_project_name_invalid")


@dataclass(frozen=True, slots=True)
class MemoryPage:
    """带总数的记忆列表结果。"""

    items: tuple[MemoryRecord, ...]
    total: int
    stats: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryRelationRecord:
    """记忆之间的替代或矛盾关系及对端摘要。"""

    id: str
    kind: str
    related_memory_id: str
    related_content: str
    related_status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class MemoryWriteCommand:
    """创建记忆时的正文、元数据和幂等键。"""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    operation_key: str = ""
    expected_memory_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """拒绝空正文，保持记忆记录具有实际内容。"""
        if not self.content.strip():
            raise ValueError("memory content must not be empty")


@dataclass(frozen=True, slots=True)
class CompletedTurn:
    """可异步处理的一轮用户与助手对话。"""

    turn_id: str
    session_id: str
    user_text: str
    assistant_text: str = ""
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    execution_evidence: MemoryExecutionEvidence | None = None
    project_id: str | None = None
    project_binding_frozen: bool = False


@dataclass(frozen=True, slots=True)
class ClaimedMemoryJob:
    """A memory-processing job atomically claimed by a worker."""

    turn: CompletedTurn
    attempt: int


@dataclass(frozen=True, slots=True)
class MemoryJobStatus:
    """User-safe status for one queued memory write/review job."""

    turn_id: str
    session_id: str
    project_id: str | None
    status: str
    attempt: int
    created_at: str
    updated_at: str
    available_at: str


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """事实提取器产生的候选记忆。"""

    content: str
    source_turn_id: str
    memory_type: str = "fact"
    category: str = "fact"
    confidence: float = 0.8
    session_id: str = ""
    fact: MemoryFact | None = None


class ResolutionAction(StrEnum):
    """候选记忆的持久化动作。"""

    CREATE = "create"
    REVISE = "revise"
    UPDATE = "revise"
    SUPERSEDE = "revise"
    IGNORE = "ignore"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class MemoryResolution:
    """候选记忆与已有版本之间的决策。"""

    action: ResolutionAction
    candidate: MemoryCandidate
    target_memory_id: str | None = None
    final_content: str | None = None
    relation: str | None = None
    reason_code: str | None = None
    expected_memory_ids: tuple[str, ...] = ()


__all__ = [
    "MemoryListRequest",
    "MemoryPage",
    "MemoryRelationRecord",
    "MemoryRecord",
    "MemorySearchRequest",
    "MemoryEpisodeSearchRequest",
    "MemoryProjectSpace",
    "MemoryStatus",
    "MemoryValidity",
    "MemoryWriteCommand",
    "CompletedTurn",
    "ClaimedMemoryJob",
    "MemoryCandidate",
    "MemoryResolution",
    "ResolutionAction",
]
