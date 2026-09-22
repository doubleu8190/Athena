"""审批管理路由 — 列出待审批、响应审批."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
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
