"""RAG Core — embedding provider and Chroma vector store management.

Layer 1 of the RAG architecture. Provides:
- EmbeddingProvider: local sentence-transformers (all-MiniLM-L6-v2)
- RAGManager: Chroma collection CRUD for user memories

All embedding computation runs locally — no user data is sent to external APIs.

Used directly by the API layer and Celery tasks, and wrapped by
the MCP stdio server (Layer 3).
"""

from __future__ import annotations

from typing import Any

from athena.config import Config
from athena.logging_config import get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

LOCAL_MODEL_NAME = "all-MiniLM-L6-v2"
CHROMA_COLLECTION_NAME = "athena_memories"


class EmbeddingProvider:
    """Compute text embeddings with local sentence-transformers model.

    All computation runs locally — no user data is sent to external APIs.
    The model is loaded lazily on first use.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._local_model = None  # Lazy-loaded

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embedding vectors for one or more texts."""
        if not texts:
            return []

        if self._local_model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("embedding_loading_local_model", model=LOCAL_MODEL_NAME)
            self._local_model = SentenceTransformer(LOCAL_MODEL_NAME)

        embeddings = self._local_model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()


class RAGManager:
    """Manages the Chroma vector store for user memories.

    Collection: "athena_memories"
    Metadata: {"user_id", "memory_id", "key"}
    IDs: memory_id (used as Chroma document ID = vector_id)
    """

    def __init__(self, config: Config, embedding_provider: EmbeddingProvider) -> None:
        self._config = config
        self._embedding = embedding_provider
        self._client = None
        self._collection = None

    def _ensure_collection(self) -> None:
        """Lazy-init Chroma client and collection on first use."""
        if self._collection is not None:
            return

        import chromadb
        import os


        persist_dir = os.path.join(
            os.path.dirname(self._config.chroma_persist_dir) if self._config.chroma_persist_dir else "./data",
            "chroma",
        )
        os.makedirs(persist_dir, exist_ok=True)

        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection(
            name=CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            "rag_collection_ready",
            name=CHROMA_COLLECTION_NAME,
            persist_dir=persist_dir,
        )

    async def semantic_search(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search memories by semantic similarity for a given user.

        Args:
            user_id: Filter results to this user.
            query: Natural language query to embed and search.
            top_k: Maximum number of results to return.
            where: Additional ChromaDB metadata filter (merged with user_id).
                   Example: {"type": "atomic_fact"} to search only facts.

        Returns list of dicts: {memory_id, score, metadata, text}
        """
        self._ensure_collection()

        # Embed the query
        [query_embedding] = await self._embedding.embed([query])

        # Build where clause — always filter by user_id, merge extra conditions
        where_clause: dict[str, Any] = {"user_id": user_id}
        if where:
            where_clause = {"$and": [where_clause, where]}

        # Search Chroma
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where_clause,
            include=["metadatas", "distances", "documents"],
        )

        # Normalize Chroma response format
        items: list[dict[str, Any]] = []
        if results["ids"] and results["ids"][0]:
            for i, mem_id in enumerate(results["ids"][0]):
                items.append({
                    "memory_id": mem_id,
                    "score": 1.0 - results["distances"][0][i],  # cosine distance → similarity
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "text": results["documents"][0][i] if results["documents"] else "",
                })

        logger.debug(
            "rag_semantic_search",
            user_id=user_id,
            query_preview=query[:80],
            result_count=len(items),
        )
        return items

    async def upsert_vector(
        self,
        memory_id: str,
        user_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Embed text and upsert into the Chroma collection.

        Returns the vector_id (same as memory_id for Chroma).
        """
        self._ensure_collection()

        # Embed the text
        [embedding] = await self._embedding.embed([text])

        # Build metadata
        from datetime import datetime, timezone

        meta = {"user_id": user_id, "memory_id": memory_id}
        if metadata:
            meta.update(metadata)
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Upsert into Chroma
        self._collection.upsert(
            ids=[memory_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[meta],
        )

        logger.debug(
            "rag_upsert_vector",
            memory_id=memory_id,
            user_id=user_id,
            text_preview=text[:80],
        )
        return memory_id

    async def get_vector(self, memory_id: str) -> dict[str, Any] | None:
        """Fetch a single vector by memory_id.

        Returns {memory_id, text, metadata} or None if not found.
        """
        self._ensure_collection()

        result = self._collection.get(
            ids=[memory_id],
            include=["documents", "metadatas"],
        )

        if not result["ids"]:
            return None

        return {
            "memory_id": result["ids"][0],
            "text": result["documents"][0] if result["documents"] else "",
            "metadata": result["metadatas"][0] if result["metadatas"] else {},
        }

    async def list_vectors(
        self,
        user_id: str,
        key_prefix: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List vectors for a user, optionally filtered by key prefix.

        Returns list of {memory_id, text, metadata}.
        """
        self._ensure_collection()

        result = self._collection.get(
            where={"user_id": user_id},
            include=["documents", "metadatas"],
        )

        items: list[dict[str, Any]] = []
        for i, mem_id in enumerate(result["ids"]):
            meta = result["metadatas"][i] if result["metadatas"] else {}
            key = meta.get("key", "")

            if key_prefix and not key.startswith(key_prefix):
                continue

            items.append({
                "memory_id": mem_id,
                "text": result["documents"][i] if result["documents"] else "",
                "metadata": meta,
            })

        # Sort by updated_at descending (newest first), fall back to insertion order
        items.sort(key=lambda x: x["metadata"].get("updated_at", ""), reverse=True)

        return items[:limit]

    def delete_vector(self, vector_id: str) -> None:
        """Remove a vector from the Chroma collection."""
        self._ensure_collection()

        self._collection.delete(ids=[vector_id])
        logger.debug("rag_delete_vector", vector_id=vector_id)


# ── Singleton ───────────────────────────────────────────────────────────────

_rag_manager: RAGManager | None = None


def get_rag_manager() -> RAGManager:
    """Return the process-wide singleton RAGManager (lazy-init).

    EmbeddingProvider (SentenceTransformer ~80MB) and Chroma client are
    created once and shared across all callers: ContextManager (memory
    injection), MCP RAG server (rag_server.py), Memory API, and Celery
    tasks (conversation_extract.py).
    """
    global _rag_manager
    if _rag_manager is not None:
        return _rag_manager

    from athena.config import get_config

    config = get_config()
    embedding = EmbeddingProvider(config)
    _rag_manager = RAGManager(config, embedding)
    logger.info("rag_manager_singleton_initialized")
    return _rag_manager
