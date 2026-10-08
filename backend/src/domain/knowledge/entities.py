"""知识库领域实体。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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


__all__ = ["KnowledgeBase"]
