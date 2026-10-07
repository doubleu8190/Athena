"""阶段 5 事件领域、适配器和 SSE 契约测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.src.application.events import EventStreamService
from backend.src.bootstrap import create_app
from backend.src.domain.events import ApplicationEvent, EventDurability, EventType
from backend.src.domain.sessions import Session


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


class Events:
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

    async def open_subscription(self, session_id):
        return self.queue, 5

    async def list_events_between(self, session_id, after=0, upto=None):
        return [event for event in self.replayed if event.session_seq > after]


class Transport:
    def __init__(self) -> None:
        self.closed = []

    async def close_subscription(self, session_id, queue):
        self.closed.append((session_id, queue))


def make_client():
    events = Events()
    transport = Transport()
    service = EventStreamService(Sessions(), events, transport)
    client = TestClient(create_app(event_stream_factory=lambda: service))
    return client, events, transport


def test_sse_replays_from_last_event_id_and_cleans_up() -> None:
    from backend.src.interfaces.http.sse.events import build_events_router

    events = Events()
    transport = Transport()
    service = EventStreamService(Sessions(), events, transport)
    app = __import__("fastapi").FastAPI()
    app.include_router(build_events_router(lambda: service))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/events",
            "headers": [(b"last-event-id", b"4")],
            "app": app,
        }
    )
    request.is_disconnected = lambda: _disconnected_once()  # type: ignore[method-assign]

    async def run():
        response = await app.routes[-1].endpoint("session-1", request, 0, service)
        chunks = [chunk async for chunk in response.body_iterator]
        assert "id: 5" in chunks[0]
        assert "run.started" in chunks[0]

    asyncio.run(run())
    assert transport.closed
    assert transport.closed[0][0] == "session-1"


async def _disconnected_once() -> bool:
    return True


def test_sse_rejects_unknown_session_and_negative_cursor() -> None:
    client, _, _ = make_client()
    assert client.get("/api/sessions/missing/events").status_code == 404
    assert client.get("/api/sessions/session-1/events?after=-1").status_code == 422

