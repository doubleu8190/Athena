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
    session_id: str | None = None
    n_results: int = 5


async def _get_memory_manager() -> MemoryManager | None:
    from athena.gateway.routes._runtime import get_workflow
    workflow = get_workflow()
    if workflow is None:
        return None
    # 从记忆检索服务间接获取 memory_manager
    retrieval = getattr(workflow, "_memory_retrieval", None)
    if retrieval is None:
        return None
    return getattr(retrieval, "_memory", None)


@router.post("/save")
async def save_memory(req: SaveMemoryRequest) -> dict[str, Any]:
    """主动保存记忆条目."""
    manager = await _get_memory_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Memory manager not initialized")
    try:
        memory_id = await manager.add_memory(
            content=req.content,
            session_id=req.session_id,
            metadata=req.metadata,
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
            session_id=req.session_id,
            n_results=req.n_results,
        )
    except Exception as e:
        logger.exception("search_memory_failed")
        raise HTTPException(status_code=500, detail=str(e))


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
