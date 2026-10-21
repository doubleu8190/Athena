"""Session 消息和运行列表 Controller 契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from application.runs import RunQueryService
from application.sessions import SessionMessageQueryService, SessionService
from bootstrap import create_app
from domain.events import ApplicationEvent, EventDurability
from domain.runs import RunStatus, RunSummary
from domain.sessions import (
    Message,
    MessageRole,
    Session,
)


class SessionRepository:
    def __init__(self) -> None:
        self.items = {
            "session-1": Session.create(
                session_id="session-1",
                title="Session",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            "session-2": Session.create(
                session_id="session-2",
                title="Other Session",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        }

    async def create(self, value):
        self.items[value.id] = value
        return value

    async def get(self, session_id):
        return self.items.get(session_id)

    async def list_sessions(self):
        return list(self.items.values())

    async def save(self, value):
        self.items[value.id] = value
        return value

    async def delete(self, session_id):
        return self.items.pop(session_id, None) is not None


class MessageRepository:
    async def save(self, message):
        return message.id

    async def get(self, message_id):
        return None

    async def list_by_session(self, session_id, *, limit=None):
        values = [
            Message(
                id="message-1",
                session_id=session_id,
                role=MessageRole.USER,
                content="hello",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ]
        return values[:limit] if limit is not None else values

    async def list_after(self, session_id, after_id):
        return []


class RunRepository:
    async def list_for_session(self, session_id):
        if session_id != "session-1":
            return []
        return [
            RunSummary(
                run_id="run-1",
                session_id=session_id,
                status=RunStatus.COMPLETED,
                root_thread_id="thread-1",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:01:00+00:00",
            )
        ]


class EventHistory:
    def __init__(self):
        self.events = [
            ApplicationEvent(
                event_type="node.completed",
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=4,
                run_id="run-1",
                payload={
                    "activity_id": "node:run-1:prepare_request",
                    "name": "prepare_request",
                    "status": "completed",
                    "duration_ms": 12,
                    "message": "must not be exposed",
                    "arguments": {"command": "private command"},
                    "output": "private output",
                    "error": {"code": "node_failed", "message": "safe diagnostic", "stack": "private stack"},
                },
            )
        ]

    async def list_events_for_run(self, session_id, run_id, *, after=0, limit=200):
        values = [event for event in self.events if event.session_id == session_id and event.run_id == run_id and event.session_seq > after]
        return values[:limit], len(values) > limit


def make_client(events=None) -> TestClient:
    sessions = SessionRepository()
    session_service = SessionService(sessions, id_factory=lambda: "unused")
    messages = MessageRepository()
    runs = RunRepository()
    return TestClient(
        create_app(
            session_service_factory=lambda: session_service,
            message_query_factory=lambda: SessionMessageQueryService(sessions, messages),
            run_query_factory=lambda: RunQueryService(sessions, runs, events),
        )
    )


def test_session_messages_and_runs_are_dto_mapped() -> None:
    client = make_client()

    messages = client.get("/api/sessions/session-1/messages")
    assert messages.status_code == 200
    assert messages.json()[0]["content"] == "hello"
    assert messages.json()[0]["role"] == "user"

    runs = client.get("/api/sessions/session-1/runs")
    assert runs.status_code == 200
    assert runs.json()[0]["run_id"] == "run-1"
    assert runs.json()[0]["status"] == "completed"


def test_session_messages_and_runs_return_404_for_unknown_session() -> None:
    client = make_client()
    assert client.get("/api/sessions/missing/messages").status_code == 404
    assert client.get("/api/sessions/missing/runs").status_code == 404


def test_run_timeline_is_scoped_and_filters_sensitive_payload_fields() -> None:
    client = make_client(EventHistory())

    timeline = client.get("/api/sessions/session-1/runs/run-1/timeline")
    assert timeline.status_code == 200
    body = timeline.json()
    assert body["next_after_seq"] == 4
    assert body["events"][0]["payload"]["name"] == "prepare_request"
    assert body["events"][0]["payload"]["error"] == {"code": "node_failed", "message": "safe diagnostic"}
    assert "message" not in body["events"][0]["payload"]
    assert "arguments" not in body["events"][0]["payload"]
    assert "output" not in body["events"][0]["payload"]

    assert client.get("/api/sessions/session-2/runs/run-1/timeline").status_code == 404
