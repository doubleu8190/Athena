"""知识检索降级、RRF邻接合并和上下文预算。"""

from __future__ import annotations

import asyncio
import math
from typing import cast

import pytest

from adapter.knowledge.repository import _retry_delay_seconds
from app.knowledge.service import KnowledgeSearchUnavailableError, KnowledgeService
from domain.knowledge.model import ChunkLocator, SearchHit
from domain.knowledge.ports import KnowledgeRepositoryPort


class _Tokenizer:
    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(text.split())


class _Counter:
    def count(self, text: str) -> int:
        return len(text)


class _Embedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024]


class _InvalidEmbedding(_Embedding):
    def __init__(self, value: float) -> None:
        self.value = value

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vector = [self.value] * 1024 if self.value == 0 else [0.1] * 1024
        if self.value != 0:
            vector[0] = self.value
        return [vector]


class _Repository:
    def __init__(self) -> None:
        self.keyword_hits: tuple[SearchHit, ...] = ()
        self.semantic_hits: tuple[SearchHit, ...] = ()
        self.keyword_error: Exception | None = None
        self.semantic_error: Exception | None = None
        self.keyword_calls = 0
        self.semantic_calls = 0
        self.incompatible_profile = False

    async def validate_search_scope(self, owner_id: str, base_ids: list[str]) -> None:
        assert owner_id == "owner"
        assert base_ids == ["base"]

    async def search_keyword(
        self, owner_id: str, base_ids: list[str], tokens: tuple[str, ...], limit: int
    ) -> tuple[SearchHit, ...]:
        self.keyword_calls += 1
        assert tokens == ("query",)
        if self.keyword_error:
            raise self.keyword_error
        return self.keyword_hits

    async def search_semantic(
        self,
        owner_id: str,
        base_ids: list[str],
        vector: list[float],
        limit: int,
        profile_id: str | None = None,
    ) -> tuple[SearchHit, ...]:
        self.semantic_calls += 1
        assert len(vector) == 1024
        if self.semantic_error:
            raise self.semantic_error
        return self.semantic_hits

    async def has_incompatible_profile(
        self, owner_id: str, base_ids: list[str], profile_id: str
    ) -> bool:
        return self.incompatible_profile


def _hit(chunk: str, ordinal: int, text: str, channel: str, rank: int) -> SearchHit:
    return SearchHit(
        chunk_id=chunk,
        document_id="doc",
        version_id="version",
        title="title",
        text=text,
        locators=(ChunkLocator(kind="text", start_line=ordinal + 1),),
        channels=(channel,),
        rank=float(rank),
        chunk_ordinal=ordinal,
        chunk_ids=(chunk,),
    )


def _service(repository: _Repository) -> KnowledgeService:
    return KnowledgeService(
        cast(KnowledgeRepositoryPort, repository),
        _Tokenizer(),
        _Embedding(),
        counter=_Counter(),
    )


def test_search_runs_channels_in_separate_calls_and_merges_adjacent_hits_with_budget() -> None:
    repository = _Repository()
    repository.keyword_hits = (
        _hit("chunk-1", 0, "abc", "keyword", 1),
        _hit("chunk-3", 3, "XYZ", "keyword", 2),
    )
    repository.semantic_hits = (_hit("chunk-2", 1, "def", "semantic", 1),)

    result = asyncio.run(_service(repository).search("owner", ["base"], "query", context_budget=8))

    assert repository.keyword_calls == repository.semantic_calls == 1
    assert result.channels == ("keyword", "semantic")
    assert result.hits[0].chunk_ids == ("chunk-1", "chunk-2")
    assert result.hits[0].text == "abc\ndef"
    assert sum(len(hit.text) for hit in result.hits) <= 8


def test_search_degrades_to_semantic_when_keyword_channel_fails() -> None:
    repository = _Repository()
    repository.keyword_error = RuntimeError("database unavailable")
    repository.semantic_hits = (_hit("chunk-1", 0, "proof", "semantic", 1),)

    result = asyncio.run(_service(repository).search("owner", ["base"], "query"))

    assert result.channels == ("semantic",)
    assert result.degraded_reasons == ("keyword_search_unavailable",)
    assert result.hits[0].chunk_id == "chunk-1"


def test_search_degrades_to_keyword_when_embedding_channel_fails() -> None:
    repository = _Repository()
    repository.semantic_error = RuntimeError("vector index unavailable")
    repository.keyword_hits = (_hit("chunk-1", 0, "proof", "keyword", 1),)

    result = asyncio.run(_service(repository).search("owner", ["base"], "query"))

    assert result.channels == ("keyword",)
    assert result.degraded_reasons == ("semantic_search_unavailable",)


def test_search_raises_unavailable_when_both_channels_fail() -> None:
    repository = _Repository()
    repository.keyword_error = RuntimeError("keyword failure")
    repository.semantic_error = RuntimeError("semantic failure")

    with pytest.raises(KnowledgeSearchUnavailableError):
        asyncio.run(_service(repository).search("owner", ["base"], "query"))


@pytest.mark.parametrize("invalid", [math.nan, math.inf, 0.0])
def test_invalid_semantic_vector_degrades_without_querying_database(invalid: float) -> None:
    repository = _Repository()
    service = KnowledgeService(
        cast(KnowledgeRepositoryPort, repository),
        _Tokenizer(),
        _InvalidEmbedding(invalid),
    )
    result = asyncio.run(service.search("owner", ["base"], "query"))
    assert repository.semantic_calls == 0
    assert result.channels == ("keyword",)
    assert "embedding_profile_mismatch" in result.degraded_reasons


def test_profile_incompatibility_is_reported_without_flagging_empty_scope() -> None:
    empty_repository = _Repository()
    empty_result = asyncio.run(_service(empty_repository).search("owner", ["base"], "query"))
    assert "semantic_profile_incompatible" not in empty_result.degraded_reasons

    incompatible_repository = _Repository()
    incompatible_repository.incompatible_profile = True
    result = asyncio.run(_service(incompatible_repository).search("owner", ["base"], "query"))
    assert result.channels == ("keyword",)
    assert "semantic_profile_incompatible" in result.degraded_reasons


@pytest.mark.parametrize(
    ("attempt", "base"),
    [(1, 5), (2, 30), (3, 120), (5, 120)],
)
def test_retry_delay_uses_exponential_backoff_with_jitter(attempt: int, base: int) -> None:
    assert _retry_delay_seconds(attempt, 0.8) == base * 0.8
    assert _retry_delay_seconds(attempt, 1.2) == base * 1.2
    assert base * 0.8 <= _retry_delay_seconds(attempt) <= base * 1.2
