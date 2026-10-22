from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from itertools import count
from types import SimpleNamespace

import pytest

from domain.orchestration import (
    OrchestrationCommand,
    OrchestrationCommandType,
    PlanRevision,
    PlanStatus,
    ReviewAction,
    ReviewDecision,
    TaskStatus,
)
from infrastructure.persistence.postgres.models import (
    OrchestrationCommandModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from infrastructure.persistence.postgres.repositories.orchestration_command_repository import (
    OrchestrationCommandConflict,
    PlanCommandRejected,
    PostgresOrchestrationCommandRepository,
    _review_command_hash,
    _receipt_document,
)


class Result:
    def __init__(self, *, scalar=None, rows=()):
        self.scalar = scalar
        self.rows = list(rows)

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows


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

    async def get(self, *_args, **_kwargs):
        return self.get_values.pop(0)

    async def execute(self, _statement):
        return self.execute_values.pop(0)


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


def command(command_type=OrchestrationCommandType.CONTINUE_PLAN):
    return OrchestrationCommand(
        message_id="message-1",
        command_type=command_type,
        idempotency_key="review:round-1",
        correlation_id="run-1",
        occurred_at=datetime.now(timezone.utc),
        plan_id="plan-1",
        scope_key="run-1",
        plan_version=1,
        round_id="round-1",
    )


def persisted_chain(*, current_version=1):
    run = SimpleNamespace(run_id="run-1", status="running")
    plan = SimpleNamespace(
        plan_id="plan-1",
        run_id="run-1",
        session_id="session-1",
        user_goal="Finish the report",
        max_parallelism=4,
        status=PlanStatus.WAITING_REVIEW.value,
        current_version=current_version,
        active_round_id="round-1",
        plan_json="{}",
        updated_at=None,
    )
    round_row = SimpleNamespace(
        round_id="round-1",
        plan_id="plan-1",
        plan_version=current_version,
        round_number=1,
        status="completed",
        expected_task_count=1,
        terminal_task_count=1,
    )
    return run, plan, round_row


def task_row(task_id, status, *, generation=1):
    return SimpleNamespace(
        task_id=task_id,
        plan_id="plan-1",
        run_id="run-1",
        title=task_id.upper(),
        objective=f"Do {task_id}",
        input_json="{}",
        expected_output_json="{}",
        allowed_tools_json="[]",
        worker_type="general",
        status=status,
        execution_generation=generation,
        worker_thread_id=None,
        active_execution_id=None,
        error_json=None,
        updated_at=None,
    )


def version_document():
    return {
        "plan_id": "plan-1",
        "version": 1,
        "goal": "Finish the report",
        "max_parallelism": 4,
        "nodes": [
            {
                "task_id": "a",
                "title": "A",
                "objective": "Do a",
                "input": {},
                "expected_output": {},
                "allowed_tools": [],
                "worker_type": "general",
            },
            {
                "task_id": "b",
                "title": "B",
                "objective": "Do b",
                "input": {},
                "expected_output": {},
                "allowed_tools": [],
                "worker_type": "general",
            },
        ],
        "edges": [{"from": "a", "to": "b"}],
    }


def setup_continue_session(session):
    run, plan, round_row = persisted_chain()
    version = SimpleNamespace(dag_json=version_document())
    a, b = task_row("a", TaskStatus.COMPLETED.value), task_row(
        "b", TaskStatus.PENDING.value, generation=1
    )
    session.get_values = [run, plan, round_row, version]
    session.execute_values = [Result(scalar=None), Result(rows=[a, b])]
    return plan, round_row, a, b


@pytest.mark.asyncio
async def test_continue_decision_freezes_all_ready_tasks_and_is_idempotent():
    session = Session()
    id_counter = count()
    repository = PostgresOrchestrationCommandRepository(
        factory(session), id_factory=lambda: f"generated-{next(id_counter)}"
    )
    plan, _round, _a, task_b = setup_continue_session(session)
    review_command = command()
    decision = ReviewDecision(ReviewAction.CONTINUE, "Continue with the draft.")

    receipt = await repository.apply_review_decision(review_command, decision)

    assert receipt.plan_version == 1
    assert receipt.round_id == "generated-0"
    assert receipt.task_ids == ("b",)
    assert plan.status == PlanStatus.RUNNING.value
    assert plan.active_round_id == receipt.round_id
    assert task_b.execution_generation == 1
    assert task_b.active_execution_id == "generated-1"
    assert any(isinstance(item, PlanRoundModel) for item in session.added)
    assert any(isinstance(item, RoundTaskModel) for item in session.added)
    assert any(isinstance(item, TaskExecutionModel) for item in session.added)
    assert any(isinstance(item, WorkerJobModel) for item in session.added)
    assert any(isinstance(item, OrchestrationOutboxModel) for item in session.added)
    command_row = next(
        item
        for item in session.added
        if isinstance(item, OrchestrationCommandModel)
    )

    retry_session = Session()
    retry_session.get_values = [SimpleNamespace(run_id="run-1", status="completed")]
    retry_session.execute_values = [
        Result(
            scalar=SimpleNamespace(
                payload_hash=_review_command_hash(review_command, decision),
                result_json=_receipt_document(receipt),
            )
        )
    ]
    deduplicated = await PostgresOrchestrationCommandRepository(
        factory(retry_session)
    ).apply_review_decision(review_command, decision)

    assert deduplicated.deduplicated is True
    assert deduplicated.round_id == receipt.round_id
    assert command_row.idempotency_key == review_command.idempotency_key


@pytest.mark.asyncio
async def test_replan_writes_new_immutable_version_and_reuses_completed_task():
    session = Session()
    id_counter = count()
    repository = PostgresOrchestrationCommandRepository(
        factory(session), id_factory=lambda: f"generated-{next(id_counter)}"
    )
    run, plan, round_row = persisted_chain()
    version = SimpleNamespace(dag_json=version_document())
    a = task_row("a", TaskStatus.COMPLETED.value)
    b = task_row("b", TaskStatus.PENDING.value, generation=0)
    session.get_values = [run, plan, round_row, version]
    session.execute_values = [Result(scalar=None), Result(rows=[a, b])]
    review_command = command(OrchestrationCommandType.REVISE_PLAN)
    decision = ReviewDecision(
        ReviewAction.REPLAN,
        "Replace the draft task with a verification task.",
        revision=PlanRevision(
            base_version=1,
            keep_task_ids=("a",),
            cancel_task_ids=("b",),
            new_tasks=(
                {
                    "task_id": "c",
                    "title": "Verify",
                    "instruction": "Verify the report",
                    "depends_on": ["a"],
                },
            ),
        ),
    )

    receipt = await repository.apply_review_decision(review_command, decision)

    assert receipt.plan_version == 2
    assert receipt.task_ids == ("c:run-1",)
    assert plan.current_version == 2
    assert plan.status == PlanStatus.RUNNING.value
    assert b.status == TaskStatus.CANCELLED.value
    new_version = next(
        item
        for item in session.added
        if isinstance(item, PlanVersionModel)
    )
    assert new_version.based_on_round_id == "round-1"
    assert [node["task_id"] for node in new_version.dag_json["nodes"]] == [
        "a",
        "c:run-1",
    ]
    assert new_version.dag_json["edges"] == [
        {"from": "a", "to": "c:run-1"}
    ]
    assert any(isinstance(item, WorkerJobModel) for item in session.added)


@pytest.mark.asyncio
async def test_finish_decision_records_terminal_plan_outbox():
    session = Session()
    repository = PostgresOrchestrationCommandRepository(factory(session))
    run, plan, round_row = persisted_chain()
    version = SimpleNamespace(dag_json=version_document())
    session.get_values = [run, plan, round_row, version]
    session.execute_values = [Result(scalar=None), Result(rows=[])]
    review_command = command(OrchestrationCommandType.FINISH_PLAN)
    decision = ReviewDecision(
        ReviewAction.FINISH,
        "The result meets the goal.",
        final_status=PlanStatus.COMPLETED.value,
    )

    receipt = await repository.apply_review_decision(review_command, decision)

    assert receipt.task_ids == ()
    assert plan.status == PlanStatus.COMPLETED.value
    event = next(
        item
        for item in session.added
        if isinstance(item, OrchestrationOutboxModel)
    )
    assert event.event_type == "PlanCompleted"
    assert event.payload_json["reason"] == decision.reason


@pytest.mark.asyncio
async def test_review_command_rejects_reuse_of_idempotency_key_with_new_decision():
    session = Session()
    review_command = command()
    session.get_values = [SimpleNamespace(run_id="run-1", status="running")]
    session.execute_values = [
        Result(scalar=SimpleNamespace(payload_hash="different", result_json={}))
    ]
    decision = ReviewDecision(ReviewAction.CONTINUE, "continue")

    with pytest.raises(OrchestrationCommandConflict):
        await PostgresOrchestrationCommandRepository(factory(session)).apply_review_decision(
            review_command, decision
        )


@pytest.mark.asyncio
async def test_review_command_rejects_a_stale_plan_version_before_reading_tasks():
    session = Session()
    run, plan, _round = persisted_chain(current_version=2)
    session.get_values = [run, plan]
    session.execute_values = [Result(scalar=None)]

    with pytest.raises(PlanCommandRejected, match="review_round_identity_stale"):
        await PostgresOrchestrationCommandRepository(factory(session)).apply_review_decision(
            command(), ReviewDecision(ReviewAction.CONTINUE, "continue")
        )

    assert session.execute_values == []
