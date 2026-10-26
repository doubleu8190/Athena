"""Session Controller 的 HTTP 契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from application.sessions import SessionService
from bootstrap import create_app
from domain.sessions import Session


class FakeSessionRepository:
    def __init__(self) -> None:
        self.items: dict[str, Session] = {}
        self.creation_requests: dict[str, str] = {}

    async def create(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def create_idempotent(self, session: Session, *, creation_request_id: str) -> Session:
        existing_id = self.creation_requests.get(creation_request_id)
        if existing_id:
            existing = self.items.get(existing_id)
            if existing is None:
                raise ValueError("session_deleted")
            return existing
        self.items[session.id] = session
        self.creation_requests[creation_request_id] = session.id
        return session

    async def get(self, session_id: str) -> Session | None:
        return self.items.get(session_id)

    async def list_sessions(self) -> list[Session]:
        return list(self.items.values())

    async def save(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def rename(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def delete(self, session_id: str) -> bool:
        return self.items.pop(session_id, None) is not None


def make_client() -> TestClient:
    repository = FakeSessionRepository()
    service = SessionService(
        repository,
        id_factory=lambda: "session-controller-1",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return TestClient(create_app(session_service_factory=lambda: service))


def test_session_controller_routes_crud_through_service() -> None:
    client = make_client()

    created = client.post(
        "/api/sessions",
        json={"project_id": "project-1", "title": "  Project  "},
        headers={"Idempotency-Key": "request-1"},
    )
    assert created.status_code == 200
    assert created.json()["title"] == "Project"
    assert created.json()["status"] == "idle"

    session_id = created.json()["id"]
    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [session_id]

    fetched = client.get(f"/api/sessions/{session_id}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Project"

    renamed = client.patch(
        f"/api/sessions/{session_id}", json={"title": "  Renamed  "}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed"

    deleted = client.delete(f"/api/sessions/{session_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {
        "status": "deleted",
        "session_id": session_id,
    }


def test_session_controller_maps_missing_and_invalid_requests() -> None:
    client = make_client()

    assert client.get("/api/sessions/missing").status_code == 404
    assert client.patch(
        "/api/sessions/missing", json={"title": "Renamed"}
    ).status_code == 404
    assert client.delete("/api/sessions/missing").status_code == 404

    created = client.post(
        "/api/sessions",
        json={"project_id": "project-1", "title": "Existing"},
        headers={"Idempotency-Key": "request-2"},
    )
    session_id = created.json()["id"]
    invalid = client.patch(
        f"/api/sessions/{session_id}", json={"title": "   "}
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "title_empty"


def test_session_controller_rejects_replay_for_deleted_idempotent_session() -> None:
    client = make_client()
    headers = {"Idempotency-Key": "request-deleted"}
    created = client.post(
        "/api/sessions", json={"project_id": "project-1"}, headers=headers
    )
    assert created.status_code == 200
    assert client.delete(f"/api/sessions/{created.json()['id']}").status_code == 200

    replay = client.post(
        "/api/sessions", json={"project_id": "project-1"}, headers=headers
    )

    assert replay.status_code == 409
    assert replay.json()["detail"] == "session_deleted"
