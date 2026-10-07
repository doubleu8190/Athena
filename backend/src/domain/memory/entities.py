"""长期记忆领域实体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


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


@dataclass(frozen=True, slots=True)
class MemorySearchRequest:
    """记忆检索用例的输入。"""

    query: str
    limit: int = 5
    filters: dict[str, Any] | None = None
    record_access: bool = False

    def __post_init__(self) -> None:
        """校验检索参数，避免无效分页值进入基础设施。"""
        if not self.query.strip():
            raise ValueError("memory search query must not be empty")
        if self.limit < 1:
            raise ValueError("memory search limit must be positive")


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
class MemoryPage:
    """带总数的记忆列表结果。"""

    items: tuple[MemoryRecord, ...]
    total: int
    stats: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryWriteCommand:
    """创建记忆时的正文、元数据和幂等键。"""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    operation_key: str = ""

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


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """事实提取器产生的候选记忆。"""

    content: str
    source_turn_id: str
    memory_type: str = "fact"
    category: str = "fact"
    confidence: float = 0.8


class ResolutionAction(StrEnum):
    """候选记忆的持久化动作。"""

    CREATE = "create"
    UPDATE = "update"
    SUPERSEDE = "supersede"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class MemoryResolution:
    """候选记忆与已有版本之间的决策。"""

    action: ResolutionAction
    candidate: MemoryCandidate
    target_memory_id: str | None = None
    final_content: str | None = None


__all__ = [
    "MemoryListRequest",
    "MemoryPage",
    "MemoryRecord",
    "MemorySearchRequest",
    "MemoryStatus",
    "MemoryValidity",
    "MemoryWriteCommand",
    "CompletedTurn",
    "MemoryCandidate",
    "MemoryResolution",
    "ResolutionAction",
]
