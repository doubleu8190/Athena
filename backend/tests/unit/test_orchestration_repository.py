from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from domain.orchestration import (
    PlanStatus,
    TaskStatus,
)
from infrastructure.persistence.postgres.models import (
    TaskExecutionResultModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresOrchestrationRepository,
)


class Result:
    def __init__(self, *, scalar=None, rows=(), rowcount=1):
        self._scalar = scalar
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class Session:
    def __init__(self):
        self.get_values = []
        self.execute_values = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args, **_kwargs):
        return self.get_values.pop(0) if self.get_values else None

    async def execute(self, _statement):
        return self.execute_values.pop(0) if self.execute_values else Result()


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


def task(task_id, *, status=TaskStatus.PENDING, generation=0, output=None, error=None):
    return SimpleNamespace(
        task_id=task_id,
        plan_id="p-1",
        run_id="r-1",
        title=task_id,
        objective=f"do {task_id}",
        input_json={},
        expected_output_json={},
        allowed_tools_json=[],
        worker_type="general",
        status=status.value,
        execution_generation=generation,
        timeout_seconds=3600.0,
        worker_thread_id=None,
        active_execution_id=None,
        output_json=output,
        error_json=error,
    )


@pytest.mark.asyncio
async def test_orchestration_repository_reads_plan_status():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    plan_row = SimpleNamespace(
        plan_id="p-1", status="created"
    )
    session.get_values = [plan_row]
    assert await repository.get_plan_status("p-1") is PlanStatus.CREATED


@pytest.mark.asyncio
async def test_load_review_context_uses_requested_round_and_its_plan_version():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    round_row = SimpleNamespace(
        round_id="round-2",
        plan_id="p-1",
        plan_version=1,
        round_number=2,
        status="completed",
        expected_task_count=1,
        terminal_task_count=1,
    )
    plan_row = SimpleNamespace(
        plan_id="p-1",
        status=PlanStatus.WAITING_REVIEW.value,
        active_round_id="round-2",
        current_version=1,
        user_goal="Build the report",
    )
    dag = {
        "nodes": [
            {"task_id": "a", "title": "Research", "objective": "Find facts", "input": {}, "expected_output": {}, "allowed_tools": [], "worker_type": "general", "timeout_seconds": 3600},
            {"task_id": "b", "title": "Analyze", "objective": "Analyze facts", "input": {}, "expected_output": {}, "allowed_tools": [], "worker_type": "general", "timeout_seconds": 3600},
            {"task_id": "c", "title": "Draft", "objective": "Write draft", "input": {}, "expected_output": {}, "allowed_tools": [], "worker_type": "general", "timeout_seconds": 3600},
            {"task_id": "d", "title": "Publish", "objective": "Publish report", "input": {}, "expected_output": {}, "allowed_tools": [], "worker_type": "general", "timeout_seconds": 3600},
        ],
        "edges": [
            {"from": "a", "to": "d"},
            {"from": "b", "to": "c"},
        ],
        "user_constraints": ["Cite sources"],
        "remaining_budget": {"tokens": 1200},
    }
    version_row = SimpleNamespace(dag_json=dag)
    tasks = [
        task("a", status=TaskStatus.FAILED, error='{"reason":"source_missing"}'),
        task("b", status=TaskStatus.COMPLETED),
        task("c", status=TaskStatus.PENDING),
        task(
            "d",
            status=TaskStatus.CANCELLED,
            error='{"reason":"dependency_failed"}',
        ),
    ]
    previous_execution = SimpleNamespace(
        execution_id="exec-a",
        round_id="round-1",
        task_id="a",
        result_ref=None,
        error_json={"reason": "source_missing"},
        result_json={
            "output": {
                "output": {"cause": "source unavailable"},
                "evidence": ["source-1"],
            }
        },
    )
    current_output = {
        "output": {"answer": "draft ready"},
        "evidence": ["report-1"],
        "uncertainties": ["one figure needs confirmation"],
    }
    serialized_output = json.dumps(
        current_output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    current_execution = SimpleNamespace(
        execution_id="exec-b",
        round_id="round-2",
        task_id="b",
        result_ref="result-b",
        error_json=None,
        result_json=None,
    )
    result_artifact = SimpleNamespace(
        result_ref="result-b",
        execution_id="exec-b",
        result_json=current_output,
        content_bytes=len(serialized_output),
        content_sha256=hashlib.sha256(serialized_output).hexdigest(),
    )
    previous_round_task = SimpleNamespace(
        round_id="round-1", task_id="a", status="failed"
    )
    current_round_task = SimpleNamespace(
        round_id="round-2", task_id="b", status="completed"
    )
    session.get_values = [round_row, plan_row, version_row]
    session.execute_values = [
        Result(rows=tasks),
        Result(
            rows=[
                (1, previous_round_task, previous_execution),
                (2, current_round_task, current_execution),
            ]
        ),
        Result(rows=[result_artifact]),
    ]

    context = await repository.load_review_context("round-2")

    assert context.plan_id == "p-1"
    assert context.plan_version == 1
    assert context.round_id == "round-2"
    assert context.full_dag_summary == dag
    assert [item["task_id"] for item in context.previous_results_summary] == ["a"]
    assert context.previous_results_summary[0]["summary"] == '{"cause":"source unavailable"}'
    assert [item["task_id"] for item in context.round_results] == ["b"]
    assert context.round_results[0]["summary"] == '{"answer":"draft ready"}'
    assert context.round_results[0]["result_ref"] == "result-b"
    assert context.round_results[0]["evidence"] == ["report-1"]
    assert context.round_results[0]["uncertainties"] == [
        "one figure needs confirmation"
    ]
    assert [item["task_id"] for item in context.remaining_tasks] == ["c", "d"]
    assert context.remaining_tasks[1]["blocked_by"] == ["a"]
    assert context.user_constraints == ("Cite sources",)
    assert context.remaining_budget == {"tokens": 1200}


@pytest.mark.asyncio
async def test_load_review_context_rejects_a_round_that_is_no_longer_active():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    session.get_values = [
        SimpleNamespace(
            round_id="round-1",
            plan_id="p-1",
            status="completed",
            plan_version=1,
            terminal_task_count=1,
            expected_task_count=1,
        ),
        SimpleNamespace(
            status=PlanStatus.WAITING_REVIEW.value,
            active_round_id="round-2",
            current_version=2,
        ),
    ]

    with pytest.raises(RuntimeError, match="review_round_identity_stale"):
        await repository.load_review_context("round-1")

    assert session.execute_values == []


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_target_orchestration_repository_loads_task_results():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    failed, pending = task("a", status=TaskStatus.FAILED), task("b")
    session.execute_values = [Result(rows=[failed, pending])]
    results = await repository.get_results("p-1")
    assert results["a"].status is TaskStatus.FAILED
