"""检索候选质量的离线评估。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class RetrievalEvaluationCase:
    """一个查询及其期望召回的不可变证据 ID。"""

    query: str
    relevant_source_ids: frozenset[str]

    @classmethod
    def from_ids(cls, query: str, source_ids: Sequence[str]) -> "RetrievalEvaluationCase":
        """从有序或无序的证据 ID 创建评估样本。"""
        return cls(query=query, relevant_source_ids=frozenset(source_ids))


@dataclass(frozen=True)
class RetrievalMetrics:
    """一组查询上的候选召回指标。"""

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
    """计算候选召回质量。

    参数：
        cases: 评估查询及其相关证据 ID；证据 ID 应指向不可变 revision 或 chunk。
        rankings_by_query: 每个查询对应的候选排序，顺序即 rank 顺序。
        ks: 要计算的截断位置，必须是正整数。

    返回值：
        ``RetrievalMetrics``，其中 Recall@K 是主要指标。

    异常：
        ``ValueError``：``ks`` 为空或包含非正整数。
    """
    normalized_ks = tuple(dict.fromkeys(int(k) for k in ks))
    if not normalized_ks or any(k <= 0 for k in normalized_ks):
        raise ValueError("ks must contain positive integers")
    if not cases:
        zero = {k: 0.0 for k in normalized_ks}
        return RetrievalMetrics(0, zero, dict(zero), 0.0, dict(zero))

    recall_totals = {k: 0.0 for k in normalized_ks}
    hit_totals = {k: 0.0 for k in normalized_ks}
    ndcg_totals = {k: 0.0 for k in normalized_ks}
    reciprocal_rank_total = 0.0

    for case in cases:
        # 候选融合通常已经去重；评估器再次去重，避免重复候选虚增 Recall 或 nDCG。
        ranking = list(dict.fromkeys(rankings_by_query.get(case.query, ())))
        relevant = case.relevant_source_ids
        first_rank: int | None = None
        for index, source_id in enumerate(ranking, 1):
            if source_id in relevant:
                first_rank = index
                break
        if first_rank is not None:
            reciprocal_rank_total += 1.0 / first_rank

        for k in normalized_ks:
            top_ids = ranking[:k]
            hits = len(relevant.intersection(top_ids))
            recall_totals[k] += hits / len(relevant) if relevant else 0.0
            hit_totals[k] += 1.0 if hits else 0.0
            dcg = sum(
                1.0 / math.log2(index + 2)
                for index, source_id in enumerate(top_ids)
                if source_id in relevant
            )
            ideal_hits = min(k, len(relevant))
            idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_hits))
            ndcg_totals[k] += dcg / idcg if idcg else 0.0

    count = len(cases)
    return RetrievalMetrics(
        query_count=count,
        recall_at_k={k: value / count for k, value in recall_totals.items()},
        hit_rate_at_k={k: value / count for k, value in hit_totals.items()},
        mrr=reciprocal_rank_total / count,
        ndcg_at_k={k: value / count for k, value in ndcg_totals.items()},
    )
