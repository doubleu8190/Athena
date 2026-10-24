"""阶段 5 事件领域、适配器和 SSE 契约测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from starlette.requests import Request

from application.events import EventStreamService
from bootstrap import create_app
from domain.events import ApplicationEvent, EventDurability, EventPort, EventType
from domain.sessions import Session


class Sessions:
    async def get(self, session_id: str):
        return (
            Session.create(
                session_id=session_id,
                title="Session",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            if session_id == "session-1"
            else None
        )


class Events(EventPort):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[ApplicationEvent] = asyncio.Queue()
        self.replayed = [
            ApplicationEvent(
                event_type=EventType.RUN_STARTED,
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=5,
                payload={"status": "running"},
            )
        ]
        self.closed: list[tuple[str, asyncio.Queue[ApplicationEvent]]] = []

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        return self.queue, 5

    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        return event

    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent:
        return event

    async def list_events_between(
        self, session_id: str, after: int = 0, upto: int | None = None,
        limit: int = 256,
    ) -> list[ApplicationEvent]:
        return [event for event in self.replayed if event.session_seq > after][:limit]

    async def list_events_for_run(
        self,
        session_id: str,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> tuple[list[ApplicationEvent], bool]:
        return [], False

    async def close_subscription(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        self.closed.append((session_id, queue))


def make_client():
    events = Events()
    service = EventStreamService(Sessions(), events)
    client = TestClient(create_app(event_stream_factory=lambda: service))
    return client, events, events


def test_sse_replays_from_last_event_id_and_cleans_up() -> None:
    from  interfaces.http.sse.events import build_events_router

    events = Events()
    replayed_event = events.replayed[0]
    completed_event = replace(
        replayed_event, session_seq=6, event_type=EventType.RUN_COMPLETED
    )
    events.replayed.append(completed_event)
    events.queue.put_nowait(replayed_event)
    events.queue.put_nowait(completed_event)
    service = EventStreamService(Sessions(), events)
    app = __import__("fastapi").FastAPI()
    router = build_events_router(lambda: service)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/sessions/{session_id}/events"
    )
    app.include_router(router)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/events",
            "headers": [(b"last-event-id", b"4")],
            "app": app,
        }
    )
    disconnect_checks = 0

    async def disconnect_after_live_event() -> bool:
        nonlocal disconnect_checks
        disconnect_checks += 1
        return disconnect_checks > 2

    request.is_disconnected = disconnect_after_live_event  # type: ignore[method-assign]

    async def run():
        response = await endpoint("session-1", request, 0, service)
        chunks = [chunk async for chunk in response.body_iterator]
        assert "id: 5" in chunks[0]
        assert "run.started" in chunks[0]
        assert "id: 6" in chunks[1]
        assert "run.completed" in chunks[1]
        assert len(chunks) == 2

    asyncio.run(run())
    assert events.closed
    assert events.closed[0][0] == "session-1"


def test_sse_rejects_unknown_session_and_negative_cursor() -> None:
    client, _, _ = make_client()
    assert client.get("/api/sessions/missing/events").status_code == 404
    assert client.get("/api/sessions/session-1/events?after=-1").status_code == 422
