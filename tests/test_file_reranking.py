from __future__ import annotations

import pytest

from athena.core.files.contracts import FileRetrievalCandidate
from athena.core.files.runtime import FileIntelligenceRuntime


class _FakeReranker:
    async def initialize(self) -> None:
        return None

    async def rerank(self, _query, candidates, limit):
        ranked = sorted(candidates, key=lambda item: len(item.content), reverse=True)
        for rank, item in enumerate(ranked[:limit], 1):
            item.rerank_score = float(len(item.content))
            item.rerank_rank = rank
        return ranked[:limit]


def _candidate(source_id: str, content: str, attachment_id: str = "doc"):
    return FileRetrievalCandidate(
        source_id=source_id,
        content=content,
        attachment_id=attachment_id,
    )


def test_file_fusion_preserves_typed_candidates_and_ranks():
    runtime = FileIntelligenceRuntime.__new__(FileIntelligenceRuntime)
    candidates = runtime._fuse_file_results(
        [_candidate("keyword", "keyword")],
        [_candidate("vector", "vector")],
        2,
    )

    assert all(isinstance(item, FileRetrievalCandidate) for item in candidates)
    assert {item.source_id for item in candidates} == {"keyword", "vector"}
    assert [item.fused_rank for item in candidates] == [1, 2]


@pytest.mark.asyncio
async def test_file_rerank_changes_order_and_records_scores():
    runtime = FileIntelligenceRuntime.__new__(FileIntelligenceRuntime)
    runtime._reranker = _FakeReranker()
    candidates = [
        _candidate("short", "x"),
        _candidate("long", "long content"),
    ]

    results = await runtime._rerank_file_results("query", candidates, 2)

    assert [item.source_id for item in results] == ["long", "short"]
    assert [item.rerank_rank for item in results] == [1, 2]
    assert results[0].rerank_score == 12.0


@pytest.mark.asyncio
async def test_file_rerank_failure_falls_back_to_fusion_order():
    class BrokenReranker:
        async def rerank(self, *_args, **_kwargs):
            raise RuntimeError("model unavailable")

    runtime = FileIntelligenceRuntime.__new__(FileIntelligenceRuntime)
    runtime._reranker = BrokenReranker()
    candidates = [_candidate("first", "a"), _candidate("second", "b")]
    candidates[0].fused_score = 0.5
    candidates[1].fused_score = 0.4

    results = await runtime._rerank_file_results("query", candidates, 2)

    assert [item.source_id for item in results] == ["first", "second"]
    assert [item.rerank_rank for item in results] == [1, 2]
    assert results[0].rerank_score == 0.5


def test_knowledge_diversity_limits_document_concentration():
    candidates = [
        _candidate("a1", "a1", "doc-a"),
        _candidate("a2", "a2", "doc-a"),
        _candidate("b1", "b1", "doc-b"),
    ]

    selected = FileIntelligenceRuntime._diversify_knowledge_results(
        candidates,
        3,
        max_per_document=1,
    )

    assert [item.source_id for item in selected] == ["a1", "b1", "a2"]


def test_candidate_serialization_keeps_legacy_api_fields():
    candidate = _candidate("chunk-1", "body")
    candidate.fused_score = 0.4
    payload = candidate.to_dict()

    assert payload["id"] == "chunk-1"
    assert payload["score"] == 0.4
    assert payload["content"] == "body"
