"""Publish durable approval events through an injected event port."""

from domain.approval import ApprovalEventPort, ApprovalRequest
from domain.events import ApplicationEvent, EventDurability, EventType, EventPublisherPort


class ApprovalEventPublisher(ApprovalEventPort):
    """Translate approval requests into durable application events."""

    def __init__(self, events: EventPublisherPort) -> None:
        self._events = events

    async def required(self, request: ApprovalRequest) -> None:
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.APPROVAL_REQUIRED,
                durability=EventDurability.DURABLE,
                session_id=request.session_id,
                run_id=request.run_id,
                payload={
                    "approval_id": request.approval_id,
                    "tool_name": request.tool_name,
                    "arguments": request.arguments,
                    "risk_level": request.risk_level,
                },
            )
        )

    async def resolved(self, approval_id: str, *, session_id: str, run_id: str, decision: str) -> None:
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.APPROVAL_RESOLVED,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                payload={"approval_id": approval_id, "decision": decision},
            )
        )


__all__ = ["ApprovalEventPublisher"]
