"""PostgreSQL acceptance for atomic initial Plan and Round persistence."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from itertools import count
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.orchestration import (
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    PlanEdge,
    PlanRevision,
    PlanStatus,
    ReviewAction,
    ReviewDecision,
    RoundStatus,
    Task,
    TaskStatus,
    WorkerTaskTerminalEvent,
)
from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    OrchestrationInboxModel,
    OrchestrationCommandModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    RootResumeJobModel,
    ProjectModel,
    SessionModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from infrastructure.persistence.postgres.repositories.orchestration_command_repository import (
    OrchestrationCommandConflict,
    PostgresOrchestrationCommandRepository,
)
from infrastructure.persistence.postgres.repositories.orchestration_event_repository import (
    PostgresOrchestrationEventRepository,
)
from infrastructure.persistence.postgres.repositories.root_resume_job_repository import (
    PostgresRootResumeJobRepository,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)
from workers.root_resume_dispatcher import RootResumeDispatcher


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_round_barrier_holds_dependent_task_and_resumes_root_once() -> None:
    """A's completion cannot release C before B closes the fixed Round."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"round_barrier_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    now = datetime.now(timezone.utc)
    run_id = f"run-{uuid4().hex}"
    session_id = f"session-{run_id}"
    plan_id = f"plan-{run_id}"

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        tables = [
            ProjectModel.__table__,
            SessionModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            OrchestrationCommandModel.__table__,
            OrchestrationInboxModel.__table__,
            OrchestrationOutboxModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
            RootResumeJobModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session, session.begin():
            session.add(ProjectModel(
                id=f"project-{run_id}", name="Plan submission",
                created_at=now.isoformat(), updated_at=now.isoformat(),
            ))
            session.add(SessionModel(
                id=session_id, project_id=f"project-{run_id}", title="Plan submission",
                active_run_id=run_id, last_run_sequence=1,
                created_at=now.isoformat(), updated_at=now.isoformat(),
                last_activity_at=now.isoformat(),
            ))
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    message_id="message:" + str(run_id),
                    sequence=1,
                    session_id=session_id,
                    status="running",
                    root_thread_id=f"run:{run_id}",
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )

        plan = Plan(
            plan_id=plan_id,
            goal="Complete A and B before Root may review and release C",
            task_ids=("task-a", "task-b", "task-c"),
            edges=(PlanEdge("task-a", "task-c"),),
            max_parallelism=3,
            session_id=session_id,
            run_id=run_id,
        )
        tasks = [
            Task(task_id=task_id, title=task_id, objective=f"Complete {task_id}")
            for task_id in plan.task_ids
        ]
        submit = OrchestrationCommand(
            message_id=f"submit:{run_id}",
            command_type=OrchestrationCommandType.SUBMIT_PLAN,
            idempotency_key=f"submit:{run_id}",
            correlation_id=run_id,
            occurred_at=now,
            plan_id=plan_id,
            scope_key=run_id,
            plan_version=1,
        )
        ids = count()
        commands = PostgresOrchestrationCommandRepository(
            session_factory,
            id_factory=lambda: f"generated-{next(ids)}",
            clock=lambda: now,
        )
        receipt = await commands.submit_plan(submit, plan, tasks)
        assert set(receipt.task_ids) == {"task-a", "task-b"}
        async with session_factory() as session:
            created_event = await session.scalar(
                select(OrchestrationOutboxModel).where(
                    OrchestrationOutboxModel.dedupe_key
                    == f"plan:{plan_id}:created"
                )
            )
        assert created_event is not None
        assert created_event.event_type == "PlanCreated"
        assert created_event.payload_json["task_ids"] == list(plan.task_ids)
        assert created_event.payload_json["tasks"][0]["task_id"] == "task-a"
        round_id = receipt.round_id

        worker_jobs = PostgresWorkerJobRepository(
            session_factory, clock=lambda: now
        )
        first_lease = await worker_jobs.claim_next("worker-a", lease_seconds=60)
        second_lease = await worker_jobs.claim_next("worker-b", lease_seconds=60)
        assert second_lease is not None
        assert first_lease is not None
        leases = [first_lease, second_lease]
        by_task = {lease.task_id: lease for lease in leases if lease is not None}
        assert set(by_task) == {"task-a", "task-b"}

        events = PostgresOrchestrationEventRepository(
            session_factory,
            clock=lambda: now,
            id_factory=lambda: f"event-{uuid4().hex}",
        )

        def terminal_event(task_id: str) -> WorkerTaskTerminalEvent:
            lease = by_task[task_id]
            return WorkerTaskTerminalEvent(
                message_id=f"terminal:{task_id}:{run_id}",
                occurred_at=now,
                worker_id=lease.worker_id,
                run_id=run_id,
                plan_id=plan_id,
                plan_version=lease.plan_version,
                round_id=round_id,
                task_id=task_id,
                execution_id=lease.execution_id,
                fencing_token=lease.fencing_token,
                cancellation_epoch=lease.cancellation_epoch,
                status=TaskStatus.COMPLETED,
                result={"output": {"task": task_id}},
            )

        event_a = terminal_event("task-a")
        event_b = terminal_event("task-b")
        receipt_a = await events.apply_worker_terminal_event(event_a)
        assert receipt_a.accepted is True
        assert receipt_a.round_completed is False
        async with session_factory() as session:
            round_row = await session.get(PlanRoundModel, round_id)
            plan_row = await session.get(AgentPlanModel, plan_id)
            round_complete_count = await session.scalar(
                select(func.count()).select_from(OrchestrationOutboxModel).where(
                    OrchestrationOutboxModel.event_type == "RoundCompleted"
                )
            )
            jobs_before_barrier = await session.scalar(
                select(func.count()).select_from(WorkerJobModel)
            )
            c_round_task = await session.get(RoundTaskModel, (round_id, "task-c"))
        assert round_row is not None and round_row.terminal_task_count == 1
        assert plan_row is not None and plan_row.status == PlanStatus.RUNNING.value
        assert round_complete_count == 0
        assert jobs_before_barrier == 2
        assert c_round_task is None

        receipt_b = await events.apply_worker_terminal_event(event_b)
        assert receipt_b.accepted is True
        assert receipt_b.round_completed is True
        duplicate_b = await events.apply_worker_terminal_event(event_b)
        assert duplicate_b.deduplicated is True

        async with session_factory() as session:
            round_row = await session.get(PlanRoundModel, round_id)
            plan_row = await session.get(AgentPlanModel, plan_id)
            run_row = await session.get(AgentRunModel, run_id)
            c_task = await session.get(AgentTaskModel, "task-c")
            c_round_task = await session.get(RoundTaskModel, (round_id, "task-c"))
            worker_job_count = await session.scalar(
                select(func.count()).select_from(WorkerJobModel)
            )
            round_complete_count = await session.scalar(
                select(func.count()).select_from(OrchestrationOutboxModel).where(
                    OrchestrationOutboxModel.event_type == "RoundCompleted"
                )
            )
            inbox_count = await session.scalar(
                select(func.count()).select_from(OrchestrationInboxModel)
            )
            resume_jobs = (
                await session.execute(select(RootResumeJobModel))
            ).scalars().all()
        assert round_row is not None and round_row.terminal_task_count == 2
        assert round_row.status == RoundStatus.COMPLETED.value
        assert plan_row is not None and plan_row.status == PlanStatus.WAITING_REVIEW.value
        assert run_row is not None
        assert run_row.root_wait_generation == 1
        assert run_row.root_wait_interrupt_id is None
        assert c_task is not None and c_task.status == TaskStatus.PENDING.value
        assert c_round_task is None
        assert worker_job_count == 2
        assert round_complete_count == 1
        assert inbox_count == 2
        assert len(resume_jobs) == 1
        assert resume_jobs[0].status == "pending"

        resume_jobs_repo = PostgresRootResumeJobRepository(
            session_factory, clock=lambda: now
        )
        early_claim = await resume_jobs_repo.claim_next("root-app", lease_seconds=60)
        assert early_claim is not None
        assert early_claim.interrupt_id is None
        assert await resume_jobs_repo.retry(early_claim, delay_seconds=0)
        assert await PostgresRunRepository(session_factory).arm_root_wait(
            run_id,
            plan_id=plan_id,
            round_id=round_id,
            plan_version=1,
            interrupt_id="interrupt-root-review",
            checkpoint_id="checkpoint-root-review",
        )
        ready_claim = await resume_jobs_repo.claim_next("root-app", lease_seconds=60)
        assert ready_claim is not None
        assert ready_claim.interrupt_id == "interrupt-root-review"
        assert ready_claim.checkpoint_id == "checkpoint-root-review"

        class Graph:
            async def has_pending_round_wait(self, **identity):
                return identity == {
                    "run_id": run_id,
                    "plan_id": plan_id,
                    "round_id": round_id,
                    "plan_version": 1,
                    "interrupt_id": "interrupt-root-review",
                    "checkpoint_id": "checkpoint-root-review",
                }

        class Execution:
            def __init__(self) -> None:
                self.calls = 0

            async def execute(self, _command):
                self.calls += 1
                return {"content": "review complete"}

        class Runs:
            async def get(self, _run_id):
                return type("Run", (), {"status": "completed"})()

            async def acquire_execution_lease(
                self, _run_id, *, owner, lease_seconds
            ):
                return 7

            async def renew_execution_lease(
                self, _run_id, *, owner, generation, lease_seconds
            ):
                return True

            async def release_execution_lease(
                self, _run_id, *, owner, generation
            ):
                return True

        execution = Execution()
        await RootResumeDispatcher(
            resume_jobs_repo,
            Graph(),
            execution,
            Runs(),
            worker_id="root-app",
        )._resume(ready_claim)
        assert execution.calls == 1

        review_ids = count()
        review_repository = PostgresOrchestrationCommandRepository(
            session_factory,
            id_factory=lambda: f"review-{next(review_ids)}",
            clock=lambda: now,
        )
        review_command = OrchestrationCommand(
            message_id=f"continue:{run_id}:round-1",
            command_type=OrchestrationCommandType.CONTINUE_PLAN,
            idempotency_key=f"continue:{run_id}:round-1",
            correlation_id=run_id,
            occurred_at=now,
            plan_id=plan_id,
            scope_key=run_id,
            plan_version=1,
            round_id=round_id,
        )
        continue_decision = ReviewDecision(
            ReviewAction.CONTINUE, "Continue after reviewing the fixed Round."
        )
        continue_receipts = await asyncio.gather(
            *(
                review_repository.apply_review_decision(
                    review_command, continue_decision
                )
                for _ in range(10)
            )
        )
        assert sum(not receipt.deduplicated for receipt in continue_receipts) == 1
        assert sum(receipt.deduplicated for receipt in continue_receipts) == 9
        assert len({receipt.round_id for receipt in continue_receipts}) == 1
        assert continue_receipts[0].task_ids == ("task-c",)
        with pytest.raises(OrchestrationCommandConflict):
            await review_repository.apply_review_decision(
                review_command,
                ReviewDecision(
                    ReviewAction.CONTINUE,
                    "The same key must not accept different review content.",
                ),
            )
        async with session_factory() as session:
            rounds = (
                await session.execute(select(PlanRoundModel))
            ).scalars().all()
            c_round_task = await session.get(
                RoundTaskModel, (continue_receipts[0].round_id, "task-c")
            )
            c_jobs = await session.scalar(
                select(func.count())
                .select_from(WorkerJobModel)
                .join(
                    TaskExecutionModel,
                    WorkerJobModel.execution_id == TaskExecutionModel.execution_id,
                )
                .where(TaskExecutionModel.task_id == "task-c")
            )
        assert len(rounds) == 2
        assert c_round_task is not None and c_round_task.status == "pending"
        assert c_jobs == 1
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submit_and_replan_preserve_completed_results_in_postgres() -> None:
    """Initial persistence and a later replan preserve immutable history/results."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"plan_submit_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    now = datetime.now(timezone.utc)
    run_id = f"run-{uuid4().hex}"
    session_id = f"session-{run_id}"
    plan_id = f"plan-{run_id}"

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        tables = [
            ProjectModel.__table__,
            SessionModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            OrchestrationCommandModel.__table__,
            OrchestrationOutboxModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session, session.begin():
            session.add(ProjectModel(
                id=f"project-{run_id}", name="Plan submission",
                created_at=now.isoformat(), updated_at=now.isoformat(),
            ))
            session.add(SessionModel(
                id=session_id, project_id=f"project-{run_id}", title="Plan submission",
                active_run_id=run_id, last_run_sequence=1,
                created_at=now.isoformat(), updated_at=now.isoformat(),
                last_activity_at=now.isoformat(),
            ))
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    message_id="message:" + str(run_id),
                    sequence=1,
                    session_id=session_id,
                    status="running",
                    root_thread_id=f"run:{run_id}",
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )

        plan = Plan(
            plan_id=plan_id,
            goal="Answer two independent questions and combine the result",
            task_ids=("task-a", "task-b", "task-merge"),
            edges=(PlanEdge("task-a", "task-merge"), PlanEdge("task-b", "task-merge")),
            max_parallelism=2,
            session_id=session_id,
            run_id=run_id,
        )
        tasks = [
            Task(task_id=task_id, title=task_id, objective=f"Complete {task_id}")
            for task_id in plan.task_ids
        ]
        command = OrchestrationCommand(
            message_id=f"submit:{run_id}",
            command_type=OrchestrationCommandType.SUBMIT_PLAN,
            idempotency_key=f"submit-plan:{run_id}",
            correlation_id=run_id,
            occurred_at=now,
            plan_id=plan_id,
            scope_key=run_id,
            plan_version=1,
        )
        ids = count()
        repository = PostgresOrchestrationCommandRepository(
            session_factory, id_factory=lambda: f"generated-{next(ids)}"
        )

        receipt = await repository.submit_plan(command, plan, tasks)

        assert receipt.plan_id == plan_id
        assert receipt.plan_version == 1
        assert receipt.task_ids == ("task-a", "task-b")
        async with session_factory() as session:
            assert await session.scalar(
                select(func.count()).select_from(PlanVersionModel)
            ) == 1
            assert await session.scalar(
                select(func.count()).select_from(PlanRoundModel)
            ) == 1
            assert await session.scalar(
                select(func.count()).select_from(RoundTaskModel)
            ) == 2
            assert await session.scalar(
                select(func.count()).select_from(TaskExecutionModel)
            ) == 2
            assert await session.scalar(
                select(func.count()).select_from(WorkerJobModel)
            ) == 2
            persisted_run = await session.get(AgentRunModel, run_id)
            assert persisted_run is not None
            assert persisted_run.status == "running"

        async with session_factory() as session, session.begin():
            persisted_plan = await session.get(AgentPlanModel, plan_id)
            persisted_round = await session.get(PlanRoundModel, receipt.round_id)
            assert persisted_plan is not None
            assert persisted_round is not None
            persisted_plan.status = PlanStatus.WAITING_REVIEW.value
            persisted_round.status = RoundStatus.COMPLETED.value
            persisted_round.terminal_task_count = 2
            persisted_round.completed_at = now
            for task_id in ("task-a", "task-b"):
                task = await session.get(AgentTaskModel, task_id)
                round_task = await session.get(
                    RoundTaskModel, (receipt.round_id, task_id)
                )
                assert task is not None
                assert round_task is not None
                task.status = TaskStatus.COMPLETED.value
                round_task.status = TaskStatus.COMPLETED.value
                if task_id == "task-a":
                    task.output_json = '{"answer":"preserve this result"}'

        revision = PlanRevision(
            base_version=1,
            keep_task_ids=("task-a",),
            cancel_task_ids=("task-b", "task-merge"),
            new_tasks=(
                {
                    "task_id": "task-verify",
                    "title": "Verify retained result",
                    "objective": "Verify the result from task-a",
                    "depends_on": ["task-a"],
                },
            ),
        )
        review_command = OrchestrationCommand(
            message_id=f"review:{run_id}:v1",
            command_type=OrchestrationCommandType.REVISE_PLAN,
            idempotency_key=f"review:{plan_id}:round-1",
            correlation_id=run_id,
            occurred_at=now,
            plan_id=plan_id,
            scope_key=run_id,
            plan_version=1,
            round_id=receipt.round_id,
        )
        revised = await repository.apply_review_decision(
            review_command,
            ReviewDecision(
                ReviewAction.REPLAN,
                "Retain the completed result and add a verification task.",
                revision=revision,
            ),
        )

        new_task_id = "task-verify:" + run_id
        assert revised.plan_version == 2
        assert revised.round_id != receipt.round_id
        assert revised.task_ids == (new_task_id,)
        async with session_factory() as session:
            plan_versions = (
                await session.execute(
                    select(PlanVersionModel)
                    .where(PlanVersionModel.plan_id == plan_id)
                    .order_by(PlanVersionModel.version)
                )
            ).scalars().all()
            assert [version.version for version in plan_versions] == [1, 2]
            assert plan_versions[0].dag_json["version"] == 1
            assert [node["task_id"] for node in plan_versions[1].dag_json["nodes"]] == [
                "task-a",
                new_task_id,
            ]
            persisted_plan = await session.get(AgentPlanModel, plan_id)
            assert persisted_plan is not None
            assert persisted_plan.current_version == 2
            assert persisted_plan.status == PlanStatus.RUNNING.value
            retained = await session.get(AgentTaskModel, "task-a")
            removed_pending = await session.get(AgentTaskModel, "task-merge")
            created = await session.get(AgentTaskModel, new_task_id)
            assert retained is not None
            assert retained.status == TaskStatus.COMPLETED.value
            assert retained.output_json == '{"answer":"preserve this result"}'
            assert removed_pending is not None
            assert removed_pending.status == TaskStatus.CANCELLED.value
            assert created is not None
            assert created.status == TaskStatus.PENDING.value
            next_round = await session.get(PlanRoundModel, revised.round_id)
            assert next_round is not None
            assert next_round.plan_version == 2
            assert next_round.round_number == 2
            assert next_round.status == RoundStatus.RUNNING.value
            assert await session.scalar(
                select(func.count()).select_from(WorkerJobModel).where(
                    WorkerJobModel.execution_id.in_(
                        select(TaskExecutionModel.execution_id).where(
                            TaskExecutionModel.round_id == revised.round_id
                        )
                    )
                )
            ) == 1
    finally:
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await test_engine.dispose()
        await base_engine.dispose()
