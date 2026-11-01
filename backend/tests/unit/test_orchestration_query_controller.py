"""编排快照 HTTP 契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from application.orchestration import OrchestrationQueryService
from bootstrap import create_app


class ReadRepository:
    async def get_run_orchestration_snapshot(self, run_id: str):
        if run_id != "run-1":
            return None
        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        return {
            "run": {
                "run_id": run_id,
                "session_id": "session-1",
                "status": "running",
                "root_thread_id": "root-thread-1",
                "cancellation_epoch": 0,
                "cancellation_external_effect_status": "none",
                "created_at": now,
                "updated_at": now,
            },
            "worker_plans": [{
                "plan_id": "plan-1",
                "run_id": run_id,
                "sequence": 1,
                "goal": "Build a report",
                "status": "completed",
                "expected_task_count": 1,
                "terminal_task_count": 1,
                "root_wait_generation": 0,
                "created_at": now,
                "completed_at": now,
                "tasks": [{
                    "task_id": "task-1",
                    "operation_id": "operation-1",
                    "task_ref": "research",
                    "objective": "Find sources",
                    "acceptance_criteria": "At least one source",
                    "status": "completed",
                    "active_execution_id": "execution-1",
                    "generation": 1,
                    "output": ["source-1"],
                    "error": None,
                    "executions": [{
                        "execution_id": "execution-1",
                        "generation": 1,
                        "status": "completed",
                        "started_at": now,
                        "deadline_at": now,
                        "completed_at": now,
                        "cancellation_epoch": 0,
                        "capacity_reserved": False,
                        "error": None,
                    }],
                }],
            }],
            "approvals": [],
        }


def make_client() -> TestClient:
    service = OrchestrationQueryService(ReadRepository())
    return TestClient(
        create_app(orchestration_query_service=service)
    )


def test_run_snapshot_returns_worker_plan_and_execution_facts() -> None:
    response = make_client().get("/api/runs/run-1/orchestration")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["cancellation_epoch"] == 0
    assert body["worker_plans"][0]["sequence"] == 1
    task = body["worker_plans"][0]["tasks"][0]
    assert task["task_id"] == "task-1"
    assert task["output"] == ["source-1"]
    assert task["executions"][0]["completed_at"] == "2026-10-22T00:00:00Z"
    assert body["approvals"] == []


def test_plan_version_endpoint_is_removed_and_unknown_run_is_404() -> None:
    client = make_client()

    assert client.get("/api/plans/plan-1/versions/1").status_code == 404
    assert client.get("/api/runs/missing/orchestration").status_code == 404
