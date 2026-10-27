from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from application.common.responses import RunStopResponse
from application.runs import RunService
from bootstrap import create_app
from domain.runs import RunCancellationResult, RunEnqueueResult, RunStatus


class Submissions:
    def __init__(self):
        self.items = {}

    async def enqueue_run(self, **values):
        request_id = values["request_id"]
        if request_id in self.items:
            return self.items[request_id], True
        payload = values["payload"]
        result = RunEnqueueResult(
            values["session_id"], values["run_id"], payload.message_id,
            1, RunStatus.QUEUED, False,
        )
        self.items[request_id] = result
        return result, False


class Lifecycle:
    async def stop_active(self, session_id, **values):
        run_id = values["expected_run_id"]
        if run_id is None:
            return RunCancellationResult(RunStatus.CANCELLED, False, "none", no_op=True), False
        return RunCancellationResult(RunStatus.STOPPING, True, "unknown", run_id=run_id), False

    async def finalize_stop(self, run_id, *, failed=False):
        return False


def make_client() -> TestClient:
    ids = iter(["session-1", "run-1", "message-1"])
    service = RunService(
        Submissions(),
        Lifecycle(),
        id_factory=lambda: next(ids),
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return TestClient(create_app(run_service_factory=lambda: service))


def test_submit_run_requires_idempotency_key_and_uses_server_ids():
    client = make_client()
    response = client.post(
        "/api/runs",
        json={"project_id": "project-1", "message": "hello"},
        headers={"Idempotency-Key": "request-1"},
    )
    assert response.status_code == 202
    assert response.json() == {
        "session_id": "session-1",
        "message_id": "message-1",
        "run_id": "run-1",
        "sequence": 1,
        "status": "queued",
    }
    assert "command_id" not in response.json()


def test_stop_route_is_session_scoped_and_carries_display_cursor():
    client = make_client()
    stopped = client.post(
        "/api/sessions/session-1/stop",
        json={"expected_run_id": "run-active", "displayed_session_seq": 8},
        headers={"Idempotency-Key": "stop-1"},
    )
    assert stopped.status_code == 202
    assert stopped.json() == {
        "run_id": "run-active", "status": "stopping", "no_op": False,
    }


def test_missing_idempotency_key_and_legacy_command_routes_are_rejected():
    client = make_client()
    missing_key = client.post("/api/runs", json={"project_id": "p", "message": "x"})
    assert missing_key.status_code == 422
    assert client.get("/api/commands/missing").status_code == 404
    assert client.post("/api/runs/missing/cancel").status_code == 404
