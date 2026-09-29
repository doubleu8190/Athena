"""记忆查询路由。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from athena.utils.logging import get_logger
from athena.container import get_runtime_container
from athena.contracts.errors import ErrorDetail

if TYPE_CHECKING:
    from athena.core.memory.long_term_memory import LongTermMemoryService

logger = get_logger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


class SearchMemoryRequest(BaseModel):
    """记忆检索请求体。"""

    query: str
    where: dict[str, Any] | None = None
    n_results: int = 5


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
    expired: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    """分页列出记忆条目，附带总数、过期数和最近一周新增数。"""
    service = await _get_memory_service(request)
    items = await service.list_memories(
        limit=limit,
        offset=offset,
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
