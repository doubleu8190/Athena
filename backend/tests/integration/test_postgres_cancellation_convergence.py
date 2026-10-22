"""PostgreSQL acceptance for Run cancellation and Worker lease convergence."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.orchestration import RunCancellationReceipt, Task, WorkerJobLease
from domain.runs import RunStatus
from infrastructure.persistence.postgres.models import (
    AgentCommandModel,
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RootResumeJobModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_cancellation_converges_after_expired_worker_lease() -> None:
    """Cancel fences active work, cancels approvals, then reaps an expired lease."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"run_cancel_test_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    run_id = f"run-cancel-{uuid4().hex}"
    session_id = f"session-{run_id}"
    plan_id = f"plan-{run_id}"
    round_id = f"round-{run_id}"
    running_task_id = f"task-running-{run_id}"
    waiting_task_id = f"task-waiting-{run_id}"
    running_execution_id = f"execution-running-{run_id}"
    waiting_execution_id = f"execution-waiting-{run_id}"
    job_id = f"job-{run_id}"
    approval_id = f"approval-{run_id}"
    now = datetime.now(timezone.utc)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        required_tables = [
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            AgentCommandModel.__table__,
            ApprovalRecordModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
            RootResumeJobModel.__table__,
            OrchestrationOutboxModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=required_tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    AgentRunModel(
                        run_id=run_id,
                        session_id=session_id,
                        status="running",
                        root_thread_id=f"thread-{run_id}",
                        cancellation_epoch=0,
                        cancellation_external_effect_status="none",
                        created_at=now.isoformat(),
                        updated_at=now.isoformat(),
                    )
                )
                session.add(
                    AgentPlanModel(
                        plan_id=plan_id,
                        run_id=run_id,
                        session_id=session_id,
                        status="running",
                        user_goal="Verify cancellation convergence",
                        max_parallelism=1,
                        plan_json='{"nodes":[],"edges":[]}',
                        current_version=1,
                        active_round_id=round_id,
                        cancellation_epoch=0,
                        created_at=now.isoformat(),
                        updated_at=now.isoformat(),
                    )
                )
                await session.flush()
                session.add(
                    PlanVersionModel(
                        plan_id=plan_id,
                        version=1,
                        dag_json={"nodes": [], "edges": []},
                        created_at=now,
                    )
                )
                await session.flush()
                session.add(
                    PlanRoundModel(
                        round_id=round_id,
                        plan_id=plan_id,
                        plan_version=1,
                        round_number=1,
                        status="running",
                        expected_task_count=2,
                        terminal_task_count=0,
                        created_at=now,
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        AgentTaskModel(
                            task_id=running_task_id,
                            plan_id=plan_id,
                            run_id=run_id,
                            title="Active work",
                            objective="Simulate an unresponsive Worker",
                            status="running",
                            execution_generation=1,
                            active_execution_id=running_execution_id,
                            worker_thread_id=f"thread-{running_execution_id}",
                            created_at=now.isoformat(),
                            updated_at=now.isoformat(),
                        ),
                        AgentTaskModel(
                            task_id=waiting_task_id,
                            plan_id=plan_id,
                            run_id=run_id,
                            title="Waiting approval",
                            objective="Simulate a pending approval",
                            status="waiting_approval",
                            execution_generation=1,
                            active_execution_id=waiting_execution_id,
                            worker_thread_id=f"thread-{waiting_execution_id}",
                            created_at=now.isoformat(),
                            updated_at=now.isoformat(),
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        RoundTaskModel(
                            round_id=round_id,
                            task_id=running_task_id,
                            status="running",
                            active_execution_id=running_execution_id,
                        ),
                        RoundTaskModel(
                            round_id=round_id,
                            task_id=waiting_task_id,
                            status="waiting_approval",
                            active_execution_id=waiting_execution_id,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        TaskExecutionModel(
                            execution_id=running_execution_id,
                            round_id=round_id,
                            task_id=running_task_id,
                            attempt=1,
                            worker_thread_id=f"thread-{running_execution_id}",
                            status="running",
                            lease_owner="worker-old",
                            lease_until=now - timedelta(seconds=5),
                            fencing_token=7,
                            cancellation_epoch=0,
                            created_at=now,
                            updated_at=now,
                        ),
                        TaskExecutionModel(
                            execution_id=waiting_execution_id,
                            round_id=round_id,
                            task_id=waiting_task_id,
                            attempt=1,
                            worker_thread_id=f"thread-{waiting_execution_id}",
                            status="waiting_approval",
                            fencing_token=2,
                            cancellation_epoch=0,
                            created_at=now,
                            updated_at=now,
                        ),
                    ]
                )
                session.add(
                    WorkerJobModel(
                        job_id=job_id,
                        dedupe_key=f"execute:{running_execution_id}",
                        job_type="execute",
                        execution_id=running_execution_id,
                        status="running",
                        attempt=1,
                        lease_owner="worker-old",
                        lease_until=now - timedelta(seconds=5),
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    ApprovalRecordModel(
                        approval_id=approval_id,
                        session_id=session_id,
                        run_id=run_id,
                        plan_id=plan_id,
                        task_id=waiting_task_id,
                        execution_id=waiting_execution_id,
                        worker_thread_id=f"thread-{waiting_execution_id}",
                        interrupt_id=f"interrupt-{approval_id}",
                        tool_call_id=f"call-{approval_id}",
                        tool_name="write_file",
                        arguments_json="{}",
                        risk_level="high",
                        status="pending",
                        created_at=now.isoformat(),
                    )
                )

        run_repository = PostgresRunRepository(session_factory)
        accepted = await run_repository.cancel(run_id)
        assert accepted is not None and accepted.changed
        assert accepted.status == RunStatus.CANCELLING
        assert accepted.external_effect_status == "unknown"

        async with session_factory() as session:
            async with session.begin():
                outbox_before_retry = await session.scalar(
                    select(func.count())
                    .select_from(OrchestrationOutboxModel)
                    .where(OrchestrationOutboxModel.correlation_id == run_id)
                )
        repeated = await run_repository.cancel(run_id)
        assert repeated is not None and not repeated.changed
        assert repeated.status == RunStatus.CANCELLING
        assert repeated.external_effect_status == "unknown"
        async with session_factory() as session:
            outbox_after_retry = await session.scalar(
                select(func.count())
                .select_from(OrchestrationOutboxModel)
                .where(OrchestrationOutboxModel.correlation_id == run_id)
            )
        assert outbox_after_retry == outbox_before_retry

        repository = PostgresWorkerJobRepository(
            session_factory, clock=lambda: now + timedelta(seconds=1)
        )
        receipts = await repository.reconcile_cancellations()
        assert receipts == [
            RunCancellationReceipt(
                run_id, session_id, plan_id, 1, "unknown"
            )
        ]
        stale_lease = WorkerJobLease(
            job_id=job_id,
            worker_id="worker-old",
            run_id=run_id,
            plan_id=plan_id,
            round_id=round_id,
            plan_version=1,
            task_id=running_task_id,
            execution_id=running_execution_id,
            session_id=session_id,
            attempt=1,
            worker_thread_id=f"thread-{running_execution_id}",
            fencing_token=7,
            cancellation_epoch=0,
            lease_until=now - timedelta(seconds=5),
            task=Task(
                task_id=running_task_id,
                title="Active work",
                objective="Simulate an unresponsive Worker",
            ),
        )
        assert not await repository.acknowledge_cancelled(stale_lease)
        terminal_repeat = await run_repository.cancel(run_id)
        assert terminal_repeat is not None and not terminal_repeat.changed
        assert terminal_repeat.status == RunStatus.CANCELLED
        assert terminal_repeat.external_effect_status == "unknown"

        async with session_factory() as session:
            run = await session.get(AgentRunModel, run_id)
            plan = await session.get(AgentPlanModel, plan_id)
            round_row = await session.get(PlanRoundModel, round_id)
            running_task = await session.get(AgentTaskModel, running_task_id)
            waiting_task = await session.get(AgentTaskModel, waiting_task_id)
            execution = await session.get(TaskExecutionModel, running_execution_id)
            job = await session.get(WorkerJobModel, job_id)
            approval = await session.get(ApprovalRecordModel, approval_id)
            events = (
                await session.execute(
                    select(OrchestrationOutboxModel).where(
                        OrchestrationOutboxModel.correlation_id == run_id
                    )
                )
            ).scalars().all()
            event_count = len(events)

        assert run is not None and run.status == "cancelled"
        assert run.cancellation_epoch == 1
        assert run.cancellation_external_effect_status == "unknown"
        assert plan is not None and plan.status == "cancelled"
        assert round_row is not None and round_row.status == "cancelled"
        assert round_row.terminal_task_count == round_row.expected_task_count == 2
        assert running_task is not None and running_task.status == "cancelled"
        assert waiting_task is not None and waiting_task.status == "cancelled"
        assert execution is not None and execution.status == "cancelled"
        assert job is not None and job.status == "cancelled"
        assert approval is not None and approval.status == "cancelled"
        assert {event.event_type for event in events} >= {
            "PlanCancelling",
            "RunCancelling",
            "TaskCancelled",
            "ApprovalCancelled",
            "PlanCancelled",
            "RunCancelled",
        }
        assert len([event for event in events if event.event_type == "RunCancelling"]) == 1
        assert len([event for event in events if event.event_type == "RunCancelled"]) == 1
        assert event_count == 7
        assert len([event for event in events if event.event_type == "TaskCancelled"]) == 2
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
