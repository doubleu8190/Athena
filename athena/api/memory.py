"""Memory management API — CRUD for user memories via ChromaDB."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from athena.api.response import error, success
from athena.config import get_config
from athena.core.rag import RAGManager, get_rag_manager
from athena.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["memory"])


# ── Request models ────────────────────────────────────────────────────


class MemoryCreate(BaseModel):
    key: str
    value: str
    meta: dict[str, Any] | None = None


class MemoryUpdate(BaseModel):
    value: str
    meta: dict[str, Any] | None = None


class MemorySearch(BaseModel):
    query: str
    top_k: int = 5
    type: str | None = None


# ── Dependency ────────────────────────────────────────────────────────


def _get_rag_manager() -> RAGManager:
    return get_rag_manager()


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("/list")
async def list_memories(
    key_prefix: str = "",
    limit: int = 50,
):
    """List user memories from ChromaDB, ordered by most recent."""
    rag = get_rag_manager()
    config = get_config()
    user_id = config.user.web.user_id
    vectors = await rag.list_vectors(user_id, key_prefix=key_prefix, limit=limit)
    items = [
        {
            "memory_id": v.memory_id,
            "key": v.metadata.get("key", ""),
            "value": v.text,
            "meta": v.metadata,
            "updated_at": v.metadata.get("updated_at", ""),
        }
        for v in vectors
    ]
    return success({"memories": items, "count": len(items)})


@router.post("")
async def create_memory(
    body: MemoryCreate,
):
    """Create a memory (write to ChromaDB)."""
    rag = get_rag_manager()
    config = get_config()
    user_id = config.user.web.user_id
    memory_id = f"mem_{uuid.uuid4().hex[:16]}"

    await rag.upsert_vector(
        memory_id=memory_id,
        user_id=user_id,
        text=body.value,
        metadata={"key": body.key, **(body.meta or {})},
    )

    return success(
        {"memory_id": memory_id, "key": body.key, "value": body.value},
        message="created",
    )


@router.put("/{memory_id}")
async def update_memory(
    memory_id: str,
    body: MemoryUpdate,
):
    """Update an existing memory by memory_id."""
    rag = get_rag_manager()
    config = get_config()
    existing = await rag.get_vector(memory_id)
    if not existing:
        return error(404, "memory_not_found", f"Memory {memory_id} not found")

    key = existing.metadata.get("key", "")
    meta = {k: v for k, v in existing.metadata.items()
            if k not in ("user_id", "memory_id", "key", "updated_at")}
    if body.meta:
        meta.update(body.meta)

    await rag.upsert_vector(
        memory_id=memory_id,
        user_id=config.user.web.user_id,
        text=body.value,
        metadata={"key": key, **meta},
    )

    return success(
        {"memory_id": memory_id, "key": key, "value": body.value},
        message="updated",
    )


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
):
    """Delete a memory from ChromaDB."""
    rag = get_rag_manager()
    existing = await rag.get_vector(memory_id)
    if not existing:
        return error(404, "memory_not_found", f"Memory {memory_id} not found")

    rag.delete_vector(memory_id)
    return success(message="deleted")


@router.post("/search")
async def search_memories(
    body: MemorySearch,
):
    """Semantic search across user memories via ChromaDB vector query.

    Use 'type' to filter: "atomic_fact" for granular facts,
    "paragraph_summary" for discussion summaries. Omit to search all.
    """
    rag = get_rag_manager()
    config = get_config()
    user_id = config.user.web.user_id
    where = {"type": body.type} if body.type else None
    results = await rag.semantic_search(
        user_id=user_id,
        query=body.query,
        top_k=body.top_k,
        where=where,
    )
    items = [
        {
            "memory_id": r.memory_id,
            "key": r.metadata.get("key", ""),
            "value": r.text,
            "score": r.score,
            "meta": r.metadata,
        }
        for r in results
    ]
    return success({"memories": items, "count": len(items)})
