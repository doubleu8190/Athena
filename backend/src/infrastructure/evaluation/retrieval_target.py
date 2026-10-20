"""Retrieval targets used by offline evaluation."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from domain.evaluation import EvalCase, EvaluationConfig, ExecutionArtifact


class StaticRankingTarget:
    """Deterministic target for CI and unit tests."""

    def __init__(self, rankings: Mapping[str, Sequence[str]]) -> None:
        self.rankings = {str(key): tuple(str(item) for item in value) for key, value in rankings.items()}

    async def execute(self, case: EvalCase, config: EvaluationConfig) -> ExecutionArtifact:
        query = str(case.input.get("query", case.input.get("message", "")))
        ranking = self.rankings.get(case.case_id, self.rankings.get(query, ()))
        return ExecutionArtifact(ranking=tuple(ranking), duration_ms=0.0)


class EmbeddingRetrievalTarget:
    """Run an embedding provider against a fixed dataset corpus."""

    def __init__(self, corpus: Mapping[str, str], provider: Any) -> None:
        self.corpus = dict(corpus)
        self.provider = provider
        self._ids = tuple(self.corpus)
        self._vectors: list[list[float]] | None = None

    async def _ensure_vectors(self) -> None:
        if self._vectors is None:
            self._vectors = await self.provider.embed_documents([self.corpus[source_id] for source_id in self._ids])

    async def execute(self, case: EvalCase, config: EvaluationConfig) -> ExecutionArtifact:
        await self._ensure_vectors()
        query = str(case.input.get("query", case.input.get("message", "")))
        started = time.perf_counter()
        vector = await self.provider.embed_query(query)
        ranked = sorted(
            zip(self._ids, self._vectors or ()),
            key=lambda item: _cosine_distance(vector, item[1]),
        )
        duration_ms = (time.perf_counter() - started) * 1000
        return ExecutionArtifact(ranking=tuple(source_id for source_id, _ in ranked), duration_ms=duration_ms)


def _cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if not left_norm or not right_norm:
        return 1.0
    return 1.0 - dot / (left_norm * right_norm)


__all__ = ["EmbeddingRetrievalTarget", "StaticRankingTarget"]
