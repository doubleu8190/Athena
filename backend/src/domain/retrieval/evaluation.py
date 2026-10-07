"""Offline retrieval quality metrics owned by the target domain."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalEvaluationCase:
    query: str
    relevant_source_ids: frozenset[str]

    @classmethod
    def from_ids(cls, query: str, source_ids: Sequence[str]) -> "RetrievalEvaluationCase":
        return cls(query, frozenset(source_ids))


@dataclass(frozen=True)
class RetrievalMetrics:
    query_count: int
    recall_at_k: dict[int, float]
    hit_rate_at_k: dict[int, float]
    mrr: float
    ndcg_at_k: dict[int, float]


def evaluate_rankings(
    cases: Sequence[RetrievalEvaluationCase],
    rankings_by_query: Mapping[str, Sequence[str]],
    *,
    ks: Sequence[int] = (1, 3, 5, 10),
) -> RetrievalMetrics:
    normalized_ks = tuple(dict.fromkeys(int(k) for k in ks))
    if not normalized_ks or any(k <= 0 for k in normalized_ks):
        raise ValueError("ks must contain positive integers")
    zero = {k: 0.0 for k in normalized_ks}
    if not cases:
        return RetrievalMetrics(0, zero, dict(zero), 0.0, dict(zero))

    recall = {k: 0.0 for k in normalized_ks}
    hits = {k: 0.0 for k in normalized_ks}
    ndcg = {k: 0.0 for k in normalized_ks}
    reciprocal = 0.0
    for case in cases:
        ranking = list(dict.fromkeys(rankings_by_query.get(case.query, ())))
        first = next((i for i, value in enumerate(ranking, 1) if value in case.relevant_source_ids), None)
        if first is not None:
            reciprocal += 1 / first
        for k in normalized_ks:
            top = ranking[:k]
            found = len(case.relevant_source_ids.intersection(top))
            recall[k] += found / len(case.relevant_source_ids) if case.relevant_source_ids else 0.0
            hits[k] += 1.0 if found else 0.0
            dcg = sum(1 / math.log2(index + 2) for index, value in enumerate(top) if value in case.relevant_source_ids)
            ideal = min(k, len(case.relevant_source_ids))
            idcg = sum(1 / math.log2(index + 2) for index in range(ideal))
            ndcg[k] += dcg / idcg if idcg else 0.0
    count = len(cases)
    return RetrievalMetrics(
        count,
        {k: value / count for k, value in recall.items()},
        {k: value / count for k, value in hits.items()},
        reciprocal / count,
        {k: value / count for k, value in ndcg.items()},
    )


__all__ = ["RetrievalEvaluationCase", "RetrievalMetrics", "evaluate_rankings"]
