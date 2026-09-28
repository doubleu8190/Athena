"""Storage port for graph indexing."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .contracts import (
    GraphChunk,
    GraphEntity,
    GraphEntityMention,
    GraphPathCandidate,
    GraphRelation,
)


class GraphStore(Protocol):
    async def replace_document_graph(
        self,
        *,
        knowledge_base_id: str,
        attachment_id: str,
        logical_document_id: str,
        document_version: int,
        filename: str,
        chunks: Sequence[GraphChunk],
        entities: Sequence[GraphEntity],
        mentions: Sequence[GraphEntityMention],
        relations: Sequence[GraphRelation],
    ) -> None:
        """Atomically replace the graph projection for one document version."""
        ...

    async def delete_document_graph(
        self, *, attachment_id: str, document_version: int | None = None
    ) -> None:
        """Remove a document projection and orphaned entities."""
        ...

    async def search_paths(
        self,
        *,
        query: str,
        knowledge_base_ids: Sequence[str] | None,
        max_hops: int,
        entity_limit: int,
        path_limit: int,
        min_confidence: float,
    ) -> list[GraphPathCandidate]:
        """Find bounded paths rooted at entities linked from the query."""
        ...
