"""终态 Run Trace HTTP 边界测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.observability import RunSpanAttributes, TraceSpan
from interfaces.http.controllers.runs_controller import build_run_query_router


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Runs:
    def __init__(self, status: str):
        self.status = status

    async def get(self, run_id: str):
        return SimpleNamespace(run_id=run_id, status=self.status)


class Observability:
    async def list_run_spans(self, run_id: str):
        return [
            TraceSpan(
                span_id=f"run:{run_id}",
                session_id="session-1",
                run_id=run_id,
                parent_span_id=None,
                span_type="run",
                name="run.execute",
                status="completed",
                started_at=NOW,
                completed_at=NOW,
                duration_ms=0,
                attributes=RunSpanAttributes(),
                error={
                    "code": "run_failed",
                    "message": "Trace 不得包含的敏感原文",
                },
                sequence=1,
            )
        ]


def _client(status: str) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_run_query_router(Runs(status), Observability()),
        prefix="/api",
    )
    return TestClient(app)


def test_active_run_trace_is_rejected() -> None:
    response = _client("running").get("/api/runs/run-1/trace")
    assert response.status_code == 409
    assert response.json()["detail"] == "run_not_terminal"


def test_terminal_run_trace_returns_span_tree_data() -> None:
    response = _client("completed").get("/api/runs/run-1/trace")
    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "status": "completed",
        "spans": [
            {
                "span_id": "run:run-1",
                "parent_span_id": None,
                "span_type": "run",
                "name": "run.execute",
                "status": "completed",
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": "2026-01-01T00:00:00Z",
                "duration_ms": 0,
                "attributes": {
                    "span_type": "run",
                },
                "error": {"code": "run_failed"},
                "sequence": 1,
            }
        ],
    }
    assert "敏感原文" not in response.text
