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
            "plan": {
                "plan_id": "plan-1",
                "status": "waiting_review",
                "user_goal": "Build a report",
                "max_parallelism": 2,
                "current_version": 1,
                "active_round_id": "round-1",
                "cancellation_epoch": 0,
                "versions": [{
                    "version": 1,
                    "change_reason": "initial plan",
                    "based_on_round_id": None,
                    "created_at": now,
                }],
                "rounds": [{
                    "round_id": "round-1",
                    "plan_version": 1,
                    "round_number": 1,
                    "status": "completed",
                    "expected_task_count": 1,
                    "terminal_task_count": 1,
                    "created_at": now,
                    "completed_at": now,
                    "tasks": [{
                        "task_id": "task-1",
                        "title": "Research",
                        "objective": "Find sources",
                        "status": "completed",
                        "active_execution_id": "execution-1",
                        "terminal_at": now,
                        "executions": [{
                            "execution_id": "execution-1",
                            "attempt": 1,
                            "worker_thread_id": "worker-thread-1",
                            "status": "completed",
                            "fencing_token": 1,
                            "cancellation_epoch": 0,
                            "lease_until": None,
                            "result_ref": "result-1",
                            "error": None,
                            "created_at": now,
                            "updated_at": now,
                        }],
                    }],
                }],
            },
            "approvals": [],
        }

    async def get_plan_version(self, plan_id: str, version: int):
        if (plan_id, version) != ("plan-1", 1):
            return None
        return {
            "plan_id": plan_id,
            "version": version,
            "dag_json": {"nodes": [{"task_id": "task-1"}], "edges": []},
            "change_reason": "initial plan",
            "based_on_round_id": None,
            "created_at": datetime(2026, 10, 22, tzinfo=timezone.utc),
        }


def make_client() -> TestClient:
    service = OrchestrationQueryService(ReadRepository())
    return TestClient(
        create_app(orchestration_query_factory=lambda: service)
    )


def test_run_snapshot_returns_persisted_round_and_execution_facts() -> None:
    response = make_client().get("/api/runs/run-1/orchestration")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["cancellation_epoch"] == 0
    assert body["plan"]["versions"][0]["change_reason"] == "initial plan"
    task = body["plan"]["rounds"][0]["tasks"][0]
    assert task["task_id"] == "task-1"
    assert task["executions"][0]["fencing_token"] == 1
    assert body["approvals"] == []


def test_plan_version_returns_immutable_dag_and_unknown_resources_are_404() -> None:
    client = make_client()

    response = client.get("/api/plans/plan-1/versions/1")
    assert response.status_code == 200
    assert response.json()["dag_json"]["nodes"][0]["task_id"] == "task-1"
    assert client.get("/api/plans/plan-1/versions/0").status_code == 404
    assert client.get("/api/plans/missing/versions/1").status_code == 404
    assert client.get("/api/runs/missing/orchestration").status_code == 404
