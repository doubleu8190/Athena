from __future__ import annotations

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
from infrastructure.persistence.postgres.models import OrchestrationOutboxModel
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
