from __future__ import annotations

from collections.abc import Sequence

from domain.retrieval import RetrievalCandidate


class HybridRetrievalService:
    """Deterministic keyword/vector score fusion and bounded selection."""

    def fuse(self, keyword: Sequence[RetrievalCandidate], vector: Sequence[RetrievalCandidate], *, limit: int = 10) -> list[RetrievalCandidate]:
        scores: dict[str, float] = {}
        values: dict[str, RetrievalCandidate] = {}
        for rank, value in enumerate(keyword, 1):
            scores[value.source_id] = scores.get(value.source_id, 0.0) + 1 / (60 + rank)
            values[value.source_id] = value
        for rank, value in enumerate(vector, 1):
            scores[value.source_id] = scores.get(value.source_id, 0.0) + 1 / (60 + rank)
            values.setdefault(value.source_id, value)
        return [values[key] for key in sorted(scores, key=scores.get, reverse=True)[:max(0, limit)]]


__all__ = ["HybridRetrievalService"]
