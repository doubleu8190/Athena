#!/usr/bin/env python3
"""在固定中文语料上评估当前 pgvector embedding 配置。"""

from __future__ import annotations

import json
from pathlib import Path

from backend.bootstrap.config import get_settings
from backend.domain.retrieval.evaluation import RetrievalEvaluationCase, evaluate_rankings
from backend.infrastructure.integrations.llm.provider import ConfiguredEmbeddingProvider


async def main() -> None:
    fixture_path = Path(__file__).parents[1] / "tests" / "retrieval_eval" / "chinese_cases.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    settings = get_settings()
    provider = ConfiguredEmbeddingProvider(settings)
    corpus_ids = [item["id"] for item in fixture["corpus"]]
    corpus_vectors = await provider.embed_documents([item["content"] for item in fixture["corpus"]])
    rankings: dict[str, list[str]] = {}
    for item in fixture["cases"]:
        query_vector = await provider.embed_query(item["query"])
        ranked = sorted(
            zip(corpus_ids, corpus_vectors),
            key=lambda candidate: _cosine_distance(query_vector, candidate[1]),
        )
        rankings[item["query"]] = [source_id for source_id, _ in ranked]
    cases = [
        RetrievalEvaluationCase.from_ids(item["query"], item["relevant_source_ids"])
        for item in fixture["cases"]
    ]
    metrics = evaluate_rankings(cases, rankings, ks=(1, 3, 5))
    print(json.dumps({
        "embedding": {"provider": settings.embedding_provider, "model": settings.embedding_model},
        "recall_at_k": metrics.recall_at_k,
        "hit_rate_at_k": metrics.hit_rate_at_k,
        "mrr": metrics.mrr,
        "ndcg_at_k": metrics.ndcg_at_k,
    }, ensure_ascii=False, indent=2))


def _cosine_distance(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if not left_norm or not right_norm:
        return 1.0
    return 1.0 - dot / (left_norm * right_norm)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
