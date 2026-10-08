"""检索候选的领域排序规则。"""

from __future__ import annotations

from collections.abc import Sequence

from .entities import RetrievalCandidate


class RetrievalRankFusion:
    """使用 reciprocal-rank fusion 合并多路候选排序。"""

    def fuse_ranked_candidates(
        self,
        keyword_candidates: Sequence[RetrievalCandidate],
        vector_candidates: Sequence[RetrievalCandidate],
        *,
        limit: int = 10,
    ) -> list[RetrievalCandidate]:
        scores: dict[str, float] = {}
        values: dict[str, RetrievalCandidate] = {}
        for rank, value in enumerate(keyword_candidates, 1):
            scores[value.source_id] = scores.get(value.source_id, 0.0) + 1 / (60 + rank)
            values[value.source_id] = value
        for rank, value in enumerate(vector_candidates, 1):
            scores[value.source_id] = scores.get(value.source_id, 0.0) + 1 / (60 + rank)
            values.setdefault(value.source_id, value)
        selected = sorted(scores, key=scores.get, reverse=True)[: max(0, limit)]
        return [
            RetrievalCandidate(
                source_id=key,
                content=values[key].content,
                provider=values[key].provider,
                score=scores[key],
                metadata={**values[key].metadata, "rrf_score": scores[key]},
            )
            for key in selected
        ]


__all__ = ["RetrievalRankFusion"]
