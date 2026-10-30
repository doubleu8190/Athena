"""PostgreSQL acceptance for expired Worker lease recovery and fencing."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    ExecutionLeaseModel,
    OrchestrationCommandModel,
    OrchestrationOutboxModel,
    OrchestrationInboxModel,
    ProjectModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    RootResumeJobModel,
    TaskExecutionModel,
    WorkerJobModel,
    SessionModel,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)
from infrastructure.persistence.postgres.repositories.orchestration_command_repository import (
    PostgresOrchestrationCommandRepository,
)
from infrastructure.persistence.postgres.repositories.orchestration_event_repository import (
    PostgresOrchestrationEventRepository,
)
from domain.leases import LeaseGrant
from tests.fakes.leases import acquire_postgres_lease
from domain.orchestration import (
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    Task,
    TaskStatus,
    WorkerJobLease,
    WorkerTaskTerminalEvent,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_worker_lease_is_reclaimed_once_with_new_fence() -> None:
    """并发接管过期租约时只授予一个 Worker，并隔离旧持有者。

    该测试需要可连接且允许创建临时 schema 的 PostgreSQL 测试库；
    测试结束会删除 schema。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_lease_test_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    schema_url = base_url.update_query_dict(
        {"options": f"-c search_path={schema}"}
    )
    test_engine = create_async_engine(schema_url)
    schema_created = False
    now = datetime.now(timezone.utc)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        required_tables = [

            ExecutionLeaseModel.__table__,
            ProjectModel.__table__,
            SessionModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
            OrchestrationOutboxModel.__table__,
            OrchestrationInboxModel.__table__,
            RootResumeJobModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=required_tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        await _seed_expired_worker_job(session_factory, now=now)
        repository = PostgresWorkerJobRepository(
            session_factory,
            clock=lambda: now,
        )

        first_claim, second_claim = await asyncio.gather(
            repository.claim_next("worker-a", lease_seconds=60),
            repository.claim_next("worker-b", lease_seconds=60),
        )

        claims = [claim for claim in (first_claim, second_claim) if claim is not None]
        assert len(claims) == 1
        current_lease = claims[0]
        assert current_lease.lease.fencing_token == 8
        assert current_lease.attempt == 1
        assert current_lease.worker_id in {"worker-a", "worker-b"}
        assert current_lease.task.allowed_tools == ("exec_shell",)

        previous_lease = replace(
            current_lease,
            worker_id="crashed-worker",
            lease=LeaseGrant(
                key=current_lease.lease.key,
                owner_id="crashed-worker",
                fencing_token=7,
                lease_until=now - timedelta(seconds=1),
            ),
        )
        assert (
            await repository.renew_lease(previous_lease, lease_seconds=60)
        ) is None

        async with session_factory() as session:
            execution = await session.get(TaskExecutionModel, "execution-1")
            job = await session.get(WorkerJobModel, "job-1")
            lease_row = await session.get(
                ExecutionLeaseModel, ("worker_execution", "execution-1")
            )

        assert execution is not None
        assert job is not None
        assert lease_row is not None
        assert lease_row.owner_id == current_lease.lease.owner_id
        assert lease_row.fencing_token == 8
        assert lease_row.state == "active"
        assert execution.attempt == 1
        assert job.attempt == 1
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_app_instances_respect_plan_parallelism_with_ten_ready_tasks() -> None:
    """Ten fixed Round members never exceed the persisted concurrency limit."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_parallelism_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    app_a_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    app_b_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    now = datetime.now(timezone.utc)
    run_id = f"run-{uuid4().hex}"
    plan_id = f"plan-{run_id}"
    round_id = f"round-{run_id}"
    task_ids = [f"task-{index}-{run_id}" for index in range(10)]
    next_task_id = f"task-next-{run_id}"

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        required_tables = [

            ExecutionLeaseModel.__table__,
            ProjectModel.__table__,
            SessionModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
            OrchestrationOutboxModel.__table__,
            OrchestrationInboxModel.__table__,
            RootResumeJobModel.__table__,
        ]
        async with app_a_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=required_tables
                )
            )

        app_a_sessions = async_sessionmaker(app_a_engine, expire_on_commit=False)
        app_b_sessions = async_sessionmaker(app_b_engine, expire_on_commit=False)
        async with app_a_sessions() as session, session.begin():
            session_id = f"session-{run_id}"
            project_id = f"project-{run_id}"
            session.add(ProjectModel(
                id=project_id, name="Worker parallelism",
                created_at=now, updated_at=now,
            ))
            session.add(SessionModel(
                id=session_id, project_id=project_id, title="Worker parallelism",
                active_run_id=run_id, last_run_sequence=1,
                created_at=now, updated_at=now,
                last_activity_at=now,
            ))
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    message_id="message:" + str(run_id),
                    sequence=1,
                    session_id=session_id,
                    status="running",
                    root_thread_id=f"run:{run_id}",
                    cancellation_epoch=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AgentPlanModel(
                    plan_id=plan_id,
                    run_id=run_id,
                    session_id=session_id,
                    status="running",
                    user_goal="Run no more than three tasks in parallel",
                    max_parallelism=3,
                    current_version=1,
                    active_round_id=round_id,
                    cancellation_epoch=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                PlanVersionModel(
                    plan_id=plan_id,
                    version=1,
                    dag_json={
                        "nodes": [
                            {"task_id": task_id}
                            for task_id in [*task_ids, next_task_id]
                        ],
                        "edges": [{"from": task_ids[0], "to": next_task_id}],
                    },
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
                    expected_task_count=len(task_ids),
                    terminal_task_count=0,
                    created_at=now,
                )
            )
            await session.flush()

            for index, task_id in enumerate(task_ids):
                execution_id = f"execution-{index}-{run_id}"
                thread_id = f"thread-{execution_id}"
                session.add(
                    AgentTaskModel(
                        task_id=task_id,
                        plan_id=plan_id,
                        run_id=run_id,
                        title=f"Task {index}",
                        objective=f"Complete fixed Round member {index}",
                        allowed_tools_json=[],
                        status="pending",
                        execution_generation=1,
                        active_execution_id=execution_id,
                        worker_thread_id=thread_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.flush()
            session.add(
                AgentTaskModel(
                    task_id=next_task_id,
                    plan_id=plan_id,
                    run_id=run_id,
                    title="Next layer",
                    objective="Wait until Root reviews the first fixed Round",
                    allowed_tools_json=[],
                    status="pending",
                    execution_generation=0,
                    active_execution_id=None,
                    worker_thread_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    RoundTaskModel(
                        round_id=round_id,
                        task_id=task_id,
                        status="pending",
                        active_execution_id=f"execution-{index}-{run_id}",
                    )
                    for index, task_id in enumerate(task_ids)
                ]
            )
            await session.flush()
            session.add_all(
                [
                    TaskExecutionModel(
                        execution_id=f"execution-{index}-{run_id}",
                        round_id=round_id,
                        task_id=task_id,
                        attempt=1,
                        worker_thread_id=f"thread-execution-{index}-{run_id}",
                        status="queued",
                        cancellation_epoch=0,
                        created_at=now,
                        updated_at=now,
                    )
                    for index, task_id in enumerate(task_ids)
                ]
            )
            await session.flush()
            session.add_all(
                [
                    WorkerJobModel(
                        job_id=f"job-{index}-{run_id}",
                        dedupe_key=f"execute:execution-{index}-{run_id}",
                        job_type="execute",
                        execution_id=f"execution-{index}-{run_id}",
                        payload_json={},
                        status="pending",
                        attempt=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    for index in range(len(task_ids))
                ]
            )

        repo_a = PostgresWorkerJobRepository(app_a_sessions, clock=lambda: now)
        repo_b = PostgresWorkerJobRepository(app_b_sessions, clock=lambda: now)
        race = await asyncio.gather(
            repo_a.claim_next("app-a-worker-0", lease_seconds=60),
            repo_b.claim_next("app-b-worker-0", lease_seconds=60),
        )
        leases = [lease for lease in race if lease is not None]
        for index in range(10):
            repository = repo_a if index % 2 == 0 else repo_b
            lease = await repository.claim_next(
                f"app-{index % 2}-worker-{index + 1}", lease_seconds=60
            )
            if lease is not None:
                leases.append(lease)

        assert len(leases) == 3
        assert len({lease.task_id for lease in leases}) == 3
        async with app_a_sessions() as session:
            round_row = await session.get(PlanRoundModel, round_id)
            tasks = (await session.execute(select(AgentTaskModel))).scalars().all()
            executions = (
                await session.execute(select(TaskExecutionModel))
            ).scalars().all()
            jobs = (await session.execute(select(WorkerJobModel))).scalars().all()
            round_tasks = (
                await session.execute(select(RoundTaskModel))
            ).scalars().all()
            next_task = await session.get(AgentTaskModel, next_task_id)

        assert round_row is not None
        assert round_row.expected_task_count == 10
        assert {task.task_id for task in tasks} == {*task_ids, next_task_id}
        assert sum(task.status == "running" for task in tasks) == 3
        assert sum(execution.status == "running" for execution in executions) == 3
        assert sum(job.status == "running" for job in jobs) == 3
        assert sum(job.status == "pending" for job in jobs) == 7
        assert {item.task_id for item in round_tasks} == set(task_ids)
        assert {execution.task_id for execution in executions} == set(task_ids)
        assert next_task is not None and next_task.status == "pending"
        assert next_task.active_execution_id is None
    finally:
        await app_b_engine.dispose()
        await app_a_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_terminal_commit_with_lost_receipt_is_deduplicated() -> None:
    """Worker 终态事务提交后丢失回执，重投只返回原回执、不重复推进。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_receipt_test_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    now = datetime.now(timezone.utc)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        required_tables = [

            ExecutionLeaseModel.__table__,
            ProjectModel.__table__,
            SessionModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
            OrchestrationOutboxModel.__table__,
            OrchestrationInboxModel.__table__,
            RootResumeJobModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=required_tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        await _seed_expired_worker_job(session_factory, now=now)
        worker_jobs = PostgresWorkerJobRepository(
            session_factory,
            clock=lambda: now,
        )
        lease = await worker_jobs.claim_next("recovery-worker", lease_seconds=60)
        assert lease is not None

        event = WorkerTaskTerminalEvent(
            message_id="terminal-event-receipt-loss",
            occurred_at=now,
            worker_id=lease.worker_id,
            run_id=lease.run_id,
            plan_id=lease.plan_id,
            plan_version=lease.plan_version,
            round_id=lease.round_id,
            task_id=lease.task_id,
            execution_id=lease.execution_id,
            lease=lease.lease,
            cancellation_epoch=lease.cancellation_epoch,
            status=TaskStatus.COMPLETED,
            result={"output": {"summary": "finished"}},
            error=None,
            retryable=False,
        )
        repository = PostgresOrchestrationEventRepository(
            session_factory,
            clock=lambda: now,
        )

        class LoseReceiptAfterCommit:
            async def apply_worker_terminal_event(self, value):
                receipt = await repository.apply_worker_terminal_event(value)
                assert receipt.accepted
                raise RuntimeError("simulated lost Worker terminal receipt")

        handler = LoseReceiptAfterCommit()
        with pytest.raises(RuntimeError, match="simulated lost Worker terminal receipt"):
            await handler.apply_worker_terminal_event(event)

        async with session_factory() as session:
            task = await session.get(AgentTaskModel, lease.task_id)
            execution = await session.get(TaskExecutionModel, lease.execution_id)
            job = await session.get(WorkerJobModel, lease.job_id)
            inbox = await session.get(
                OrchestrationInboxModel,
                ("round-task-terminal-v1", event.message_id),
            )
            outbox = (
                await session.execute(select(OrchestrationOutboxModel))
            ).scalars().all()
            resumes = (
                await session.execute(select(RootResumeJobModel))
            ).scalars().all()

        assert task is not None and task.status == "completed"
        assert execution is not None and execution.status == "completed"
        assert job is not None and job.status == "completed"
        assert inbox is not None and inbox.status == "processed"
        assert len(outbox) == 2
        assert len(resumes) == 1

        replay = await repository.apply_worker_terminal_event(event)
        assert replay.accepted is True
        assert replay.deduplicated is True
        assert replay.round_completed is True

        async with session_factory() as session:
            round_row = await session.get(PlanRoundModel, lease.round_id)
            task_rows = (
                await session.execute(select(AgentTaskModel))
            ).scalars().all()
            outbox_rows = (
                await session.execute(select(OrchestrationOutboxModel))
            ).scalars().all()
        assert round_row is not None and round_row.terminal_task_count == 1
        assert len(task_rows) == 1
        assert len(outbox_rows) == 2
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_restart_reclaims_failed_attempt_without_closing_round() -> None:
    """重启后的 app 接管租约、拒绝迟到结果，并持久化下一次 attempt。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_restart_retry_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    first_app_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    restarted_app_engine = create_async_engine(
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

        required_tables = [

            ExecutionLeaseModel.__table__,
            ProjectModel.__table__,
            SessionModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            OrchestrationCommandModel.__table__,
            OrchestrationOutboxModel.__table__,
            OrchestrationInboxModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
            RootResumeJobModel.__table__,
        ]
        async with first_app_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=required_tables
                )
            )

        first_sessions = async_sessionmaker(first_app_engine, expire_on_commit=False)
        restarted_sessions = async_sessionmaker(
            restarted_app_engine, expire_on_commit=False
        )
        async with first_sessions() as session, session.begin():
            session.add(ProjectModel(
                id=f"project-{run_id}",
                name="Worker restart recovery",
                created_at=now,
                updated_at=now,
            ))
            session.add(SessionModel(
                id=session_id,
                project_id=f"project-{run_id}",
                title="Worker restart recovery",
                active_run_id=run_id,
                last_run_sequence=1,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            ))
            await session.flush()
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    message_id="message:" + str(run_id),
                    sequence=1,
                    session_id=session_id,
                    status="running",
                    root_thread_id=f"run:{run_id}",
                    created_at=now,
                    updated_at=now,
                )
            )

        plan = Plan(
            plan_id=plan_id,
            goal="Recover one durable Worker attempt after app restart",
            task_ids=("task-retry",),
            max_parallelism=1,
            session_id=session_id,
            run_id=run_id,
        )
        root_lease = await acquire_postgres_lease(
            first_sessions, "root_execution", run_id
        )
        command = OrchestrationCommand(
            message_id=f"submit:{run_id}",
            command_type=OrchestrationCommandType.SUBMIT_PLAN,
            idempotency_key=f"submit:{run_id}",
            correlation_id=run_id,
            occurred_at=now,
            lease=root_lease,
            plan_id=plan_id,
            scope_key=run_id,
            plan_version=1,
        )
        ids = count()
        submitted = await PostgresOrchestrationCommandRepository(
            first_sessions,
            id_factory=lambda: f"generated-{next(ids)}",
            clock=lambda: now,
        ).submit_plan(
            command,
            plan,
            [Task(task_id="task-retry", title="Retry task", objective="Complete")],
        )

        old_jobs = PostgresWorkerJobRepository(first_sessions, clock=lambda: now)
        old_lease = await old_jobs.claim_next(
            "worker-before-restart", lease_seconds=5
        )
        assert old_lease is not None
        assert old_lease.attempt == 1

        # 重启后的 app 只读取 PostgreSQL 状态，并接管已经过期的租约。
        await first_app_engine.dispose()
        restarted_at = now + timedelta(seconds=6)
        async with restarted_sessions() as session, session.begin():
            await session.execute(
                update(ExecutionLeaseModel)
                .where(
                    ExecutionLeaseModel.resource_type == old_lease.lease.key.resource_type,
                    ExecutionLeaseModel.resource_id == old_lease.lease.key.resource_id,
                    ExecutionLeaseModel.fencing_token == old_lease.lease.fencing_token,
                )
                .values(lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"))
            )
        restarted_jobs = PostgresWorkerJobRepository(
            restarted_sessions, clock=lambda: restarted_at
        )
        reclaimed = await restarted_jobs.claim_next(
            "worker-after-restart", lease_seconds=60
        )
        assert reclaimed is not None
        assert reclaimed.execution_id == old_lease.execution_id
        assert reclaimed.attempt == old_lease.attempt
        assert reclaimed.lease.fencing_token == old_lease.lease.fencing_token + 1

        events = PostgresOrchestrationEventRepository(
            restarted_sessions,
            clock=lambda: restarted_at,
            id_factory=lambda: f"event-{uuid4().hex}",
        )

        def terminal_event(
            lease: WorkerJobLease,
            *,
            message_id: str,
            status: TaskStatus,
            retryable: bool,
        ) -> WorkerTaskTerminalEvent:
            """根据租约构造终态事件，便于比较旧 Worker 与接管者。"""
            return WorkerTaskTerminalEvent(
                message_id=message_id,
                occurred_at=restarted_at,
                worker_id=lease.worker_id,
                run_id=run_id,
                plan_id=plan_id,
                plan_version=lease.plan_version,
                round_id=submitted.round_id,
                task_id="task-retry",
                execution_id=lease.execution_id,
                lease=lease.lease,
                cancellation_epoch=lease.cancellation_epoch,
                status=status,
                result={"attempt": lease.attempt},
                error=(
                    {"code": "provider_timeout"}
                    if status == TaskStatus.FAILED
                    else None
                ),
                retryable=retryable,
            )

        late_result = await events.apply_worker_terminal_event(
            terminal_event(
                old_lease,
                message_id=f"late:{run_id}",
                status=TaskStatus.COMPLETED,
                retryable=False,
            )
        )
        assert not late_result.accepted
        retry_result = await events.apply_worker_terminal_event(
            terminal_event(
                reclaimed,
                message_id=f"failed:{run_id}",
                status=TaskStatus.FAILED,
                retryable=True,
            )
        )
        assert retry_result.accepted
        assert retry_result.retry_scheduled
        assert retry_result.terminal_task_count == 0
        assert retry_result.expected_task_count == 1

        async with restarted_sessions() as session:
            run = await session.get(AgentRunModel, run_id)
            plan_row = await session.get(AgentPlanModel, plan_id)
            task_row = await session.get(AgentTaskModel, "task-retry")
            round_row = await session.get(PlanRoundModel, submitted.round_id)
            round_task = await session.get(
                RoundTaskModel, (submitted.round_id, "task-retry")
            )
            executions = (
                await session.execute(
                    select(TaskExecutionModel)
                    .where(TaskExecutionModel.task_id == "task-retry")
                    .order_by(TaskExecutionModel.attempt)
                )
            ).scalars().all()
            jobs = (await session.execute(select(WorkerJobModel))).scalars().all()
            round_completed_count = await session.scalar(
                select(func.count()).select_from(OrchestrationOutboxModel).where(
                    OrchestrationOutboxModel.event_type == "RoundCompleted"
                )
            )
            resume_count = await session.scalar(
                select(func.count()).select_from(RootResumeJobModel)
            )

        assert run is not None and run.status == "running"
        assert plan_row is not None and plan_row.status == "running"
        assert task_row is not None and task_row.status == "pending"
        assert task_row.execution_generation == 2
        assert round_row is not None and round_row.status == "running"
        assert round_row.terminal_task_count == 0
        assert round_task is not None and round_task.status == "pending"
        assert len(executions) == 2
        assert executions[0].status == "failed"
        assert executions[1].status == "queued"
        assert executions[1].attempt == 2
        assert len(jobs) == 2
        assert sum(job.status == "completed" for job in jobs) == 1
        assert sum(job.status == "pending" for job in jobs) == 1
        assert round_completed_count == 0
        assert resume_count == 0

        next_attempt = await restarted_jobs.claim_next(
            "worker-retry", lease_seconds=60
        )
        assert next_attempt is not None
        assert next_attempt.execution_id == executions[1].execution_id
        assert next_attempt.attempt == 2
    finally:
        await first_app_engine.dispose()
        await restarted_app_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


async def _seed_expired_worker_job(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> None:
    """创建一条崩溃 Worker 留下的执行链和已过期租约。

    参数：session_factory 是隔离 schema 的 async_sessionmaker[AsyncSession]；
    now 是测试使用的 UTC datetime。返回值为 None；数据库写入失败时抛出异常。
    """
    async with session_factory() as session, session.begin():
        session.add(ProjectModel(
            id="project-1", name="Worker lease recovery",
            created_at=now, updated_at=now,
        ))
        session.add(SessionModel(
            id="session-1", project_id="project-1", title="Worker lease recovery",
            active_run_id="run-1", last_run_sequence=1,
            created_at=now, updated_at=now,
            last_activity_at=now,
        ))
        session.add(
            AgentRunModel(
                run_id="run-1",
                message_id="message:" + str("run-1"),
                sequence=1,
                session_id="session-1",
                status="running",
                root_thread_id="run:run-1",
                cancellation_epoch=0,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            AgentPlanModel(
                plan_id="plan-1",
                run_id="run-1",
                session_id="session-1",
                status="running",
                user_goal="recover an interrupted Worker",
                max_parallelism=1,
                current_version=1,
                active_round_id="round-1",
                cancellation_epoch=0,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            AgentTaskModel(
                task_id="task-1",
                plan_id="plan-1",
                run_id="run-1",
                title="Recover task",
                objective="Resume after Worker process loss",
                allowed_tools_json=['exec_shell'],
                timeout_seconds=600,
                status="running",
                execution_generation=1,
                active_execution_id="execution-1",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            PlanVersionModel(
                plan_id="plan-1",
                version=1,
                dag_json={"edges": []},
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            PlanRoundModel(
                round_id="round-1",
                plan_id="plan-1",
                plan_version=1,
                round_number=1,
                status="running",
                expected_task_count=1,
                terminal_task_count=0,
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            RoundTaskModel(
                round_id="round-1",
                task_id="task-1",
                status="running",
                active_execution_id="execution-1",
            )
        )
        await session.flush()
        expired_at = now - timedelta(seconds=10)
        session.add(
            TaskExecutionModel(
                execution_id="execution-1",
                round_id="round-1",
                task_id="task-1",
                attempt=1,
                worker_thread_id="task:task-1:exec:1",
                status="running",
                cancellation_epoch=0,
                created_at=now - timedelta(minutes=1),
                updated_at=expired_at,
            )
        )
        await session.flush()
        session.add(
            WorkerJobModel(
                job_id="job-1",
                dedupe_key="execution:execution-1",
                job_type="execute",
                execution_id="execution-1",
                payload_json={},
                status="running",
                attempt=1,
                available_at=now - timedelta(minutes=1),
                created_at=now - timedelta(minutes=1),
                updated_at=expired_at,
            )
        )
        # TaskExecution 插入触发器已创建 pending 行；夹具将其置为过期授权。
        await session.execute(
            update(ExecutionLeaseModel).where(
                ExecutionLeaseModel.resource_type == "worker_execution",
                ExecutionLeaseModel.resource_id == "execution-1",
            ).values(
                owner_id="crashed-worker", fencing_token=7, state="active",
                lease_until=expired_at, updated_at=expired_at,
            )
        )
