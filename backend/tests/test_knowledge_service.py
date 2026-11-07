"""Knowledge service contract tests for ownership, idempotency, and search scope."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import cast

import pytest

from app.knowledge.service import KnowledgeInputError, KnowledgeNotFoundError, KnowledgeService
from domain.knowledge.model import Document, KnowledgeBase, SearchResult
from domain.knowledge.ports import KnowledgeRepositoryPort


class MemoryKnowledgeRepository:
    def __init__(self) -> None:
        self.bases: dict[str, KnowledgeBase] = {}
        self.requests: dict[tuple[str, str], tuple[str, str, str]] = {}
        self.content_hashes: dict[tuple[str, str], str] = {}
        self.search_calls: list[
            tuple[str, list[str], str, tuple[str, ...], int, list[float] | None]
        ] = []

    async def create_base(self, item: KnowledgeBase) -> KnowledgeBase:
        self.bases[item.id] = item
        return item

    async def get_base(self, owner_id: str, base_id: str) -> KnowledgeBase | None:
        item = self.bases.get(base_id)
        return item if item and item.owner_id == owner_id and item.deleted_time is None else None

    async def list_bases(self, owner_id: str, *, offset: int, limit: int) -> list[KnowledgeBase]:
        items = [item for item in self.bases.values() if item.owner_id == owner_id]
        return items[offset : offset + limit]

    async def rename_base(
        self,
        owner_id: str,
        base_id: str,
        name: str,
        description: str | None,
        at: datetime,
    ) -> KnowledgeBase | None:
        item = await self.get_base(owner_id, base_id)
        if item is None:
            return None
        updated = KnowledgeBase(item.id, owner_id, name, description, item.created_at, at)
        self.bases[base_id] = updated
        return updated

    async def soft_delete_base(self, owner_id: str, base_id: str, at: datetime) -> bool:
        item = await self.get_base(owner_id, base_id)
        if item is None:
            return False
        self.bases[base_id] = KnowledgeBase(
            item.id,
            item.owner_id,
            item.name,
            item.description,
            item.created_at,
            item.updated_at,
            at,
        )
        return True

    async def create_text_document(
        self,
        *,
        owner_id: str,
        base_id: str,
        title: str,
        text: str,
        request_key: str,
        request_hash: str,
        content_hash: str,
        document_id: str,
        version_id: str,
        job_id: str,
        profile_id: str,
        at: datetime,
    ) -> tuple[Document, str]:
        del text, job_id, profile_id
        key = (owner_id, request_key)
        existing = self.requests.get(key)
        if existing:
            prior_hash, prior_document_id, prior_version_id = existing
            if prior_hash != request_hash:
                raise ValueError("idempotency_conflict")
            return Document(
                prior_document_id,
                owner_id,
                base_id,
                title,
                None,
                prior_version_id,
                at,
                at,
            ), prior_version_id
        self.requests[key] = (request_hash, document_id, version_id)
        self.content_hashes[key] = content_hash

        return Document(
            document_id,
            owner_id,
            base_id,
            title,
            None,
            version_id,
            at,
            at,
        ), version_id

    async def list_documents(
        self, owner_id: str, base_id: str, *, offset: int, limit: int
    ) -> list[Document]:
        del owner_id, base_id, offset, limit
        return []

    async def get_document(self, owner_id: str, base_id: str, document_id: str) -> Document | None:
        del owner_id, base_id, document_id
        return None

    async def search(
        self,
        owner_id: str,
        base_ids: list[str],
        query: str,
        tokens: tuple[str, ...],
        limit: int,
        vector: list[float] | None = None,
    ) -> SearchResult:
        self.search_calls.append((owner_id, base_ids, query, tokens, limit, vector))
        return SearchResult((), ("keyword", "semantic") if vector is not None else ("keyword",))


class Tokenizer:
    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(text.split())


class Embedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.25] * 1024 for _ in texts]


def _service(repository: MemoryKnowledgeRepository) -> KnowledgeService:
    return KnowledgeService(
        cast(KnowledgeRepositoryPort, repository),
        Tokenizer(),
        id_factory=lambda prefix: f"{prefix}_fixed",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_owner_scope_hides_another_users_base() -> None:
    repository = MemoryKnowledgeRepository()
    service = _service(repository)
    asyncio.run(service.create_base("owner_a", "private", None))

    with pytest.raises(KnowledgeNotFoundError):
        asyncio.run(service.get_base("owner_b", "kb_fixed"))
    with pytest.raises(KnowledgeNotFoundError):
        asyncio.run(service.create_text_document("owner_b", "kb_fixed", "title", "body", "key"))


def test_text_import_is_idempotent_and_conflicts_when_payload_changes() -> None:
    repository = MemoryKnowledgeRepository()
    service = _service(repository)
    asyncio.run(service.create_base("owner_a", "private", None))
    first, version_1 = asyncio.run(
        service.create_text_document("owner_a", "kb_fixed", "title", "body", "request-1")
    )
    replay, version_2 = asyncio.run(
        service.create_text_document("owner_a", "kb_fixed", "title", "body", "request-1")
    )

    assert replay.id == first.id
    assert version_2 == version_1
    request_hash = repository.requests[("owner_a", "request-1")][0]
    assert request_hash != hashlib.sha256(b"body").hexdigest()
    assert repository.content_hashes[("owner_a", "request-1")] == hashlib.sha256(
        b"body"
    ).hexdigest()
    with pytest.raises(ValueError, match="idempotency_conflict"):
        asyncio.run(
            service.create_text_document("owner_a", "kb_fixed", "title", "changed", "request-1")
        )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    asyncio.run(
        repository.create_base(KnowledgeBase("kb_other", "owner_a", "other", None, now, now))
    )
    with pytest.raises(ValueError, match="idempotency_conflict"):
        asyncio.run(
            service.create_text_document(
                "owner_a", "kb_other", "title", "body", "request-1"
            )
        )
    with pytest.raises(ValueError, match="idempotency_conflict"):
        asyncio.run(
            service.create_text_document(
                "owner_a", "kb_fixed", "different title", "body", "request-1"
            )
        )


def test_search_passes_owner_scope_and_same_tokens_and_vector_to_repository() -> None:
    repository = MemoryKnowledgeRepository()
    service = KnowledgeService(cast(KnowledgeRepositoryPort, repository), Tokenizer(), Embedding())
    result = asyncio.run(service.search("owner_a", ["kb_a"], "PostgreSQL 中文", limit=7))

    owner, bases, query, tokens, limit, vector = repository.search_calls[0]
    assert (owner, bases, query, tokens, limit) == (
        "owner_a",
        ["kb_a"],
        "PostgreSQL 中文",
        ("PostgreSQL", "中文"),
        7,
    )
    assert vector == [0.25] * 1024
    assert result.channels == ("keyword", "semantic")


@pytest.mark.parametrize("query,limit", [("", 8), ("good", 0), ("good", 21)])
def test_search_rejects_invalid_query_or_limit(query: str, limit: int) -> None:
    with pytest.raises(KnowledgeInputError):
        asyncio.run(
            _service(MemoryKnowledgeRepository()).search("owner_a", ["kb_a"], query, limit=limit)
        )
