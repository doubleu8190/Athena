"""基于 ChromaDB 的文件向量索引适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from chromadb.api import ClientAPI

from athena.core.files.ports import FileVectorStore
from athena.models.json_models import FileLocator


class ChromaFileVectorStore(FileVectorStore):
    """把文件向量操作封装在 ChromaDB 适配器内。"""

    def __init__(
        self,
        path: str,
        collection_name: str = "athena_file_chunks",
    ) -> None:
        self._path = path
        self._collection_name = collection_name
        self._client: ClientAPI | None = None
        self._collection: Any | None = None

    @classmethod
    def with_client(
        cls,
        path: str,
        client: ClientAPI,
        collection_name: str = "athena_file_chunks",
    ) -> "ChromaFileVectorStore":
        store = cls(path, collection_name)
        store._client = client
        return store

    @property
    def ready(self) -> bool:
        return self._collection is not None

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
    def _index(self) -> Any:
        if self._collection is None:
            raise RuntimeError("ChromaFileVectorStore is not initialized")
        return self._collection

    async def replace_attachment(
        self,
        attachment_id: str,
        chunks: Sequence[Mapping[str, Any]],
    ) -> None:
        await asyncio.to_thread(self._index.delete, where={"attachment_id": attachment_id})
        for start in range(0, len(chunks), 100):
            batch = chunks[start : start + 100]
            if not batch:
                continue
            await asyncio.to_thread(
                self._index.add,
                ids=[str(item["id"]) for item in batch],
                documents=[str(item["content"]) for item in batch],
                metadatas=[dict(item["metadata"]) for item in batch],
            )

    async def delete_attachment(self, attachment_id: str) -> None:
        await asyncio.to_thread(self._index.delete, where={"attachment_id": attachment_id})

    async def query(
        self,
        query: str,
        limit: int,
        where: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": limit,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = dict(where)
        result = await asyncio.to_thread(self._index.query, **kwargs)
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        items: list[dict[str, Any]] = []
        for index, item_id in enumerate(ids):
            metadata = metadatas[index] or {} if index < len(metadatas) else {}
            locator_raw = metadata.get("locator_json")
            try:
                locator = FileLocator.model_validate_json(locator_raw).model_dump(
                    mode="json", exclude_none=True
                ) if isinstance(locator_raw, str) else {}
            except Exception:
                locator = {}
            items.append(
                {
                    "id": str(item_id),
                    "content": documents[index] if index < len(documents) else "",
                    "locator": locator,
                    "metadata": metadata,
                    "attachment_id": metadata.get("attachment_id", ""),
                    "document_version_id": metadata.get("document_version_id", ""),
                    "native_score": (
                        1.0 - float(distances[index])
                        if index < len(distances) and distances[index] is not None
                        else None
                    ),
                }
            )
            items[-1]["score"] = items[-1]["native_score"]
        return items
