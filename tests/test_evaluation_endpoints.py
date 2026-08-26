"""个人检索评估工作台 REST API 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.evaluation.portal import EvaluationPortal
from athena.gateway.routes.evaluation import router
from tests.fakes import install_runtime


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    runtime = install_runtime(app)
    runtime.evaluation_portal = EvaluationPortal(tmp_path / "evaluation", record_enabled=True)
    with TestClient(app) as test_client:
        yield test_client


def _seed_event(client: TestClient) -> None:
    portal = client.app.state.runtime.evaluation_portal
    assert portal is not None
    portal.record_retrieval({
        "event_id": "evt-test-1",
        "session_id": "session-test",
        "run_id": "run-test",
        "source": "memory",
        "query": "where is the project config",
        "scope": {"source": "memory", "session_id": "session-test"},
        "results": [{"id": "memory-1", "document_alias": "memory-1", "content": "config", "locator": {"memory_alias": "memory-1"}}],
        "result_count": 1,
        "total_duration_ms": 12.5,
    })


def test_evaluation_endpoints_are_registered_and_return_empty_pages(client: TestClient):
    assert client.get("/api/evaluation/events").status_code == 200
    assert client.get("/api/evaluation/feedback").status_code == 200
    assert client.get("/api/evaluation/cases").status_code == 200
    assert client.get("/api/evaluation/reports").status_code == 200
    settings = client.get("/api/evaluation/settings")
    assert settings.status_code == 200
    assert settings.json()["counts"] == {"records": 0, "feedback": 0, "cases": 0, "reports": 0}


def test_feedback_correction_and_promotion_round_trip(client: TestClient):
    _seed_event(client)

    events = client.get("/api/evaluation/events", params={"session_id": "session-test"})
    assert events.status_code == 200
    assert events.json()["items"][0]["event_id"] == "evt-test-1"

    accepted = client.post("/api/evaluation/feedback", json={"event_id": "evt-test-1", "rating": "accepted"})
    assert accepted.status_code == 200
    assert accepted.json()["rating"] == "accepted"

    corrected = client.post("/api/evaluation/feedback", json={
        "event_id": "evt-test-1",
        "rating": "corrected",
        "correct_result_ids": ["memory-1"],
        "gain": 3,
        "comment": "confirmed",
    })
    assert corrected.status_code == 200
    assert corrected.json()["can_promote"] is True

    feedback_id = corrected.json()["feedback_id"]
    promoted = client.post(f"/api/evaluation/feedback/{feedback_id}/promote")
    assert promoted.status_code == 200
    assert promoted.json()["case"]["source"] == "memory"
    assert client.get("/api/evaluation/cases").json()["total"] == 1
    assert client.get("/api/evaluation/events/evt-test-1").json()["feedback"]["rating"] == "corrected"


def test_invalid_feedback_and_explicit_cleanup(client: TestClient):
    _seed_event(client)
    missing_answer = client.post("/api/evaluation/feedback", json={"event_id": "evt-test-1", "rating": "corrected"})
    assert missing_answer.status_code == 400

    no_delete_before_confirmation = client.get("/api/evaluation/settings")
    assert no_delete_before_confirmation.json()["counts"]["records"] == 1

    cleared = client.request("DELETE", "/api/evaluation/records", json={"targets": ["records"]})
    assert cleared.status_code == 200
    assert client.get("/api/evaluation/settings").json()["counts"]["records"] == 0


def test_settings_patch_validates_and_persists(client: TestClient):
    response = client.patch("/api/evaluation/settings", json={"record_enabled": False, "record_sample_rate": 0.25})
    assert response.status_code == 200
    assert response.json()["record_enabled"] is False
    assert response.json()["record_sample_rate"] == pytest.approx(0.25)

    invalid = client.patch("/api/evaluation/settings", json={"record_sample_rate": 1.5})
    assert invalid.status_code == 422
