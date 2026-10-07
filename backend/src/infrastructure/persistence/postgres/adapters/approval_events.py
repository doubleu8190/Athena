"""PostgreSQL durable event adapter for approval requests."""

from __future__ import annotations

from domain.approval import ApprovalRequest
from domain.events import ApplicationEvent, EventDurability, EventType
from domain.events import EventPublisherPort


class PostgresApprovalEventPublisher:
    """Publish approval-required events through the PostgreSQL event store."""

    def __init__(self, events: EventPublisherPort) -> None:
        """保存事件发布端口；具体 PostgreSQL repository 由 bootstrap 注入。"""
        self._events = events

    async def required(self, request: ApprovalRequest) -> None:
        """发布一条持久化的审批请求事件。"""
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.APPROVAL_REQUIRED,
                durability=EventDurability.DURABLE,
                session_id=request.session_id,
                run_id=request.run_id,
                payload={
                    "approval_id": request.approval_id,
                    "tool_name": request.tool_name,
                    "risk_level": request.risk_level,
                },
            )
        )


__all__ = ["PostgresApprovalEventPublisher"]
