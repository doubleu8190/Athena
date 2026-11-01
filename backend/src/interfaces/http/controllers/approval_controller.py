"""目标审批 Controller。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from application.approval import ApprovalService
from application.approval.responses import ApprovalResponse


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|denied)$")


def build_approval_router(approval_service: ApprovalService) -> APIRouter:
    """构建只依赖 ApprovalService 的审批路由。"""
    router = APIRouter(prefix="/approvals", tags=["approvals"])

    @router.get("")
    async def pending(session_id: str | None = None) -> list[dict[str, Any]]:
        return [_response(value) for value in await approval_service.pending(session_id)]

    @router.get("/logs")
    async def history(
        session_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return [_response(value) for value in await approval_service.history(session_id, limit=limit, offset=offset)]

    @router.get("/stats")
    async def stats(session_id: str | None = None) -> dict[str, int]:
        value = await approval_service.stats(session_id)
        return {"pending": value.pending, "approved": value.approved, "denied": value.denied, "total": value.total}

    @router.get("/{approval_id}")
    async def get_status(approval_id: str) -> dict[str, Any]:
        value = await approval_service.get(approval_id)
        if value is None:
            raise HTTPException(status_code=404, detail="approval_not_found")
        return _response(value)

    async def record_decision(
        approval_id: str,
        body: ApprovalDecisionRequest,
        *,
        status: str,
    ) -> dict[str, Any]:
        updated = await approval_service.resolve(
            approval_id,
            decision=body.decision,
        )
        if not updated:
            raise HTTPException(status_code=409, detail="approval_not_pending")
        return {"status": status, "approval_id": approval_id, "decision": body.decision}

    @router.post("/{approval_id}/decision", status_code=202)
    async def decide(approval_id: str, body: ApprovalDecisionRequest) -> dict[str, Any]:
        return await record_decision(
            approval_id, body, status="decision_recorded"
        )

    return router


def _response(value: ApprovalResponse) -> dict[str, Any]:
    """将审批实体转换为当前 HTTP 响应。"""
    return {
        "approval_id": value.approval_id,
        "tool_name": value.tool_name,
        "arguments": dict(value.arguments),
        "risk_level": value.risk_level,
        "created_at": value.created_at.isoformat() if value.created_at else None,
        "expires_at": value.expires_at.isoformat() if value.expires_at else None,
        "session_id": value.session_id,
        "run_id": value.run_id,
        "plan_id": value.plan_id,
        "task_id": value.task_id,
        "tool_call_id": value.tool_call_id,
        "approval_batch_id": value.approval_batch_id,
        "resolved": value.resolved,
        "resolution": value.resolution,
    }


__all__ = ["build_approval_router"]
