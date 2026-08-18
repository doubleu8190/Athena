"""ChromaDB adapter for long-term memory vectors."""

from __future__ import annotations

import asyncio
from typing import Any

from chromadb.api import ClientAPI


class ChromaMemoryStore:
    def __init__(
        self,
        path: str,
        collection_name: str = "athena_memory",
    ) -> None:
        self._path = path
        self._client: ClientAPI | None = None
        self._collection_name = collection_name
        self._collection: Any | None = None

    @classmethod
    def with_client(
        cls,
        path: str,
        client: ClientAPI,
        collection_name: str = "athena_memory",
    ) -> "ChromaMemoryStore":
        """Create a store with an explicitly supplied Chroma client."""
        store = cls(path=path, collection_name=collection_name)
        store._client = client
        return store

    async def initialize(self) -> None:
        if self._collection is not None:
            return
        if self._client is None:
            import chromadb

            self._client = chromadb.PersistentClient(path=self._path)
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def collection(self) -> Any:
        if self._collection is None:
            raise RuntimeError("ChromaMemoryStore is not initialized")
        return self._collection

    async def add(self, memory_id: str, content: str, metadata: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self.collection.add,
            ids=[memory_id],
            documents=[content],
            metadatas=[metadata],
        )

    async def query(
        self, query: str, limit: int, where: dict[str, Any] | None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": limit,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        return await asyncio.to_thread(self.collection.query, **kwargs)

    async def get(self, memory_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.collection.get, ids=[memory_id])

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"ids": [memory_id]}
        if content is not None:
            kwargs["documents"] = [content]
        if metadata is not None:
            kwargs["metadatas"] = [metadata]
        await asyncio.to_thread(self.collection.update, **kwargs)

    async def delete(self, memory_ids: list[str]) -> None:
        await asyncio.to_thread(self.collection.delete, ids=memory_ids)
