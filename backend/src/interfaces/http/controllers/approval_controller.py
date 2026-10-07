"""目标审批 Controller。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from application.approval import ApprovalService
from domain.approval import ApprovalDecision, ApprovalRequest


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|denied)$")
    run_id: str
    task_id: str


class ApprovalBatchRequest(BaseModel):
    decisions: dict[str, str]
    run_id: str | None = None


def build_approval_router(factory: Callable[[], ApprovalService]) -> APIRouter:
    """构建只依赖 ApprovalService 的审批路由。"""
    router = APIRouter(prefix="/approvals", tags=["approvals"])

    @router.get("")
    async def pending(session_id: str | None = None) -> list[dict[str, Any]]:
        return [_response(value) for value in await factory().pending(session_id)]

    @router.get("/logs")
    async def history(
        session_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return [_response(value) for value in await factory().history(session_id, limit=limit, offset=offset)]

    @router.get("/stats")
    async def stats(session_id: str | None = None) -> dict[str, int]:
        pending_items = await factory().pending(session_id)
        history_items = await factory().history(session_id, limit=100000, offset=0)
        return {
            "pending": len(pending_items),
            "approved": sum(item.decision is not None and item.decision.value == "approved" for item in history_items),
            "denied": sum(item.decision is not None and item.decision.value == "denied" for item in history_items),
            "total": len(pending_items) + len(history_items),
        }

    @router.post("/{approval_id}/resolve")
    async def resolve(approval_id: str, body: ApprovalDecisionRequest) -> dict[str, Any]:
        updated = await factory().resolve(
            approval_id,
            run_id=body.run_id,
            task_id=body.task_id,
            decision=ApprovalDecision(body.decision),
        )
        if not updated:
            raise HTTPException(status_code=409, detail="approval_not_pending")
        return {"status": "resolved", "approval_id": approval_id, "decision": body.decision}

    @router.post("/batches/{batch_id}/resolve")
    @router.post("/batches/{approval_batch_id}/respond")
    async def resolve_batch(batch_id: str | None = None, approval_batch_id: str | None = None, body: ApprovalBatchRequest = None) -> dict[str, Any]:
        batch_id = batch_id or approval_batch_id
        assert body is not None and batch_id is not None
        decisions = {key: ApprovalDecision(value) for key, value in body.decisions.items()}
        if not await factory().resolve_batch(batch_id, decisions, run_id=body.run_id):
            raise HTTPException(status_code=409, detail="approval_batch_not_pending")
        return {"status": "resolved", "approval_batch_id": batch_id}

    return router


def _response(value: ApprovalRequest) -> dict[str, Any]:
    """转换审批实体为兼容旧接口的 JSON。"""
    return {
        "approval_id": value.approval_id,
        "tool_name": value.tool_name,
        "arguments": dict(value.arguments),
        "risk_level": value.risk_level,
        "created_at": value.created_at.isoformat() if value.created_at else None,
        "session_id": value.session_id,
        "run_id": value.run_id,
        "tool_call_id": value.tool_call_id,
        "approval_batch_id": value.approval_batch_id,
        "resolved": value.decision is not None,
        "resolution": value.decision.value if value.decision else value.status.value,
    }


__all__ = ["build_approval_router"]
