"""检索运行及候选实体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    source_id: str
    content: str
    provider: str = "unknown"
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalRun:
    run_id: str
    query: str
    scope: str
    status: str = "running"
    candidates: tuple[RetrievalCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalRerankResult:
    """表示重排阶段返回的单个候选及其相关性顺序。"""

    candidate: RetrievalCandidate
    score: float
    rank: int

    def __post_init__(self) -> None:
        """拒绝非法的重排名次，避免错误顺序进入最终上下文。"""
        if type(self.rank) is not int or self.rank < 1:
            raise ValueError("retrieval_rerank_rank_invalid")


__all__ = ["RetrievalCandidate", "RetrievalRerankResult", "RetrievalRun"]
