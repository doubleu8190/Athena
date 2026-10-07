"""会话 SSE 路由和实时事件背压测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.gateway.routes.events import router, session_events
import athena.runtime.transport as transport_module
from athena.runtime.transport import SessionEventBus
from tests.fakes import install_runtime


def event_record(sequence: int) -> SimpleNamespace:
    return SimpleNamespace(
        session_seq=sequence,
        event_type="run.started",
        durability="durable",
        session_id="session-1",
        run_id="run-1",
        message_id=None,
        attachment_id=None,
        stream_id=None,
        stream_type=None,
        is_complete=False,
        parent_run_id=None,
        transition_id=None,
        payload_json='{"status":"running"}',
        occurred_at="2026-10-06T10:00:00+00:00",
    )


def test_session_events_replays_from_last_event_id_and_cleans_up() -> None:
    async def run() -> None:
        app = FastAPI()
        app.include_router(router)
        event_store = AsyncMock()
        queue: asyncio.Queue[ApplicationEvent] = asyncio.Queue()
        event_store.open_subscription.return_value = (queue, 6)
        event_store.list_events_between.return_value = [event_record(5), event_record(6)]
        transport = AsyncMock()
        db = SimpleNamespace(sessions=SimpleNamespace(get=AsyncMock(return_value=object())))
        install_runtime(app, db=db, event_store=event_store, realtime_transport=transport)

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/sessions/session-1/events",
                "headers": [(b"last-event-id", b"4")],
                "app": app,
            }
        )
        request.is_disconnected = AsyncMock(return_value=True)  # type: ignore[method-assign]
        response = await session_events("session-1", request, after=1)
        chunks = [chunk async for chunk in response.body_iterator]

        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert "connection" not in response.headers
        assert len(chunks) == 2
        assert chunks[0].startswith("id: 5\n")
        assert chunks[1].startswith("id: 6\n")
        event_store.list_events_between.assert_awaited_once_with("session-1", 4, 6)
        transport.close_subscription.assert_awaited_once_with("session-1", queue)

    asyncio.run(run())


def test_session_events_rejects_missing_session_and_negative_cursor() -> None:
    app = FastAPI()
    app.include_router(router)
    db = SimpleNamespace(sessions=SimpleNamespace(get=AsyncMock(return_value=None)))
    install_runtime(app, db=db)
    client = TestClient(app)

    assert client.get("/sessions/missing/events").status_code == 404
    assert client.get("/sessions/session-1/events?after=-1").status_code == 422


@pytest.mark.asyncio
async def test_realtime_events_do_not_block_when_subscriber_queue_is_full() -> None:
    bus = SessionEventBus()
    queue = await bus.open_subscription("session-1")
    realtime = ApplicationEvent(
        event_type=EventType.LLM_TOKEN,
        durability=EventDurability.REALTIME,
        session_id="session-1",
        payload={"delta": "x"},
    )
    for _ in range(queue.maxsize):
        queue.put_nowait(realtime)

    await asyncio.wait_for(bus.publish(realtime), timeout=0.1)
    assert queue.qsize() == queue.maxsize
    await bus.close_subscription("session-1", queue)


@pytest.mark.asyncio
async def test_stalled_durable_subscriber_is_removed_after_delivery_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_module, "_DURABLE_DELIVERY_TIMEOUT_SECONDS", 0.01)
    bus = SessionEventBus()
    queue = await bus.open_subscription("session-1")
    durable = ApplicationEvent(
        event_type=EventType.RUN_STARTED,
        durability=EventDurability.DURABLE,
        session_id="session-1",
        session_seq=1,
    )
    for _ in range(queue.maxsize):
        queue.put_nowait(durable)

    await asyncio.wait_for(bus.publish(durable), timeout=0.1)
    assert "session-1" not in bus._subscribers
