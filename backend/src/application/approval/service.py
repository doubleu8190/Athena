"""审批应用服务。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from backend.src.domain.events.entities import (
    ApplicationEvent,
    EventDurability,
    EventType,
)
from backend.src.domain.events.ports import EventPort
from domain.approval import (
    ApprovalDecision,
    ApprovalRepository,
    ApprovalRequest,
    ApprovalStatus,
)
from application.common.responses import ApprovalResponse, ApprovalStatsResponse


class ApprovalService:
    """创建、查询和解决工具审批请求。"""

    def __init__(
        self,
        repository: ApprovalRepository,
        events: EventPort,
        *,
        id_factory: Callable[[], str],
        resumer: Callable[..., Any] | None = None,
    ) -> None:
        self._repository = repository
        self._events = events
        self._id_factory = id_factory
        self._resumer = resumer

    def set_resumer(self, resumer: Callable[..., Any]) -> None:
        self._resumer = resumer

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
        execution_id: str | None = None,
        worker_thread_id: str | None = None,
        interrupt_id: str | None = None,
        args_hash: str | None = None,
        operation_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> ApprovalResponse:
        """幂等创建审批请求并发布待审批事件。"""
        existing = await self._repository.get(approval_id) if approval_id else None
        if existing is not None:
            return _to_response(existing)
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
            execution_id=execution_id,
            worker_thread_id=worker_thread_id,
            interrupt_id=interrupt_id,
            args_hash=args_hash,
            operation_key=operation_key,
            expires_at=expires_at,
        )
        if not await self._repository.create(request):
            recovered = await self._repository.get(request.approval_id)
            if recovered is None:
                raise RuntimeError("approval was not created and cannot be recovered")
            return _to_response(recovered)
        if not self._uses_transactional_event_outbox(request):
            await self._events.publish(
                ApplicationEvent(
                    event_type=EventType.APPROVAL_REQUIRED,
                    durability=EventDurability.DURABLE,
                    session_id=request.session_id,
                    run_id=request.run_id,
                    transition_id=f"approval:{request.approval_id}:requested",
                    payload={
                        "activity_id": f"approval:{request.approval_id}",
                        "activity_type": "approval",
                        "name": "tool_approval",
                        "parent_activity_id": f"tool:{request.tool_call_id}",
                        "status": "waiting",
                        "approval_id": request.approval_id,
                        "tool_name": request.tool_name,
                        "arguments": request.arguments,
                        "risk_level": request.risk_level,
                    },
                )
            )
        return _to_response(request)

    async def resolve(
        self,
        approval_id: str,
        *,
        run_id: str,
        task_id: str,
        decision: str,
    ) -> bool:
        """解决单条审批并返回是否成功。"""
        existing = await self._repository.get(approval_id)
        decision = ApprovalDecision(decision)
        value = await self._repository.resolve(
            approval_id,
            run_id=run_id,
            task_id=task_id,
            decision=decision,
        )
        if not value:
            return False
        if not self._uses_transactional_event_outbox(existing):
            await self._events.publish(
                ApplicationEvent(
                    event_type=EventType.APPROVAL_RESOLVED,
                    durability=EventDurability.DURABLE,
                    session_id=existing.session_id if existing else "",
                    run_id=run_id,
                    transition_id=f"approval:{approval_id}:resolved",
                    payload={
                        "activity_id": f"approval:{approval_id}",
                        "activity_type": "approval",
                        "name": "tool_approval",
                        "status": (
                            "approved"
                            if decision == ApprovalDecision.APPROVED
                            else "failed"
                        ),
                        "approval_id": approval_id,
                        "decision": decision.value,
                    },
                )
            )
        if self._resumer and existing and existing.execution_id is None:
            await self._resumer(existing, decision.value)
        return value

    async def resolve_batch(
        self,
        batch_id: str,
        decisions: dict[str, str | ApprovalDecision],
        *,
        run_id: str | None = None,
    ) -> bool:
        """按批次解决审批。"""
        normalized = {key: ApprovalDecision(value) for key, value in decisions.items()}
        value = await self._repository.resolve_batch(
            batch_id, normalized, run_id=run_id
        )
        if value:
            for approval_id, decision in normalized.items():
                existing = await self._repository.get(approval_id)
                if not self._uses_transactional_event_outbox(existing):
                    await self._events.publish(
                        ApplicationEvent(
                            event_type=EventType.APPROVAL_RESOLVED,
                            durability=EventDurability.DURABLE,
                            session_id=existing.session_id if existing else "",
                            run_id=run_id or (existing.run_id if existing else ""),
                            transition_id=f"approval:{approval_id}:resolved",
                            payload={
                                "activity_id": f"approval:{approval_id}",
                                "activity_type": "approval",
                                "name": "tool_approval",
                                "status": (
                                    "approved"
                                    if decision == ApprovalDecision.APPROVED
                                    else "failed"
                                ),
                                "approval_id": approval_id,
                                "decision": decision.value,
                            },
                        )
                    )
                if self._resumer and existing and existing.execution_id is None:
                    await self._resumer(existing, decision.value)
        return value

    async def expire_due_approvals(self, *, limit: int = 64) -> int:
        """将已到期审批写为拒绝并持久化对应 Worker 恢复作业。

        Args:
            limit: 一次数据库事务最多处理的审批数量，必须为正数。

        Returns:
            本次成功转换为 EXPIRED 的审批数量。

        Raises:
            ValueError: 批量上限小于 1。
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        return await self._repository.expire_due_approvals(limit=limit)

    def _uses_transactional_event_outbox(self, approval: Any) -> bool:
        """Avoid duplicate SSE writes when the repository commits the event."""
        if getattr(self._repository, "writes_approval_events_to_outbox", False):
            return True
        return bool(
            approval
            and getattr(approval, "plan_id", None)
            and getattr(approval, "task_id", None)
        )

    async def pending(self, session_id: str | None = None) -> list[ApprovalResponse]:
        """查询待审批请求。"""
        return [
            _to_response(value)
            for value in await self._repository.list_pending(session_id)
        ]

    async def history(
        self, session_id: str | None = None, *, limit: int = 50, offset: int = 0
    ) -> list[ApprovalResponse]:
        """查询已处理审批历史。"""
        return [
            _to_response(value)
            for value in await self._repository.list_history(
                session_id, limit=limit, offset=offset
            )
        ]

    async def stats(self, session_id: str | None = None) -> ApprovalStatsResponse:
        """返回审批数量统计。"""
        pending = await self.pending(session_id)
        history = await self.history(session_id, limit=100000, offset=0)
        approved = sum(item.resolution == "approved" for item in history)
        denied = sum(item.resolution == "denied" for item in history)
        return ApprovalStatsResponse(
            len(pending), approved, denied, len(pending) + len(history)
        )


def _to_response(value: ApprovalRequest) -> ApprovalResponse:
    """将审批实体转换为只读应用响应。"""
    return ApprovalResponse(
        approval_id=value.approval_id,
        tool_name=value.tool_name,
        arguments=dict(value.arguments),
        risk_level=value.risk_level,
        created_at=value.created_at,
        session_id=value.session_id,
        run_id=value.run_id,
        tool_call_id=value.tool_call_id,
        approval_batch_id=value.approval_batch_id,
        resolved=value.status != ApprovalStatus.PENDING,
        resolution=value.decision.value if value.decision else value.status.value,
        expires_at=value.expires_at,
    )


__all__ = ["ApprovalService"]
