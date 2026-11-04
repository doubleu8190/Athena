"""文件上下文通道降级与重排行为测试。"""

from __future__ import annotations

import pytest

from application.files.retrieval_service import FileContextProvider
from application.runs.context import ContextPlan
from domain.files import FileChunk, FileLocator, FileMetadata
from domain.retrieval import RetrievalRerankResult


def _chunk(
    chunk_id: str, content: str, title: str = "report.txt"
) -> FileChunk:
    return FileChunk(
        id=chunk_id,
        attachment_id="attachment-1",
        ordinal=0,
        content=content,
        token_count=1,
        locator=FileLocator({"page": 1}),
        metadata=FileMetadata({"title": title}),
    )


class _Chunks:
    def __init__(self, chunks: list[FileChunk]) -> None:
        self._chunks = chunks

    async def list_by_attachment(self, attachment_id: str):
        assert attachment_id == "attachment-1"
        return self._chunks


@pytest.mark.asyncio
async def test_file_vector_failure_keeps_keyword_results() -> None:
    """向量查询失败时保留同范围关键词候选并返回 partial。"""

    class FailingVector:
        async def query(self, text: str, limit: int = 10, **kwargs: object):
            raise RuntimeError("vector unavailable")

    provider = FileContextProvider(
        _Chunks([_chunk("chunk-1", "报销 发票 审批单")]),
        FailingVector(),
    )
    result = await provider.retrieve_context(
        session_id="session",
        run_id="run",
        plan=ContextPlan(
            providers=("file",),
            file_query="报销 发票",
            file_ids=("attachment-1",),
            max_items=5,
        ),
    )

    assert result.status == "partial"
    assert [item.content for item in result.items] == ["报销 发票 审批单"]
    vector_channel = next(
        channel for channel in result.channels if channel.channel == "vector"
    )
    assert vector_channel.status == "failed"
    assert vector_channel.error_message == "vector unavailable"


@pytest.mark.asyncio
async def test_file_reranker_changes_order_and_records_scores() -> None:
    """文件候选按重排结果进入上下文，并保留重排字段。"""

    class ReverseReranker:
        async def rerank(self, query, candidates, *, limit):
            ordered = list(reversed(candidates))[:limit]
            return [
                RetrievalRerankResult(candidate, float(limit - rank), rank)
                for rank, candidate in enumerate(ordered, 1)
            ]

    provider = FileContextProvider(
        _Chunks(
            [
                _chunk("chunk-a", "report alpha"),
                _chunk("chunk-b", "report beta"),
            ]
        ),
        reranker=ReverseReranker(),
        candidate_limit=10,
        rerank_result_limit=5,
    )
    result = await provider.retrieve_context(
        session_id="session",
        run_id="run",
        plan=ContextPlan(
            providers=("file",),
            file_query="report",
            file_ids=("attachment-1",),
            max_items=5,
            rerank_limit=2,
        ),
    )

    assert result.status == "succeeded"
    assert [item.source_id for item in result.items] == ["chunk-b", "chunk-a"]
    assert result.items[0].metadata["rerank_rank"] == 1
    assert any(
        candidate.source_id == "chunk-b"
        and candidate.rerank_rank == 1
        for candidate in result.candidates
    )


@pytest.mark.asyncio
async def test_file_reranker_failure_falls_back_to_fusion_order() -> None:
    """文件重排失败时保留融合顺序，并记录可诊断的通道错误。"""

    class FailingReranker:
        async def rerank(self, query, candidates, *, limit):
            raise RuntimeError("reranker unavailable")

    provider = FileContextProvider(
        _Chunks(
            [
                _chunk("chunk-a", "report alpha"),
                _chunk("chunk-b", "report beta"),
            ]
        ),
        reranker=FailingReranker(),
        candidate_limit=10,
        rerank_result_limit=5,
    )
    result = await provider.retrieve_context(
        session_id="session",
        run_id="run",
        plan=ContextPlan(
            providers=("file",),
            file_query="report",
            file_ids=("attachment-1",),
            max_items=5,
            rerank_limit=2,
        ),
    )

    assert result.status == "partial"
    assert [item.source_id for item in result.items] == ["chunk-a", "chunk-b"]
    rerank_channel = next(
        channel for channel in result.channels if channel.channel == "rerank"
    )
    assert rerank_channel.status == "failed"
    assert rerank_channel.error_message == "reranker unavailable"


@pytest.mark.asyncio
async def test_file_rerank_output_limit_caps_candidates() -> None:
    """重排输出上限约束结果，但被截断候选仍保留在追踪中。"""

    class SameOrderReranker:
        async def rerank(self, query, candidates, *, limit):
            return [
                RetrievalRerankResult(candidate, 1.0, rank)
                for rank, candidate in enumerate(candidates, 1)
            ]

    provider = FileContextProvider(
        _Chunks(
            [
                _chunk("chunk-a", "report alpha"),
                _chunk("chunk-b", "report beta"),
                _chunk("chunk-c", "report gamma"),
            ]
        ),
        reranker=SameOrderReranker(),
        candidate_limit=10,
        rerank_result_limit=2,
    )
    result = await provider.retrieve_context(
        session_id="session",
        run_id="run",
        plan=ContextPlan(
            providers=("file",),
            file_query="report",
            file_ids=("attachment-1",),
            max_items=5,
            rerank_limit=3,
        ),
    )

    assert result.status == "succeeded"
    assert result.candidate_count == 3
    assert len(result.items) == 2
    rejected = next(
        candidate
        for candidate in result.candidates
        if candidate.source_id == "chunk-c"
    )
    assert rejected.selected_for_result is False
    assert rejected.filter_reason == "provider_top_k"


@pytest.mark.asyncio
async def test_file_rerank_candidate_limit_keeps_tail_in_trace() -> None:
    """超出 rerank 输入上限的候选必须保留在分析轨迹中。"""

    class SameOrderReranker:
        async def rerank(self, query, candidates, *, limit):
            return [
                RetrievalRerankResult(candidate, 1.0, rank)
                for rank, candidate in enumerate(candidates, 1)
            ]

    result = await FileContextProvider(
        _Chunks(
            [
                _chunk("chunk-a", "report alpha"),
                _chunk("chunk-b", "report beta"),
                _chunk("chunk-c", "report gamma"),
            ]
        ),
        reranker=SameOrderReranker(),
        candidate_limit=10,
        rerank_result_limit=5,
    ).retrieve_context(
        session_id="session",
        run_id="run",
        plan=ContextPlan(
            providers=("file",),
            file_query="report",
            file_ids=("attachment-1",),
            max_items=5,
            rerank_limit=2,
        ),
    )

    rejected = next(
        candidate
        for candidate in result.candidates
        if candidate.source_id == "chunk-c"
    )
    assert result.candidate_count == 3
    assert rejected.selected_for_result is False
    assert rejected.filter_reason == "rerank_candidate_limit"
    assert rejected.rerank_score is None


@pytest.mark.asyncio
async def test_file_rerank_admission_rejects_low_vector_without_keyword_hit() -> None:
    """纯向量低分候选不得进入上下文，但仍保留完整追踪。"""

    class Vector:
        async def query(self, text: str, limit: int = 10, **kwargs: object):
            return [
                {
                    "chunk_id": "vector-only",
                    "content": "unrelated appendix",
                    "score": 0.95,
                }
            ]

    class LowScoreReranker:
        async def rerank(self, query, candidates, *, limit):
            return [
                RetrievalRerankResult(candidate, 0.1, rank)
                for rank, candidate in enumerate(candidates, 1)
            ]

    result = await FileContextProvider(
        _Chunks([_chunk("vector-only", "unrelated appendix")]),
        Vector(),
        reranker=LowScoreReranker(),
        rerank_min_score=0.5,
        candidate_limit=10,
        rerank_result_limit=5,
    ).retrieve_context(
        session_id="session",
        run_id="run",
        plan=ContextPlan(
            providers=("file",),
            file_query="report",
            file_ids=("attachment-1",),
            max_items=5,
            rerank_limit=2,
        ),
    )

    assert result.status == "succeeded"
    assert result.result_count == 0
    assert result.items == ()
    rejected = next(
        candidate
        for candidate in result.candidates
        if candidate.source_id == "vector-only"
    )
    assert rejected.selected_for_result is False
    assert rejected.filter_reason == "below_rerank_min_score"
    assert rejected.selection_stage == "provider"
    assert rejected.rerank_score == 0.1
    assert rejected.rerank_rank == 1
    assert rejected.hits[0].channel == "vector"


@pytest.mark.asyncio
async def test_file_rerank_admission_exempts_positive_keyword_hit() -> None:
    """正关键词命中即使重排分低也豁免准入门槛。"""

    class Vector:
        async def query(self, text: str, limit: int = 10, **kwargs: object):
            return [
                {
                    "chunk_id": "vector-only",
                    "content": "unrelated appendix",
                    "score": 0.95,
                }
            ]

    class LowScoreReranker:
        async def rerank(self, query, candidates, *, limit):
            ordered = sorted(
                candidates,
                key=lambda candidate: candidate.source_id == "keyword-hit",
            )
            return [
                RetrievalRerankResult(candidate, 0.1, rank)
                for rank, candidate in enumerate(ordered, 1)
            ]

    result = await FileContextProvider(
        _Chunks(
            [
                _chunk("keyword-hit", "report overview"),
                _chunk("vector-only", "unrelated appendix"),
            ]
        ),
        Vector(),
        reranker=LowScoreReranker(),
        rerank_min_score=0.5,
        candidate_limit=10,
        rerank_result_limit=5,
    ).retrieve_context(
        session_id="session",
        run_id="run",
        plan=ContextPlan(
            providers=("file",),
            file_query="report",
            file_ids=("attachment-1",),
            max_items=5,
            rerank_limit=2,
        ),
    )

    assert [item.source_id for item in result.items] == ["keyword-hit"]
    rejected = next(
        candidate
        for candidate in result.candidates
        if candidate.source_id == "vector-only"
    )
    assert rejected.filter_reason == "below_rerank_min_score"
