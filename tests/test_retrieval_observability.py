"""Regression tests for retrieval traces and privacy defaults."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import pytest
from langchain_core.messages import AIMessage

from athena.config.settings import Settings
from athena.core.memory.retrieval import HybridRetrievalManager
from athena.core.retrieval.trace import RetrievalTrace


class _LLM:
    async def ainvoke(self, _messages: list[Any]) -> AIMessage:
        return AIMessage(content="expanded")


class _Memory:
    async def search(
        self, query: str, n_results: int = 5, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "memory-1",
                "content": "sensitive document body",
                "metadata": {"created_at": datetime.now().isoformat()},
                "score": 1.0,
            }
        ]

    async def keyword_search(
        self, query: str, n_results: int = 10, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        raise RuntimeError("fts unavailable")

    def pending_access_stats(self, _ids: Iterable[str]) -> dict[str, tuple[int, str]]:
        return {}


def test_trace_log_fields_omit_content_and_raw_query_by_default():
    trace = RetrievalTrace(
        source_scope="memory",
        query="private query",
        query_hash_salt="test-salt",
    )
    trace.add_candidate(
        item_id="memory-1", route="vector", rank=1, native_score=0.9
    )
    fields = trace.as_log_fields()

    assert "query" not in fields
    assert "private query" not in str(fields)
    assert "document body" not in str(fields)
    assert fields["query_hash"] != "private query"
    assert fields["score_components"][0]["item_id"] == "memory-1"


@pytest.mark.asyncio
async def test_memory_trace_records_candidates_selection_and_fallback():
    traces: list[RetrievalTrace] = []
    manager = HybridRetrievalManager(
        _LLM(),
        _Memory(),
        Settings(_env_file=None, debug=False, retrieval_trace_enabled=True),
        trace_sink=traces.append,
    )

    results = await manager.retrieve("private query")

    assert [result.chunk_id for result in results] == ["memory-1"]
    assert len(traces) == 1
    trace = traces[0]
    assert trace.selected_ids == ["memory-1"]
    assert trace.fallback_reasons == ["keyword_search_failed"]
    assert {candidate.route for candidate in trace.candidates} == {"vector"}
    assert any(stage.stage == "fusion" for stage in trace.stages)
    assert any(stage.error_code == "keyword_search_failed" for stage in trace.stages)


@pytest.mark.asyncio
async def test_trace_is_not_created_when_disabled():
    traces: list[RetrievalTrace] = []
    manager = HybridRetrievalManager(
        _LLM(),
        _Memory(),
        Settings(_env_file=None, debug=False, retrieval_trace_enabled=False),
        trace_sink=traces.append,
    )

    await manager.retrieve("query")

    assert traces == []
