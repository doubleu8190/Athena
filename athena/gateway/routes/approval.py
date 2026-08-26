"""审批管理路由 — 列出待审批、响应审批."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from athena.models import ApprovalLog
from athena.utils.logging import get_logger
from athena.runtime import runtime_from

if TYPE_CHECKING:
    from athena.gateway.approval import ApprovalManager

logger = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ApprovalResponseRequest(BaseModel):
    """表示 审批ResponseRequest 组件，封装相关状态和行为。
    """
    action: str  # allow / deny（允许 / 拒绝）


async def _get_approval_manager(request: Request) -> ApprovalManager:
    """执行“获取审批管理器”操作。

    参数：
        request (Request): 当前 HTTP 或 WebSocket 请求对象。

    返回值：
        审批Manager: 操作结果；具体语义由调用场景决定。

    异常：
        Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
    """
    return runtime_from(request).approval_manager


@router.get("")
async def list_pending_approvals(request: Request, session_id: str | None = None) -> list[dict[str, Any]]:
    """列出待审批请求."""
    manager = await _get_approval_manager(request)
    return manager.get_pending(session_id=session_id)


@router.post("/{approval_id}/respond")
async def respond_approval(
    approval_id: str, req: ApprovalResponseRequest, request: Request
) -> dict[str, Any]:
    """响应审批请求."""
    manager = await _get_approval_manager(request)
    if req.action not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail="action must be 'allow' or 'deny'")
    ok = await manager.respond_approval(approval_id, req.action)
    if not ok:
        raise HTTPException(
            status_code=404, detail="Approval not found or already resolved"
        )
    return {"status": "responded", "approval_id": approval_id, "action": req.action}


@router.post("/{approval_id}/cancel")
async def cancel_approval(approval_id: str, request: Request) -> dict[str, Any]:
    """取消审批请求."""
    manager = await _get_approval_manager(request)
    await manager.cancel_approval(approval_id)
    return {"status": "cancelled", "approval_id": approval_id}


@router.post("/session/{session_id}/cancel-all")
async def cancel_session_approvals(session_id: str, request: Request) -> dict[str, Any]:
    """取消指定会话的所有待审批请求."""
    manager = await _get_approval_manager(request)
    await manager.cancel_all_pending(session_id)
    return {"status": "cancelled", "session_id": session_id}


@router.get("/logs/{session_id}")
async def get_approval_logs(session_id: str, request: Request) -> list[ApprovalLog]:
    """获取会话审批日志."""
    db = runtime_from(request).db
    return await db.approval_logs.get_by_session(session_id)


@router.get("/logs")
async def list_approval_logs(
    request: Request,
    session_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ApprovalLog]:
    """分页列出审批日志（新→旧），可选按会话过滤."""
    db = runtime_from(request).db
    return await db.approval_logs.list_all(
        limit=limit, offset=offset, session_id=session_id
    )


@router.get("/stats")
async def approval_stats(request: Request) -> dict[str, Any]:
    """今日审批统计，含审批通过率."""
    db = runtime_from(request).db
    stats = await db.approval_logs.stats()
    decided = stats["today_approved"] + stats["today_denied"]
    approval_rate = round(stats["today_approved"] / decided, 4) if decided else 0.0
    return {**stats, "approval_rate": approval_rate}
