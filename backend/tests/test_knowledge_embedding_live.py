"""Explicit local model check; run with KNOWLEDGE_REAL_EMBEDDING=1."""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
from pathlib import Path
from time import perf_counter

import pytest

from infrastructure.knowledge.tokenizer import JiebaChineseTokenizer
from infrastructure.memory.models import LocalEmbedding

pytestmark = pytest.mark.skipif(
    not os.getenv("KNOWLEDGE_REAL_EMBEDDING"),
    reason="set KNOWLEDGE_REAL_EMBEDDING=1 to load the pinned local BGE model",
)

FIXTURE = Path(__file__).parent / "fixtures" / "knowledge_retrieval_eval.json"


def test_local_bge_dimension_and_chinese_retrieval() -> None:
    async def scenario() -> None:
        started = perf_counter()
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        documents = fixture["documents"]
        queries = fixture["queries"]
        model = fixture["model"]
        revision = fixture["revision"]
        embedding = LocalEmbedding(model, revision, "cpu")
        await embedding.start()
        try:
            corpus = [item["text"] for item in documents]
            query_texts = [item["query"] for item in queries]
            vectors = await embedding.embed([*corpus, *query_texts])
            assert len(vectors) == len(corpus) + len(query_texts)
            assert all(len(vector) == 1024 for vector in vectors)
            assert all(math.isfinite(value) for vector in vectors for value in vector)
            assert all(any(abs(value) > 1e-12 for value in vector) for vector in vectors)

            def cosine(left: list[float], right: list[float]) -> float:
                norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(
                    sum(value * value for value in right)
                )
                return sum(a * b for a, b in zip(left, right, strict=True)) / norm

            tokenizer = JiebaChineseTokenizer()
            reciprocal_rank = float(fixture["rrf_k"])
            hit_at_8 = 0
            identifier_queries = 0
            identifier_hits = 0
            evaluations: list[tuple[str, int, int, int]] = []
            for query_index, query in enumerate(queries):
                query_vector = vectors[len(corpus) + query_index]
                semantic = sorted(
                    range(len(corpus)),
                    key=lambda index: cosine(query_vector, vectors[index]),
                    reverse=True,
                )
                terms = set(tokenizer.tokenize(query["query"]))
                lexical_scores = [
                    sum(1 for term in terms if term in set(tokenizer.tokenize(text)))
                    for text in corpus
                ]
                lexical = sorted(range(len(corpus)), key=lambda i: lexical_scores[i], reverse=True)
                fused = {
                    index: 1 / (reciprocal_rank + semantic.index(index) + 1)
                    + 1 / (reciprocal_rank + lexical.index(index) + 1)
                    for index in range(len(corpus))
                }
                fused_order = sorted(fused, key=fused.__getitem__, reverse=True)
                expected = query["expected_doc_id"]
                target_index = next(i for i, doc in enumerate(documents) if doc["id"] == expected)
                hit = fused_order.index(target_index) < 8
                hit_at_8 += int(hit)
                evaluations.append(
                    (
                        expected,
                        fused_order.index(target_index) + 1,
                        semantic.index(target_index) + 1,
                        lexical.index(target_index) + 1,
                    )
                )
                if query["kind"] == "identifier":
                    identifier_queries += 1
                    identifier_hits += int(hit)
            assert hit_at_8 / len(queries) >= 0.9
            assert identifier_hits == identifier_queries
            print(
                f"local_hybrid_eval hit_at_8={hit_at_8}/{len(queries)} "
                f"identifiers={identifier_hits}/{identifier_queries} "
                f"model={model}@{revision} machine={platform.machine()} "
                f"seconds={perf_counter() - started:.2f}"
            )
        finally:
            await embedding.aclose()

    asyncio.run(scenario())
