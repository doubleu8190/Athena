"""Focused tests for GraphContextProvider's evidence and failure behavior."""

from __future__ import annotations

import pytest

from athena.core.graph.contracts import GraphPathCandidate
from athena.runtime.context.contracts import ContextPlan
from athena.runtime.context.providers.graph import GraphContextProvider
from athena.runtime.task_understanding.contracts import UserTaskSpec


class FakeGraphStore:
    async def search_paths(self, **_kwargs):
        return [
            GraphPathCandidate(
                path_id="graph:path:test",
                knowledge_base_id="kb-1",
                entity_keys=["a", "b"],
                entity_names=["A", "B"],
                relation_types=["depends_on"],
                evidence_chunk_ids=["chunk-1"],
                hop_count=1,
                score=0.9,
            )
        ]


class FakeFileRepository:
    async def get_chunks_by_ids(self, _ids, *, knowledge_base_ids=None):
        assert knowledge_base_ids == ["kb-1"]
        return [
            type(
                "Chunk",
                (),
                {"id": "chunk-1", "content": "A depends on B.", "locator": {"page": 2}},
            )()
        ]


def _task() -> UserTaskSpec:
    return UserTaskSpec(
        goal="A 和 B 有什么关系？",
        domain="research",
        mode="retrieve",
        confidence=1,
        context_requirements=["graph"],
    )


@pytest.mark.asyncio
async def test_graph_provider_returns_path_and_live_evidence():
    provider = GraphContextProvider(
        graph_store=FakeGraphStore(),
        file_repository=FakeFileRepository(),
        timeout_seconds=1,
    )

    result = await provider.acquire(
        session_id="session",
        task=_task(),
        plan=ContextPlan(
            providers=["graph"],
            graph_query="A B",
            knowledge_base_ids=["kb-1"],
        ),
    )

    assert result.status == "succeeded"
    assert len(result.items) == 1
    assert "A -> B" in result.items[0].content
    assert "A depends on B." in result.items[0].content
    assert result.items[0].metadata["evidence_chunk_ids"] == ["chunk-1"]


@pytest.mark.asyncio
async def test_graph_provider_degrades_on_store_failure():
    class BrokenStore:
        async def search_paths(self, **_kwargs):
            raise RuntimeError("neo4j unavailable")

    provider = GraphContextProvider(
        graph_store=BrokenStore(),
        file_repository=FakeFileRepository(),
        timeout_seconds=1,
    )
    result = await provider.acquire(
        session_id="session",
        task=_task(),
        plan=ContextPlan(providers=["graph"], graph_query="A B"),
    )

    assert result.status == "failed"
    assert result.items == []
    assert "neo4j unavailable" in (result.error_message or "")

