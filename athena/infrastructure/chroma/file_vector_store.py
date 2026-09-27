"""基于 ChromaDB 的文件向量索引适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

import chromadb
from chromadb.api import ClientAPI

from athena.core.files.ports import FileVectorStore
from athena.core.files.contracts import FileRetrievalCandidate
from athena.core.llm.tokens import EmbeddingTokenCounter, conservative_text_token_count
from athena.infrastructure.chroma.embedding import (
    ChromaEmbeddingConfig,
    build_embedding_function,
    get_or_create_collection,
)
from athena.models.json_models import FileLocator


class ChromaFileVectorStore(FileVectorStore):
    """把文件向量操作封装在 ChromaDB 适配器内。"""

    def __init__(
        self,
        path: str,
        collection_name: str = "athena_file_chunks",
        embedding_config: ChromaEmbeddingConfig | None = None,
    ) -> None:
        self._path = path
        self._collection_name = collection_name
        self._client: ClientAPI = chromadb.PersistentClient(path=self._path)
        config = embedding_config or ChromaEmbeddingConfig(
            provider="sentence_transformers",
            model="paraphrase-multilingual-MiniLM-L12-v2",
        )
        embedding_function = build_embedding_function(config)
        embedding_model = getattr(embedding_function, "_model", None)
        tokenizer = getattr(embedding_model, "tokenizer", None)
        self.token_counter = EmbeddingTokenCounter(
            tokenizer if tokenizer is not None else conservative_text_token_count
        )
        self._collection: chromadb.Collection = get_or_create_collection(
            self._client,
            self._collection_name,
            config,
            embedding_function=embedding_function,
        )

    async def replace_attachment(
        self,
        attachment_id: str,
        chunks: Sequence[Mapping[str, Any]],
    ) -> None:
        await asyncio.to_thread(
            self._collection.delete, where={"attachment_id": attachment_id}
        )
        for start in range(0, len(chunks), 100):
            batch = chunks[start : start + 100]
            if not batch:
                continue
            await asyncio.to_thread(
                self._collection.add,
                ids=[str(item["id"]) for item in batch],
                documents=[str(item["content"]) for item in batch],
                metadatas=[dict(item["metadata"]) for item in batch],
            )

    async def delete_attachment(self, attachment_id: str) -> None:
        await asyncio.to_thread(
            self._collection.delete, where={"attachment_id": attachment_id}
        )

    async def query(
        self,
        query: str,
        limit: int,
        where: Mapping[str, Any] | None = None,
    ) -> list[FileRetrievalCandidate]:
        kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": limit,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = dict(where)
        result = await asyncio.to_thread(self._collection.query, **kwargs)
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        items: list[FileRetrievalCandidate] = []
        for item_id, document, raw_metadata, distance in zip(
            ids, documents, metadatas, distances
        ):
            metadata = raw_metadata or {}
            locator_raw = metadata.get("locator_json")
            try:
                locator = (
                    FileLocator.model_validate_json(locator_raw).model_dump(
                        mode="json", exclude_none=True
                    )
                    if isinstance(locator_raw, str)
                    else {}
                )
            except Exception:
                locator = {}
            items.append(
                FileRetrievalCandidate(
                    source_id=str(item_id),
                    content=str(document or ""),
                    locator=locator,
                    metadata=metadata,
                    attachment_id=str(metadata.get("attachment_id", "")),
                    document_version=metadata.get("document_version"),
                    vector_score=(
                        1.0 - float(distance)
                        if distance is not None
                        else None
                    ),
                )
            )
        return items
