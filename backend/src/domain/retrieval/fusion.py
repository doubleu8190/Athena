"""检索候选的领域排序规则。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .entities import RetrievalCandidate


class RetrievalRankFusion:
    """使用 reciprocal-rank fusion 合并多路候选排序。"""

    def fuse_ranked_candidates(
        self,
        keyword_candidates: Sequence[RetrievalCandidate],
        vector_candidates: Sequence[RetrievalCandidate],
        *,
        limit: int | None = 10,
    ) -> list[RetrievalCandidate]:
        scores: dict[str, float] = {}
        values: dict[str, RetrievalCandidate] = {}
        hits: dict[str, list[dict[str, Any]]] = {}
        for position, value in enumerate(keyword_candidates, 1):
            rank = int(value.metadata.get("channel_rank", position))
            scores[value.source_id] = scores.get(value.source_id, 0.0) + 1 / (60 + rank)
            values.setdefault(value.source_id, value)
            hits.setdefault(value.source_id, []).append(
                {
                    "channel": "keyword",
                    "rank": rank,
                    "score": value.metadata.get("channel_score", value.score),
                }
            )
        for position, value in enumerate(vector_candidates, 1):
            rank = int(value.metadata.get("channel_rank", position))
            scores[value.source_id] = scores.get(value.source_id, 0.0) + 1 / (60 + rank)
            values.setdefault(value.source_id, value)
            hits.setdefault(value.source_id, []).append(
                {
                    "channel": "vector",
                    "rank": rank,
                    "score": value.metadata.get("channel_score", value.score),
                }
            )
        ordered = sorted(scores, key=scores.get, reverse=True)
        selected = ordered if limit is None else ordered[: max(0, limit)]
        return [
            RetrievalCandidate(
                source_id=key,
                content=values[key].content,
                provider=values[key].provider,
                score=scores[key],
                metadata={
                    **values[key].metadata,
                    "rrf_score": scores[key],
                    "fused_rank": rank,
                    "hits": hits[key],
                },
            )
            for rank, key in enumerate(selected, 1)
        ]


__all__ = ["RetrievalRankFusion"]
