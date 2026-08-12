"""审批管理路由 — 列出待审批、响应审批."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from athena.models import ApprovalLog
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.gateway.approval import ApprovalManager

logger = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ApprovalResponseRequest(BaseModel):
    action: str  # allow / deny


async def _get_approval_manager() -> ApprovalManager | None:
    from athena.gateway.routes._runtime import get_workflow

    workflow = get_workflow()
    if workflow is None:
        return None
    # ApprovalManager 通过 tool_manager 注入，从 workflow 间接访问
    tool_manager = getattr(workflow, "_tool_manager", None)
    if tool_manager is None:
        return None
    return getattr(tool_manager, "_approval_manager", None)


@router.get("")
async def list_pending_approvals(session_id: str | None = None) -> list[dict[str, Any]]:
    """列出待审批请求."""
    manager = await _get_approval_manager()
    if manager is None:
        return []
    return manager.get_pending(session_id=session_id)


@router.post("/{approval_id}/respond")
async def respond_approval(
    approval_id: str, req: ApprovalResponseRequest
) -> dict[str, Any]:
    """响应审批请求."""
    manager = await _get_approval_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Approval manager not initialized")
    if req.action not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail="action must be 'allow' or 'deny'")
    ok = await manager.respond_approval(approval_id, req.action)
    if not ok:
        raise HTTPException(
            status_code=404, detail="Approval not found or already resolved"
        )
    return {"status": "responded", "approval_id": approval_id, "action": req.action}


@router.post("/{approval_id}/cancel")
async def cancel_approval(approval_id: str) -> dict[str, Any]:
    """取消审批请求."""
    manager = await _get_approval_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Approval manager not initialized")
    await manager.cancel_approval(approval_id)
    return {"status": "cancelled", "approval_id": approval_id}


@router.post("/session/{session_id}/cancel-all")
async def cancel_session_approvals(session_id: str) -> dict[str, Any]:
    """取消指定会话的所有待审批请求."""
    manager = await _get_approval_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail="Approval manager not initialized")
    await manager.cancel_all_pending(session_id)
    return {"status": "cancelled", "session_id": session_id}


@router.get("/logs/{session_id}")
async def get_approval_logs(session_id: str) -> list[ApprovalLog]:
    """获取会话审批日志."""
    from athena.config.settings import get_settings
    from athena.db.database import get_database

    db = await get_database(get_settings().sqlite_db_path)
    return await db.approval_logs.get_by_session(session_id)


@router.get("/logs")
async def list_approval_logs(
    session_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ApprovalLog]:
    """分页列出审批日志（新→旧），可选按会话过滤."""
    from athena.config.settings import get_settings
    from athena.db.database import get_database

    db = await get_database(get_settings().sqlite_db_path)
    return await db.approval_logs.list_all(
        limit=limit, offset=offset, session_id=session_id
    )


@router.get("/stats")
async def approval_stats() -> dict[str, Any]:
    """今日审批统计，含审批通过率."""
    from athena.config.settings import get_settings
    from athena.db.database import get_database

    db = await get_database(get_settings().sqlite_db_path)
    stats = await db.approval_logs.stats()
    decided = stats["today_approved"] + stats["today_denied"]
    approval_rate = round(stats["today_approved"] / decided, 4) if decided else 0.0
    return {**stats, "approval_rate": approval_rate}
