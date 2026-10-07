"""审批应用服务。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from domain.approval import (
    ApprovalDecision,
    ApprovalEventPort,
    ApprovalRepository,
    ApprovalRequest,
)


class ApprovalService:
    """创建、查询和解决工具审批请求。"""

    def __init__(self, repository: ApprovalRepository, events: ApprovalEventPort, *, id_factory: Callable[[], str]) -> None:
        self._repository = repository
        self._events = events
        self._id_factory = id_factory

    async def request(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        risk_level: str,
        task_id: str | None = None,
        plan_id: str | None = None,
        approval_batch_id: str | None = None,
        approval_id: str | None = None,
    ) -> ApprovalRequest:
        """幂等创建审批请求并发布待审批事件。"""
        existing = await self._repository.get(approval_id) if approval_id else None
        if existing is not None:
            return existing
        request = ApprovalRequest(
            approval_id=approval_id or self._id_factory(),
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            risk_level=risk_level,
            task_id=task_id,
            plan_id=plan_id,
            approval_batch_id=approval_batch_id,
            created_at=datetime.now(timezone.utc),
        )
        if not await self._repository.create(request):
            recovered = await self._repository.get(request.approval_id)
            if recovered is None:
                raise RuntimeError("approval was not created and cannot be recovered")
            return recovered
        await self._events.required(request)
        return request

    async def resolve(self, approval_id: str, *, run_id: str, task_id: str, decision: ApprovalDecision) -> bool:
        """解决单条审批并返回是否成功。"""
        return await self._repository.resolve(approval_id, run_id=run_id, task_id=task_id, decision=decision)

    async def resolve_batch(self, batch_id: str, decisions: dict[str, ApprovalDecision], *, run_id: str | None = None) -> bool:
        """按批次解决审批。"""
        return await self._repository.resolve_batch(batch_id, decisions, run_id=run_id)

    async def pending(self, session_id: str | None = None) -> list[ApprovalRequest]:
        """查询待审批请求。"""
        return await self._repository.list_pending(session_id)

    async def history(self, session_id: str | None = None, *, limit: int = 50, offset: int = 0) -> list[ApprovalRequest]:
        """查询已处理审批历史。"""
        return await self._repository.list_history(session_id, limit=limit, offset=offset)


__all__ = ["ApprovalService"]
