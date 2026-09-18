from __future__ import annotations

from typing import Any

import pytest

from athena.config.settings import Settings
from athena.core.memory.retrieval import HybridMemoryRetriever


class _Memory:
    def __init__(self, vector_by_query: dict[str, list[dict[str, Any]]], keyword: list[dict[str, Any]] | None = None) -> None:
        self.vector_by_query = vector_by_query
        self.keyword = keyword or []
        self.vector_queries: list[str] = []
        self.selected: list[str] = []

    async def search(self, query: str, n_results: int = 5, where: dict[str, Any] | None = None, record_access: bool = False) -> list[dict[str, Any]]:
        self.vector_queries.append(query)
        assert record_access is False
        return self.vector_by_query.get(query, [])[:n_results]

    async def keyword_search(self, query: str, n_results: int = 10, where: dict[str, Any] | None = None, record_access: bool = False) -> list[dict[str, Any]]:
        assert record_access is False
        return self.keyword[:n_results]

    def record_selected_access(self, memory_ids: list[str]) -> None:
        self.selected.extend(memory_ids)


class _Tokens:
    def count_text_tokens(self, text: str) -> int:
        return len(text)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        retrieval_candidate_k=30,
        retrieval_rerank_k=10,
        retrieval_context_k=5,
        memory_vector_min_score=0.70,
    )


@pytest.mark.asyncio
async def test_retrieval_keeps_original_vector_query_and_filters_low_native_score():
    memory = _Memory(
        {"original entity": [{"id": "weak", "content": "weak", "score": 0.69}]}
    )
    manager = HybridMemoryRetriever(memory, _settings())

    assert await manager.retrieve("original entity") == []
    assert memory.vector_queries == ["original entity"]


@pytest.mark.asyncio
async def test_retrieval_exact_keyword_hit_is_independent_of_vector_score():
    memory = _Memory(
        {"ERR_NOT_FOUND": [{"id": "weak", "content": "weak", "score": 0.1}]},
        keyword=[
            {
                "id": "error",
                "content": "API returned ERR_NOT_FOUND",
                "score": 0.01,
            }
        ],
    )
    manager = HybridMemoryRetriever(memory, _settings())

    results = await manager.retrieve("ERR_NOT_FOUND")

    assert [result.memory_id for result in results] == ["error"]
    assert results[0].exact_match is True
