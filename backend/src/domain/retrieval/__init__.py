from dataclasses import dataclass, field
from typing import Any

from .ports import GraphContextQueryPort


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


__all__ = ["GraphContextQueryPort", "RetrievalCandidate", "RetrievalRun"]
