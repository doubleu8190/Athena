"""Memory management API — CRUD for user memories with dual-write to SQLite + ChromaDB."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from athena.api.deps import get_config_dep
from athena.config import Config
from athena.core.memory import MemoryStore
from athena.core.rag import get_rag_manager
from athena.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["memory"])


# ── Response helpers ──────────────────────────────────────────────────


def success(data: Any = None, message: str = "success") -> dict[str, Any]:  # noqa: ANN401
    return {"code": 0, "message": message, "data": data}


def error(code: int, message: str, detail: str = "") -> dict[str, Any]:
    return {"code": code, "message": message, "detail": detail, "data": None}


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


def _get_memory_store(config: Config = Depends(get_config_dep)) -> MemoryStore:
    rag = get_rag_manager()
    return MemoryStore(config, rag)


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("/list")
async def list_memories(
    key_prefix: str = "",
    limit: int = 50,
    config: Config = Depends(get_config_dep),
    store: MemoryStore = Depends(_get_memory_store),
):
    """List user memories from SQLite, ordered by most recent."""
    user_id = config.user.web.user_id
    memories = await store.simple_query(user_id, key_prefix=key_prefix, limit=limit)
    items = [
        {
            "memory_id": m.memory_id,
            "key": m.key,
            "value": m.value,
            "meta": m.meta_json,
            "updated_at": m.updated_at,
        }
        for m in memories
    ]
    return success({"memories": items, "count": len(items)})


@router.post("")
async def create_memory(
    body: MemoryCreate,
    config: Config = Depends(get_config_dep),
    store: MemoryStore = Depends(_get_memory_store),
):
    """Create or update a memory (dual-write to SQLite + ChromaDB)."""
    user_id = config.user.web.user_id
    memory = await store.upsert(user_id=user_id, key=body.key, value=body.value, meta=body.meta)
    return success(
        {
            "memory_id": memory.memory_id,
            "key": memory.key,
            "value": memory.value,
        },
        message="created",
    )


@router.put("/{memory_id}")
async def update_memory(
    memory_id: str,
    body: MemoryUpdate,
    config: Config = Depends(get_config_dep),
    store: MemoryStore = Depends(_get_memory_store),
):
    """Update an existing memory by memory_id."""
    # Read existing to get the key
    user_id = config.user.web.user_id
    existing = await store.simple_query(user_id, limit=1000)
    target = next((m for m in existing if m.memory_id == memory_id), None)

    if not target:
        return error(404, "memory_not_found", f"Memory {memory_id} not found")

    memory = await store.upsert(
        user_id=user_id,
        key=target.key,
        value=body.value,
        meta=body.meta or target.meta_json,
    )
    return success(
        {
            "memory_id": memory.memory_id,
            "key": memory.key,
            "value": memory.value,
        },
        message="updated",
    )


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    store: MemoryStore = Depends(_get_memory_store),
):
    """Delete a memory from both SQLite and ChromaDB."""
    deleted = await store.delete(memory_id)
    if not deleted:
        return error(404, "memory_not_found", f"Memory {memory_id} not found")
    return success(message="deleted")


@router.post("/search")
async def search_memories(
    body: MemorySearch,
    config: Config = Depends(get_config_dep),
    store: MemoryStore = Depends(_get_memory_store),
):
    """Semantic search across user memories via ChromaDB vector query.

    Use 'type' to filter: "atomic_fact" for granular facts,
    "paragraph_summary" for discussion summaries. Omit to search all.
    """
    user_id = config.user.web.user_id
    where = {"type": body.type} if body.type else None
    memories = await store.semantic_search(
        user_id=user_id,
        query=body.query,
        top_k=body.top_k,
        where=where,
    )
    items = [
        {
            "memory_id": m.memory_id,
            "key": m.key,
            "value": m.value,
            "score": m.meta_json.get("score", 0.0),
            "meta": m.meta_json,
        }
        for m in memories
    ]
    return success({"memories": items, "count": len(items)})
