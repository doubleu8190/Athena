"""记忆管理路由 — 检索/保存/删除记忆."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.core.memory.memory import MemoryManager

logger = get_logger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


class SaveMemoryRequest(BaseModel):
    content: str
    session_id: str
    metadata: dict[str, Any] | None = None
    pinned: bool = False


class SearchMemoryRequest(BaseModel):
    query: str
    where: dict[str, Any] | None = None
    n_results: int = 5


class UpdateMemoryRequest(BaseModel):
    content: str


async def _get_memory_manager() -> MemoryManager | None:
    from athena.gateway.routes._runtime import get_workflow
    workflow = get_workflow()
    if workflow is None:
        return None
    # AgentWorkflow 直接持有 memory_manager（记忆检索服务上的 _memory 属主不同，不可用）
    return getattr(workflow, "_memory_manager", None)


@router.post("/save")
async def save_memory(req: SaveMemoryRequest) -> dict[str, Any]:
    """主动保存记忆条目."""
    manager = await _get_memory_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Memory manager not initialized")
    try:
        meta = {"session_id": req.session_id, **(req.metadata or {})}
        memory_id = await manager.add_memory(
            content=req.content,
            metadata=meta,
            pinned=req.pinned,
        )
        return {"status": "saved", "memory_id": memory_id}
    except Exception as e:
        logger.exception("save_memory_failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search")
async def search_memory(req: SearchMemoryRequest) -> list[dict[str, Any]]:
    """检索记忆."""
    manager = await _get_memory_manager()
    if manager is None:
        return []
    try:
        return await manager.search(
            query=req.query,
            n_results=req.n_results,
            where=req.where,
        )
    except Exception as e:
        logger.exception("search_memory_failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_memories(
    limit: int = 50,
    offset: int = 0,
    pinned: bool = False,
    expired: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    """分页列出记忆条目，附带统计（total/pinned/expired/recent_week）."""
    manager = await _get_memory_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Memory manager not initialized")
    items = await manager.list_memories(
        limit=limit,
        offset=offset,
        pinned_only=pinned,
        expired_only=expired,
        session_id=session_id,
    )
    stats = await manager.count_memories()
    return {"items": items, **stats}


@router.get("/{memory_id}")
async def get_memory(memory_id: str) -> dict[str, Any]:
    """根据 ID 获取记忆."""
    manager = await _get_memory_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Memory manager not initialized")
    result = await manager.get(memory_id)
    if not result:
        raise HTTPException(status_code=404, detail="Memory not found")
    return result


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str) -> dict[str, str]:
    """删除记忆条目."""
    manager = await _get_memory_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Memory manager not initialized")
    await manager.delete(memory_id)
    return {"status": "deleted", "memory_id": memory_id}


@router.patch("/{memory_id}")
async def update_memory(
    memory_id: str, req: UpdateMemoryRequest
) -> dict[str, Any]:
    """编辑记忆内容."""
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content must not be empty")
    manager = await _get_memory_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Memory manager not initialized")
    updated = await manager.update_memory(memory_id, content)
    if not updated:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "updated", "memory_id": memory_id}


@router.post("/{memory_id}/pin")
async def pin_memory(memory_id: str, pinned: bool = True) -> dict[str, Any]:
    """固定/取消固定记忆."""
    manager = await _get_memory_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Memory manager not initialized")
    await manager.pin(memory_id, pinned=pinned)
    return {"status": "pinned" if pinned else "unpinned", "memory_id": memory_id}


@router.post("/cleanup")
async def cleanup_expired() -> dict[str, Any]:
    """清理过期记忆（手动触发）."""
    manager = await _get_memory_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Memory manager not initialized")
    count = await manager.cleanup_expired()
    return {"status": "cleaned", "count": count}
