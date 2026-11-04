"""图检索端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .entities import RetrievalCandidate, RetrievalRerankResult


class GraphSearchPort(Protocol):
    """按查询文本搜索图索引节点。"""

    async def search_nodes(
        self, query: str, *, limit: int = 12, after_id: str | None = None
    ) -> list[dict[str, object]]: ...


GraphContextQueryPort = GraphSearchPort


class RetrievalRerankPort(Protocol):
    """按查询文本对融合候选执行相关性重排。"""

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievalCandidate],
        *,
        limit: int,
    ) -> list[RetrievalRerankResult]: ...


__all__ = [
    "GraphContextQueryPort",
    "GraphSearchPort",
    "RetrievalRerankPort",
]
