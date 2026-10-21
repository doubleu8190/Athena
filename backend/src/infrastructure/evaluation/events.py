"""Bridge evaluation progress to the existing durable event publisher."""

from __future__ import annotations

from domain.events import ApplicationEvent, EventDurability, EventPort


class EvaluationEventPublisher:
    def __init__(self, publisher: EventPort | None = None, *, session_id: str = "evaluation", fanout=None) -> None:
        self.publisher = publisher
        self.session_id = session_id
        self.fanout = fanout

    async def publish(self, event_type: str, payload: dict) -> None:
        if self.fanout is not None:
            await self.fanout.publish(event_type, payload)
        if self.publisher is not None:
            await self.publisher.publish(ApplicationEvent(event_type=event_type, durability=EventDurability.DURABLE, session_id=self.session_id, run_id=str(payload.get("evaluation_run_id")) if payload.get("evaluation_run_id") else None, payload=dict(payload)))


__all__ = ["EvaluationEventPublisher"]
