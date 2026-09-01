"""审批管理路由 — 列出待审批、响应审批."""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from athena.models import ApprovalLog
from athena.utils.logging import get_logger
from athena.runtime import runtime_from
from athena.contracts.commands import Command, CommandType
from athena.contracts.errors import ErrorDetail
from athena.contracts.statuses import AgentApprovalDecision
from athena.utils.ids import generate_time_id

if TYPE_CHECKING:
    from athena.gateway.approval import ApprovalManager

logger = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ApprovalResponseRequest(BaseModel):
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
    return runtime_from(request).approval_manager


@router.get("")
async def list_pending_approvals(
    request: Request, session_id: str | None = None
) -> list[dict[str, Any]]:
    """列出待审批请求."""
    rows = await runtime_from(request).agent_store.pending_approvals(session_id)
    return [
        {
            "approval_id": row.approval_id,
            "tool_name": row.tool_name,
            "arguments": json.loads(row.arguments_json),
            "risk_level": row.risk_level,
            "created_at": row.created_at,
            "session_id": row.session_id,
            "run_id": row.run_id,
            "tool_call_id": row.tool_call_id,
            "resolved": False,
            "resolution": "pending",
        }
        for row in rows
    ]


@router.post("/{approval_id}/respond", status_code=202)
async def respond_approval(
    approval_id: str, req: ApprovalResponseRequest, request: Request
) -> dict[str, Any]:
    """响应审批请求."""
    if req.action not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail=ErrorDetail.INVALID_APPROVAL_ACTION)
    runtime = runtime_from(request)
    approval = await runtime.agent_store.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=ErrorDetail.APPROVAL_NOT_FOUND)
    command = Command(
        command_id=f"cmd_{generate_time_id()}",
        command_type=CommandType.APPROVAL_RESOLVE,
        session_id=approval.session_id,
        run_id=approval.run_id,
        payload={
            "approval_id": approval_id,
            "decision": (
                AgentApprovalDecision.APPROVED.value
                if req.action == "allow"
                else AgentApprovalDecision.DENIED.value
            ),
        },
    )
    await runtime.agent_store.enqueue(command)
    return {
        "status": "pending",
        "approval_id": approval_id,
        "command_id": command.command_id,
    }


@router.post("/{approval_id}/cancel")
async def cancel_approval(approval_id: str, request: Request) -> dict[str, Any]:
    """提交审批取消命令，由 Runtime 条件更新持久化记录。"""
    runtime = runtime_from(request)
    approval = await runtime.agent_store.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=ErrorDetail.APPROVAL_NOT_FOUND)
    command = Command(
        command_id=f"cmd_{generate_time_id()}",
        command_type=CommandType.APPROVAL_CANCEL,
        session_id=approval.session_id,
        run_id=approval.run_id,
        payload={"approval_id": approval_id},
    )
    await runtime.agent_store.enqueue(command)
    return {
        "status": "pending",
        "approval_id": approval_id,
        "command_id": command.command_id,
    }


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
