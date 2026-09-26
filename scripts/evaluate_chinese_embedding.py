#!/usr/bin/env python3
"""在固定中文语料上评估当前 Chroma embedding 配置。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import chromadb

from athena.config.settings import get_settings
from athena.core.retrieval.evaluation import RetrievalEvaluationCase, evaluate_rankings
from athena.infrastructure.chroma.embedding import (
    collection_metadata,
    embedding_config_from_settings,
    get_or_create_collection,
)


async def main() -> None:
    fixture_path = Path(__file__).parents[1] / "tests" / "retrieval_eval" / "chinese_cases.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    settings = get_settings()
    config = embedding_config_from_settings(settings)
    client = chromadb.EphemeralClient()
    collection = get_or_create_collection(client, "athena_chinese_eval", config)
    collection.add(
        ids=[item["id"] for item in fixture["corpus"]],
        documents=[item["content"] for item in fixture["corpus"]],
        metadatas=[{"eval": True} for _ in fixture["corpus"]],
    )
    rankings: dict[str, list[str]] = {}
    for item in fixture["cases"]:
        result = collection.query(
            query_texts=[item["query"]],
            n_results=len(fixture["corpus"]),
            include=["distances"],
        )
        rankings[item["query"]] = [str(source_id) for source_id in result["ids"][0]]
    cases = [
        RetrievalEvaluationCase.from_ids(item["query"], item["relevant_source_ids"])
        for item in fixture["cases"]
    ]
    metrics = evaluate_rankings(cases, rankings, ks=(1, 3, 5))
    print(json.dumps({
        "embedding": json.loads(config.signature),
        "recall_at_k": metrics.recall_at_k,
        "hit_rate_at_k": metrics.hit_rate_at_k,
        "mrr": metrics.mrr,
        "ndcg_at_k": metrics.ndcg_at_k,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
