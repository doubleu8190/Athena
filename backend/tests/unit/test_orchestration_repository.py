from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from domain.orchestration import (
    Plan,
    PlanEdge,
    PlanStatus,
    Task,
    TaskStatus,
)
from infrastructure.persistence.postgres.models import (
    OrchestrationOutboxModel,
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
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added.extend(values)

    async def get(self, *_args, **_kwargs):
        return self.get_values.pop(0) if self.get_values else None

    async def execute(self, _statement):
        return self.execute_values.pop(0) if self.execute_values else Result()


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


def plan(status=PlanStatus.CREATED):
    return Plan(
        "p-1",
        "goal",
        ("a", "b"),
        (PlanEdge("a", "b"),),
        max_parallelism=2,
        status=status,
        session_id="s-1",
        run_id="r-1",
    )


def task(task_id, *, status=TaskStatus.PENDING, generation=0, output=None, error=None):
    return SimpleNamespace(
        task_id=task_id,
        plan_id="p-1",
        run_id="r-1",
        title=task_id,
        objective=f"do {task_id}",
        input_json="{}",
        expected_output_json="{}",
        allowed_tools_json="[]",
        worker_type="general",
        status=status.value,
        execution_generation=generation,
        worker_thread_id=None,
        active_execution_id=None,
        output_json=output,
        error_json=error,
    )


@pytest.mark.asyncio
async def test_target_orchestration_repository_claims_and_completes_dag():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    await repository.create(plan(), [Task("a", "A", "do A"), Task("b", "B", "do B")])
    assert len(session.added) == 3

    plan_row = SimpleNamespace(
        plan_id="p-1", run_id="r-1", session_id="s-1", user_goal="goal",
        max_parallelism=2, status="created",
        plan_json='{"nodes":[{"task_id":"a"},{"task_id":"b"}],"edges":[{"from":"a","to":"b"}]}',
    )
    a_row, b_row = task("a"), task("b")
    session.get_values = [plan_row]
    session.execute_values = [Result(rows=[a_row, b_row])]
    loaded = await repository.get("p-1")
    assert loaded and loaded[0].edges[0].to_task_id == "b"

    session.get_values = [plan_row]
    session.execute_values = [Result(rows=[a_row, b_row])]
    claimed = await repository.claim_ready_tasks("p-1", 2)
    assert [item.task.task_id for item in claimed] == ["a"]
    assert a_row.status == TaskStatus.RUNNING.value and a_row.execution_generation == 1

    session.execute_values = [Result(scalar=a_row), Result(rows=[("done",), ("pending",)])]
    session.get_values = [SimpleNamespace(status="created", updated_at=None)]
    outcome = await repository.finish_task("a", 1, status=TaskStatus.DONE.value, output={"ok": True})
    assert outcome == {"accepted": True, "plan_completed": False}
    session.execute_values = [Result(rows=[("done",), ("done",)])]
    assert await repository._terminal_plan_status(session, "p-1") is PlanStatus.COMPLETED
    session.execute_values = [Result(rows=[("done",), ("cancelled",)])]
    assert await repository._terminal_plan_status(session, "p-1") is PlanStatus.FAILED


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
            {"task_id": "a", "title": "Research", "objective": "Find facts"},
            {"task_id": "b", "title": "Analyze", "objective": "Analyze facts"},
            {"task_id": "c", "title": "Draft", "objective": "Write draft"},
            {"task_id": "d", "title": "Publish", "objective": "Publish report"},
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
        task("b", status=TaskStatus.DONE),
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
        round_id="round-2", task_id="b", status="done"
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
@pytest.mark.parametrize(
    ("terminal_statuses", "expected_plan_status", "expected_event"),
    [
        (["done", "completed"], PlanStatus.COMPLETED, "PlanCompleted"),
        (["done", "failed"], PlanStatus.FAILED, "PlanFailed"),
    ],
)
async def test_finish_task_writes_plan_terminal_outbox_with_state(
    terminal_statuses, expected_plan_status, expected_event
):
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    task_row = task("b", status=TaskStatus.RUNNING, generation=3)
    plan_row = SimpleNamespace(
        plan_id="p-1",
        run_id="r-1",
        session_id="s-1",
        user_goal="goal",
        status=PlanStatus.RUNNING.value,
        current_version=2,
        active_round_id="round-2",
        updated_at=None,
    )
    session.execute_values = [
        Result(scalar=task_row),
        Result(rows=[(value,) for value in terminal_statuses]),
    ]
    session.get_values = [plan_row]

    outcome = await repository.finish_task(
        "b", 3, status=terminal_statuses[-1]
    )

    assert outcome == {"accepted": True, "plan_completed": True}
    assert plan_row.status == expected_plan_status.value
    outbox = next(
        value
        for value in session.added
        if isinstance(value, OrchestrationOutboxModel)
    )
    assert outbox.event_type == expected_event
    assert outbox.dedupe_key == f"plan:p-1:{expected_plan_status.value}"
    assert outbox.plan_version == 2
    assert outbox.round_id == "round-2"
    assert outbox.payload_json["goal"] == "goal"


@pytest.mark.asyncio
async def test_finish_task_does_not_reopen_cancelled_plan_or_emit_terminal_outbox():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    task_row = task("b", status=TaskStatus.RUNNING, generation=3)
    plan_row = SimpleNamespace(
        plan_id="p-1",
        run_id="r-1",
        session_id="s-1",
        user_goal="goal",
        status=PlanStatus.CANCELLED.value,
        current_version=2,
        active_round_id="round-2",
        updated_at=None,
    )
    session.execute_values = [
        Result(scalar=task_row),
        Result(rows=[("done",), ("cancelled",)]),
    ]
    session.get_values = [plan_row]

    outcome = await repository.finish_task("b", 3, status="done")

    assert outcome == {"accepted": True, "plan_completed": False}
    assert plan_row.status == PlanStatus.CANCELLED.value
    assert not any(
        isinstance(value, OrchestrationOutboxModel) for value in session.added
    )


@pytest.mark.asyncio
async def test_finalize_plan_records_deferred_terminal_after_retry_is_not_scheduled():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    task_row = task("b", status=TaskStatus.RUNNING, generation=3)
    plan_row = SimpleNamespace(
        plan_id="p-1",
        run_id="r-1",
        session_id="s-1",
        user_goal="goal",
        status=PlanStatus.RUNNING.value,
        current_version=2,
        active_round_id="round-2",
        updated_at=None,
    )
    session.execute_values = [
        Result(scalar=task_row),
        Result(rows=[("done",), ("failed",)]),
    ]
    session.get_values = [plan_row]

    outcome = await repository.finish_task(
        "b", 3, status="failed", finalize_plan=False
    )

    assert outcome == {"accepted": True, "plan_completed": True}
    assert plan_row.status == PlanStatus.RUNNING.value
    assert not session.added

    session.get_values = [plan_row]
    session.execute_values = [Result(rows=[("done",), ("failed",)])]
    status = await repository.finalize_plan("p-1")

    assert status is PlanStatus.FAILED
    assert plan_row.status == PlanStatus.FAILED.value
    assert session.added[0].event_type == "PlanFailed"


@pytest.mark.asyncio
async def test_target_orchestration_repository_dependency_failure_retry_and_results():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    plan_row = SimpleNamespace(
        plan_id="p-1", run_id="r-1", session_id="s-1", user_goal="goal",
        max_parallelism=2, status="running",
        plan_json='{"nodes":[{"task_id":"a"},{"task_id":"b"}],"edges":[{"from":"a","to":"b"}]}',
    )
    failed, pending = task("a", status=TaskStatus.FAILED), task("b")
    session.get_values = [plan_row]
    session.execute_values = [Result(rows=[failed, pending])]
    assert await repository.claim_ready_tasks("p-1", 2) == []
    assert pending.status == TaskStatus.CANCELLED.value

    session.execute_values = [Result(rowcount=1)]
    assert await repository.retry_task("a") == {"task_id": "a"}
    session.execute_values = [Result(rows=[failed, pending])]
    results = await repository.get_results("p-1")
    assert results["a"].status is TaskStatus.FAILED

    session.get_values = [failed, plan_row]
    session.execute_values = [Result(rows=[failed, pending])]
    execution = await repository.get_task_execution("a")
    assert execution and execution.task.task_id == "a"


@pytest.mark.asyncio
async def test_target_orchestration_repository_approval_and_stale_execution():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    session.execute_values = [Result(rowcount=1), Result(rowcount=1)]
    assert await repository.set_task_waiting_approval("a", 1, {"approval_id": "x"})
    assert await repository.set_task_running("a", 1)

    session.execute_values = [Result(scalar=None)]
    assert (await repository.finish_task("a", 99, status=TaskStatus.DONE.value))["accepted"] is False
