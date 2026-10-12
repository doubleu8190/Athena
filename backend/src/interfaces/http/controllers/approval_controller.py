"""目标审批 Controller。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from application.approval import ApprovalService
from application.approval.responses import ApprovalResponse


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
        service = factory()
        stats_method = getattr(service, "stats", None)
        if stats_method is not None:
            value = await stats_method(session_id)
            return {"pending": value.pending, "approved": value.approved, "denied": value.denied, "total": value.total}
        pending_items = await service.pending(session_id)
        history_items = await service.history(session_id, limit=100000, offset=0)
        approved = sum(_resolution(item) == "approved" for item in history_items)
        denied = sum(_resolution(item) == "denied" for item in history_items)
        return {"pending": len(pending_items), "approved": approved, "denied": denied, "total": len(pending_items) + len(history_items)}

    @router.post("/{approval_id}/resolve")
    async def resolve(approval_id: str, body: ApprovalDecisionRequest) -> dict[str, Any]:
        updated = await factory().resolve(
            approval_id,
            run_id=body.run_id,
            task_id=body.task_id,
            decision=body.decision,
        )
        if not updated:
            raise HTTPException(status_code=409, detail="approval_not_pending")
        return {"status": "resolved", "approval_id": approval_id, "decision": body.decision}

    @router.post("/batches/{batch_id}/resolve")
    async def resolve_batch(batch_id: str, body: ApprovalBatchRequest) -> dict[str, Any]:
        if not await factory().resolve_batch(batch_id, body.decisions, run_id=body.run_id):
            raise HTTPException(status_code=409, detail="approval_batch_not_pending")
        return {"status": "resolved", "approval_batch_id": batch_id}

    return router


def _response(value: ApprovalResponse) -> dict[str, Any]:
    """将审批实体转换为当前 HTTP 响应。"""
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


def _resolution(value: Any) -> str | None:
    """兼容旧审批实体和新的应用响应。"""
    resolution = getattr(value, "resolution", None)
    if resolution is not None:
        return str(getattr(resolution, "value", resolution))
    decision = getattr(value, "decision", None)
    return None if decision is None else str(getattr(decision, "value", decision))


__all__ = ["build_approval_router"]
