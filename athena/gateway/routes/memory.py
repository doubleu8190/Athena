"""记忆管理路由 — 检索/保存/删除记忆."""

from __future__ import annotations

from typing import Any, Literal, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from athena.utils.logging import get_logger
from athena.container import get_runtime_container
from athena.contracts.errors import ErrorDetail

if TYPE_CHECKING:
    from athena.core.memory.long_term_memory import LongTermMemoryService

logger = get_logger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


class CreateMemoryRequest(BaseModel):
    """主动保存记忆的请求体。"""

    content: str
    session_id: str
    metadata: dict[str, Any] | None = None
    pinned: bool = False


class SearchMemoryRequest(BaseModel):
    """记忆检索请求体。"""

    query: str
    where: dict[str, Any] | None = None
    n_results: int = 5


class UpdateMemoryRequest(BaseModel):
    """修改记忆正文的请求体。"""

    content: str


class MemoryValidityRequest(BaseModel):
    """更新事实有效性的请求体。"""

    validity_status: Literal["valid", "uncertain", "invalid"]
    valid_until: str | None = None


async def _get_memory_service(request: Request) -> LongTermMemoryService:
    """从请求应用状态获取长期记忆服务。

    参数：
        request (Request): 当前 HTTP 请求对象。

    返回值：
        LongTermMemoryService: 应用启动时注入的长期记忆服务。

    异常：
        RuntimeError: 应用运行时未初始化。
    """
    return get_runtime_container(request).memory_service


@router.post("/save")
async def create_memory(req: CreateMemoryRequest, request: Request) -> dict[str, Any]:
    """主动保存记忆条目."""
    service = await _get_memory_service(request)
    try:
        meta = {"session_id": req.session_id, **(req.metadata or {})}
        memory_id = await service.add_memory(
            content=req.content,
            metadata=meta,
            pinned=req.pinned,
        )
        return {"status": "saved", "memory_id": memory_id}
    except Exception as e:
        logger.exception("save_memory_failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search")
async def search_memory(
    req: SearchMemoryRequest, request: Request
) -> list[dict[str, Any]]:
    """检索记忆."""
    service = await _get_memory_service(request)
    try:
        return await service.search(
            query=req.query,
            n_results=req.n_results,
            where=req.where,
        )
    except Exception as e:
        logger.exception("search_memory_failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_memories(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    pinned: bool = False,
    expired: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    """分页列出记忆条目，附带统计（total/pinned/expired/recent_week）."""
    service = await _get_memory_service(request)
    items = await service.list_memories(
        limit=limit,
        offset=offset,
        pinned_only=pinned,
        expired_only=expired,
        session_id=session_id,
    )
    stats = await service.count_memories()
    return {"items": items, **stats}


@router.get("/{memory_id}")
async def get_memory(memory_id: str, request: Request) -> dict[str, Any]:
    """根据 ID 获取记忆."""
    service = await _get_memory_service(request)
    result = await service.get_memory(memory_id)
    if not result:
        raise HTTPException(status_code=404, detail=ErrorDetail.MEMORY_NOT_FOUND)
    return result


@router.get("/{memory_id}/revisions")
async def list_memory_revisions(
    memory_id: str, request: Request
) -> list[dict[str, Any]]:
    """读取记忆的完整不可变 revision 链。"""
    service = await _get_memory_service(request)
    revisions = await service.list_memory_revisions(memory_id)
    if not revisions:
        raise HTTPException(status_code=404, detail=ErrorDetail.MEMORY_NOT_FOUND)
    return revisions


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, request: Request) -> dict[str, str]:
    """删除记忆条目."""
    service = await _get_memory_service(request)
    await service.delete_memory(memory_id)
    return {"status": "deleted", "memory_id": memory_id}


@router.patch("/{memory_id}")
async def update_memory(
    memory_id: str, req: UpdateMemoryRequest, request: Request
) -> dict[str, Any]:
    """编辑记忆内容."""
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail=ErrorDetail.CONTENT_EMPTY)
    service = await _get_memory_service(request)
    new_memory_id = await service.update_memory(memory_id, content)
    if new_memory_id is None:
        raise HTTPException(status_code=404, detail=ErrorDetail.MEMORY_NOT_FOUND)
    return {
        "status": "updated",
        "memory_id": new_memory_id,
        "revised_from": memory_id,
    }


@router.post("/{memory_id}/pin")
async def set_memory_pinned(
    memory_id: str, request: Request, pinned: bool = True
) -> dict[str, Any]:
    """固定/取消固定记忆."""
    service = await _get_memory_service(request)
    await service.set_memory_pinned(memory_id, pinned=pinned)
    return {"status": "pinned" if pinned else "unpinned", "memory_id": memory_id}


@router.patch("/{memory_id}/validity")
async def set_memory_validity(
    memory_id: str, req: MemoryValidityRequest, request: Request
) -> dict[str, Any]:
    """更新记忆的事实有效性，不影响访问热度和保留期限。"""
    service = await _get_memory_service(request)
    try:
        updated = await service.set_memory_validity(
            memory_id,
            req.validity_status,
            valid_until=req.valid_until,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail=ErrorDetail.MEMORY_NOT_FOUND)
    return {
        "status": "updated",
        "memory_id": memory_id,
        "validity_status": req.validity_status,
        "valid_until": req.valid_until,
    }


@router.post("/cleanup")
async def cleanup_expired_memories(request: Request) -> dict[str, Any]:
    """清理过期记忆（手动触发）."""
    service = await _get_memory_service(request)
    count = await service.cleanup_expired()
    return {"status": "cleaned", "count": count}
