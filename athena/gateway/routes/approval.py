"""审批管理路由 — 列出待审批、响应审批."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from athena.utils.logging import get_logger
from athena.container import get_runtime_container
from athena.contracts.errors import ErrorDetail
from athena.infrastructure.postgre.repositories import _json_loads

if TYPE_CHECKING:
    from athena.gateway.approval import ApprovalManager

logger = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ApprovalDecisionRequest(BaseModel):
    """审批响应请求体。

    ``action`` 仅接受 ``allow`` 或 ``deny``，由路由转换为内部审批决定。
    """

    action: str  # allow / deny（允许 / 拒绝）


async def _get_approval_manager(request: Request) -> ApprovalManager:
    """从请求应用状态获取审批管理器。

    参数：
        request (Request): 当前 HTTP 请求对象。

    返回值：
        ApprovalManager: 应用启动时注入的审批管理器。

    异常：
        RuntimeError: 应用运行时未初始化。
    """
    return get_runtime_container(request).approval_manager


@router.get("")
async def list_pending_approvals(
    request: Request, session_id: str | None = None
) -> list[dict[str, Any]]:
    """列出待审批请求."""
    runtime = get_runtime_container(request)
    rows = await runtime.agent_store.list_pending_approvals(session_id)
    return [
        {
            "approval_id": row.approval_id,
            "tool_name": row.tool_name,
            "arguments": _json_loads(row.arguments_json, {}),
            "risk_level": row.risk_level,
            "created_at": row.created_at,
            "session_id": row.session_id,
            "run_id": row.run_id,
            "tool_call_id": row.tool_call_id,
            "timeout": runtime.approval_manager.timeout,
            "expires_at": row.expires_at,
            "resolved": False,
            "resolution": "pending",
        }
        for row in rows
    ]


def _approval_log_payload(row: Any) -> dict[str, Any]:
    """将持久化审批记录转换为审批日志接口对象。"""
    created_at = _parse_datetime(row.created_at)
    decided_at = _parse_datetime(row.decided_at) if row.decided_at else created_at
    decision = {
        "expired": "timeout",
        "approved": "approved",
        "denied": "denied",
        "cancelled": "cancelled",
    }.get(row.decision or "denied", "denied")
    return {
        "id": row.approval_id,
        "session_id": row.session_id,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "arguments": _json_loads(row.arguments_json, {}),
        "risk_level": row.risk_level,
        "decision": decision,
        "decision_time_ms": max(0.0, (decided_at - created_at).total_seconds() * 1000),
        "timestamp": row.created_at,
    }


def _parse_datetime(value: str) -> datetime:
    """解析数据库中的 ISO 时间并统一为带 UTC 时区的时间。"""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


@router.get("/logs")
async def list_approval_logs(
    request: Request,
    session_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """分页查询已完成的审批日志。"""
    runtime = get_runtime_container(request)
    rows = await runtime.agent_store.list_approval_history(
        session_id=session_id, limit=limit, offset=offset
    )
    return [_approval_log_payload(row) for row in rows]


@router.get("/logs/{session_id}")
async def list_session_approval_logs(
    session_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """兼容按路径传入会话标识的审批日志查询。"""
    return await list_approval_logs(
        request, session_id=session_id, limit=limit, offset=offset
    )


@router.get("/stats")
async def get_approval_stats(request: Request) -> dict[str, float | int]:
    """返回审批日志统计数据。"""
    runtime = get_runtime_container(request)
    rows = await runtime.agent_store.list_resolved_approvals()
    today = datetime.now(timezone.utc).date()
    today_rows = [row for row in rows if _parse_datetime(row.created_at).date() == today]
    approved = sum(row.decision == "approved" for row in rows)
    denied = sum(row.decision == "denied" for row in rows)
    timeout = sum(row.decision == "expired" for row in rows)
    today_approved = sum(row.decision == "approved" for row in today_rows)
    today_denied = sum(row.decision == "denied" for row in today_rows)
    today_timeout = sum(row.decision == "expired" for row in today_rows)
    total = approved + denied + timeout
    return {
        "today_total": len(today_rows),
        "today_approved": today_approved,
        "today_denied": today_denied,
        "today_timeout": today_timeout,
        "approval_rate": approved / total if total else 0.0,
    }


@router.post("/{approval_id}/respond")
async def submit_approval_decision(
    approval_id: str, req: ApprovalDecisionRequest, request: Request
) -> dict[str, Any]:
    """响应审批请求."""
    if req.action not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail=ErrorDetail.INVALID_APPROVAL_ACTION)
    runtime = get_runtime_container(request)
    approval = await runtime.agent_store.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=ErrorDetail.APPROVAL_NOT_FOUND)
    resolved = await runtime.approval_manager.submit_approval_decision(
        approval_id, req.action
    )
    if not resolved:
        raise HTTPException(status_code=409, detail="approval_already_resolved")
    return {
        "status": "resolved",
        "approval_id": approval_id,
    }


@router.post("/{approval_id}/cancel")
async def cancel_approval(approval_id: str, request: Request) -> dict[str, Any]:
    """提交审批取消命令，由 Runtime 条件更新持久化记录。"""
    runtime = get_runtime_container(request)
    approval = await runtime.agent_store.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=ErrorDetail.APPROVAL_NOT_FOUND)
    resolved = await runtime.approval_manager.cancel_approval(approval_id)
    if not resolved:
        raise HTTPException(status_code=409, detail="approval_already_resolved")
    return {
        "status": "resolved",
        "approval_id": approval_id,
    }


@router.post("/session/{session_id}/cancel-all")
async def cancel_session_approvals(session_id: str, request: Request) -> dict[str, Any]:
    """取消指定会话的所有待审批请求."""
    manager = await _get_approval_manager(request)
    await manager.cancel_pending_approvals(session_id)
    return {"status": "cancelled", "session_id": session_id}
