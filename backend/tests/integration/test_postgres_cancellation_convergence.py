"""PostgreSQL acceptance for FIFO Run scheduling and stop convergence."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from application.runs import RunRejectedError, RunService
from domain.events import ApplicationEvent, EventDurability, EventType
from domain.runs import RunStatus
from infrastructure.persistence.postgres.models import (
    AgentEventModel,
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    IdempotencyReceiptModel,
    MemoryJobScopeSnapshotModel,
    MemoryProcessingJobModel,
    MemoryRunEvidenceSnapshotModel,
    MemorySourceSuppressionModel,
    MessageModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    ProjectModel,
    RootResumeJobModel,
    RoundTaskModel,
    SessionMemorySettingsModel,
    SessionModel,
    TaskExecutionModel,
    WorkerJobModel,
    WorkerExecutionMessageModel,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from infrastructure.persistence.postgres.repositories.event_repository import (
    PostgresEventRepository,
)
from infrastructure.persistence.postgres.repositories.message_repository import (
    PostgresMessageRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)
from infrastructure.persistence.postgres.repositories.project_repository import PostgresProjectRepository


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Use an isolated schema so concurrency checks never touch app data."""
    database_url = os.getenv("ATHENA_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"run_queue_{uuid4().hex}"
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(url)
    engine = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema},public"})
    )
    tables = [
        ProjectModel.__table__,
        SessionModel.__table__,
        MessageModel.__table__,
        AgentRunModel.__table__,
        AgentPlanModel.__table__,
        AgentTaskModel.__table__,
        ApprovalRecordModel.__table__,
        PlanVersionModel.__table__,
        PlanRoundModel.__table__,
        RoundTaskModel.__table__,
        TaskExecutionModel.__table__,
        WorkerJobModel.__table__,
        WorkerExecutionMessageModel.__table__,
        AgentEventModel.__table__,
        RootResumeJobModel.__table__,
        IdempotencyReceiptModel.__table__,
        MemoryJobScopeSnapshotModel.__table__,
        MemorySourceSuppressionModel.__table__,
        MemoryProcessingJobModel.__table__,
        MemoryRunEvidenceSnapshotModel.__table__,
        SessionMemorySettingsModel.__table__,
        OrchestrationOutboxModel.__table__,
    ]
    created = False
    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=tables, checkfirst=False
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        async with factory() as session, session.begin():
            session.add(
                ProjectModel(
                    id="project-1",
                    name="Run queue tests",
                    created_at=now,
                    updated_at=now,
                )
            )
        yield factory
    finally:
        await engine.dispose()
        if created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


def _service(repository: PostgresRunRepository) -> RunService:
    return RunService(repository, repository, id_factory=lambda: uuid4().hex)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_session_runs_are_fifo_and_stop_keeps_the_pointer_until_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Queueing is allowed, but the next Run cannot claim before stop finalizes."""
    repository = PostgresRunRepository(session_factory)
    service = _service(repository)
    first, retry = await asyncio.gather(
        service.submit(
            None,
            project_id="project-1",
            request_id="first-request",
            message="first",
        ),
        service.submit(
            None,
            project_id="project-1",
            request_id="first-request",
            message="first",
        ),
    )
    assert first.session_id == retry.session_id
    assert first.run_id == retry.run_id
    assert {first.replayed, retry.replayed} == {False, True}

    with pytest.raises(RunRejectedError, match="idempotency_conflict"):
        await service.submit(
            None,
            project_id="project-1",
            request_id="first-request",
            message="different payload",
        )

    second = await service.submit(
        first.session_id,
        request_id="second-request",
        message="second",
    )
    assert (first.sequence, second.sequence) == (1, 2)
    async with session_factory() as session, session.begin():
        first_message = await session.get(MessageModel, first.message_id)
        second_message = await session.get(MessageModel, second.message_id)
        assert first_message is not None and second_message is not None
        first_message.timestamp = "2030-01-01T00:00:00+00:00"
        second_message.timestamp = "2020-01-01T00:00:00+00:00"
    ordered_messages = await PostgresMessageRepository(session_factory).list_by_session(
        first.session_id
    )
    assert [message.id for message in ordered_messages] == [
        first.message_id,
        second.message_id,
    ]
    recent_messages = await PostgresMessageRepository(session_factory).list_recent_conversation(
        first.session_id, limit=100
    )
    assert [message.id for message in recent_messages] == [first.message_id, second.message_id]

    claimed, concurrent_claim = await asyncio.gather(
        repository.claim_next_run(), repository.claim_next_run()
    )
    invocation = claimed or concurrent_claim
    assert invocation is not None
    assert invocation.run_id == first.run_id
    assert (claimed is None) != (concurrent_claim is None)
    assert await repository.claim_next_run() is None

    accepted, replayed = await repository.stop_active(
        first.session_id,
        request_id="stop-first",
        expected_run_id=first.run_id,
        displayed_session_seq=4,
    )
    assert not replayed
    assert accepted.changed is True
    assert accepted.status is RunStatus.STOPPING
    replay, was_replayed = await repository.stop_active(
        first.session_id,
        request_id="stop-first",
        expected_run_id=first.run_id,
        displayed_session_seq=4,
    )
    assert was_replayed
    assert replay.run_id == first.run_id
    assert replay.status is RunStatus.STOPPING
    assert await repository.claim_next_run() is None

    event_repository = PostgresEventRepository(session_factory)
    with pytest.raises(ValueError, match="answer_stream_run_not_running"):
        await event_repository.publish(
            ApplicationEvent(
                event_type=EventType.THINKING_COMPLETED,
                durability=EventDurability.DURABLE,
                session_id=first.session_id,
                run_id=first.run_id,
                payload={"content": "late thinking"},
            )
        )
    async with session_factory() as session, session.begin():
        session.add(
            AgentEventModel(
                session_id=first.session_id,
                session_seq=5,
                run_id=first.run_id,
                event_type="thinking.completed",
                durability="durable",
                payload_json='{"content":"late thinking"}',
                occurred_at="2026-01-01T00:00:00+00:00",
            )
        )
    assert await event_repository.list_events_between(first.session_id, after=4) == []
    hidden, has_more = await event_repository.list_events_for_run(
        first.session_id, first.run_id, after=4
    )
    assert hidden == [] and not has_more

    async with session_factory() as session:
        session_row = await session.get(SessionModel, first.session_id)
        stopped = await session.get(AgentRunModel, first.run_id)
    assert session_row is not None and session_row.active_run_id == first.run_id
    assert stopped is not None and stopped.display_cutoff_session_seq == 4

    assert await repository.release_execution_lease(
        first.run_id,
        owner=invocation.lease_owner or "",
        generation=invocation.execution_generation,
    )
    assert await repository.finalize_stop(first.run_id)
    next_invocation = await repository.claim_next_run()
    assert next_invocation is not None
    assert next_invocation.run_id == second.run_id

    async with session_factory() as session:
        session_row = await session.get(SessionModel, first.session_id)
        first_run = await session.get(AgentRunModel, first.run_id)
        second_run = await session.get(AgentRunModel, second.run_id)
        queued_events = (
            await session.execute(
                select(AgentEventModel).where(
                    AgentEventModel.session_id == first.session_id,
                    AgentEventModel.event_type == "run.queued",
                )
            )
        ).scalars().all()
    assert session_row is not None and session_row.active_run_id == second.run_id
    assert first_run is not None and first_run.status == "cancelled"
    assert second_run is not None and second_run.status == "running"
    assert len(queued_events) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_delete_rejects_active_run_before_waiting_for_session_lock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """活动 Run 持有 Session 锁时，项目删除及时拒绝，避免作用域锁互等。"""
    repository = PostgresRunRepository(session_factory)
    run = await _service(repository).submit(None, project_id="project-1", request_id="project-lock", message="first")
    assert await repository.claim_next_run() is not None
    async with session_factory() as session, session.begin():
        await session.execute(select(SessionModel).where(SessionModel.id == run.session_id).with_for_update())
        with pytest.raises(ValueError, match="project_busy"):
            await asyncio.wait_for(PostgresProjectRepository(session_factory).delete("project-1"), timeout=1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_different_sessions_can_be_claimed_independently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository = PostgresRunRepository(session_factory)
    service = _service(repository)
    first = await service.submit(
        None,
        project_id="project-1",
        request_id="session-a-request",
        message="session a",
    )
    second = await service.submit(
        None,
        project_id="project-1",
        request_id="session-b-request",
        message="session b",
    )

    claimed = await asyncio.gather(
        repository.claim_next_run(), repository.claim_next_run()
    )
    invocations = [item for item in claimed if item is not None]
    assert {item.run_id for item in invocations} == {first.run_id, second.run_id}
    assert {item.session_id for item in invocations} == {
        first.session_id,
        second.session_id,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_root_lease_fails_once_and_unblocks_fifo(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """失去 Root 租约后失败收尾；旧代次不能提交结果或重新领取。"""
    repository = PostgresRunRepository(session_factory)
    service = _service(repository)
    first = await service.submit(None, project_id="project-1", request_id="expired-1", message="first")
    second = await service.submit(first.session_id, request_id="expired-2", message="second")
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease_owner is not None
    async with session_factory() as session, session.begin():
        run = await session.get(AgentRunModel, first.run_id)
        run.execution_lease_until = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    assert await repository.acquire_execution_lease(first.run_id, owner="resume", lease_seconds=30) is None
    assert not await repository.complete(first.run_id, {}, owner=invocation.lease_owner, generation=invocation.execution_generation)
    assert not await repository.release_execution_lease(first.run_id, owner=invocation.lease_owner, generation=invocation.execution_generation)
    counts = await asyncio.gather(repository.reconcile_expired_execution_leases(), repository.reconcile_expired_execution_leases())
    assert sum(counts) == 1
    async with session_factory() as session:
        run = await session.get(AgentRunModel, first.run_id)
        session_row = await session.get(SessionModel, first.session_id)
        events = (await session.execute(select(OrchestrationOutboxModel).where(
            OrchestrationOutboxModel.correlation_id == first.run_id,
            OrchestrationOutboxModel.event_type == "RunFailed",
        ))).scalars().all()
        assert run.status == "failed"
        assert json.loads(run.error_json)["code"] == "root_execution_lease_expired"
        assert session_row.active_run_id is None
        assert len(events) == 1
    next_invocation = await repository.claim_next_run()
    assert next_invocation is not None and next_invocation.run_id == second.run_id


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("task_count,tool_count", [(13, 1), (1, 5)])
async def test_memory_evidence_limits_are_applied_before_domain_validation(
    session_factory: async_sessionmaker[AsyncSession], task_count: int, tool_count: int,
) -> None:
    """任务和工具证据超限时冻结有界快照并标记截断，记忆作业仍可继续。"""
    from infrastructure.persistence.postgres.repositories.memory_job_repository import (
        _load_or_freeze_execution_evidence,
    )

    repository = PostgresRunRepository(session_factory)
    run = await _service(repository).submit(None, project_id="project-1", request_id="evidence", message="first")
    now = datetime.now(timezone.utc)
    async with session_factory() as session, session.begin():
        session.add(AgentPlanModel(plan_id="evidence-plan", run_id=run.run_id, session_id=run.session_id,
            status="completed", user_goal="goal", current_version=1,
            created_at=now.isoformat(), updated_at=now.isoformat()))
        await session.flush()
        session.add(PlanVersionModel(plan_id="evidence-plan", version=1, dag_json={}, created_at=now))
        session.add_all([
            AgentTaskModel(task_id=f"evidence-task-{index:02d}", plan_id="evidence-plan", run_id=run.run_id,
                title="task", objective="work", status="completed", execution_generation=1,
                created_at=now.isoformat(), updated_at=now.isoformat())
            for index in range(task_count)
        ])
        await session.flush()
        session.add(PlanRoundModel(round_id="evidence-round", plan_id="evidence-plan", plan_version=1,
            round_number=1, status="completed", expected_task_count=task_count, terminal_task_count=task_count, created_at=now))
        await session.flush()
        session.add(RoundTaskModel(round_id="evidence-round", task_id="evidence-task-00", status="completed"))
        await session.flush()
        session.add(TaskExecutionModel(execution_id="evidence-execution", round_id="evidence-round", task_id="evidence-task-00",
            attempt=1, worker_thread_id="evidence-thread", status="completed", fencing_token=1, created_at=now, updated_at=now))
        await session.flush()
        session.add_all([
            WorkerExecutionMessageModel(message_id=f"evidence-message-{index}", session_id=run.session_id,
                run_id=run.run_id, plan_id="evidence-plan", task_id="evidence-task-00", execution_id="evidence-execution",
                execution_generation=1, invocation_id="evidence-call", sequence_no=index,
                entry_type="tool_result", role=None, tool_name="shell", content_json={"tool_result": {"status": "succeeded"}},
                created_at=now + timedelta(microseconds=index))
            for index in range(tool_count)
        ])
    async with session_factory() as session, session.begin():
        evidence = await _load_or_freeze_execution_evidence(session, turn_id=run.run_id, observed_at=now.isoformat())
        assert evidence is not None and evidence.truncated
        assert evidence.total_task_count == task_count
        assert len(evidence.tasks) == min(task_count, 12)
        assert len(evidence.tasks[0].tool_outcomes) == min(tool_count, 4)
        assert evidence.tasks[0].tool_outcomes[-1].message_id == f"evidence-message-{tool_count - 1}"
        assert await _load_or_freeze_execution_evidence(session, turn_id=run.run_id, observed_at=now.isoformat()) == evidence


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkpoint_wait_keeps_session_and_allows_new_resume_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """正常释放 Root 调用租约后继续持有会话，并拒绝上代迟到结果。"""
    repository = PostgresRunRepository(session_factory)
    first = await _service(repository).submit(None, project_id="project-1", request_id="wait-1", message="first")
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease_owner is not None
    assert await repository.release_execution_lease(first.run_id, owner=invocation.lease_owner, generation=invocation.execution_generation)
    assert await repository.reconcile_expired_execution_leases() == 0
    generation = await repository.acquire_execution_lease(first.run_id, owner="resumer", lease_seconds=30)
    assert generation is not None and generation > invocation.execution_generation
    assert not await repository.fail(first.run_id, {"code": "stale"}, owner=invocation.lease_owner, generation=invocation.execution_generation)
    assert (await repository.get(first.run_id)).status == "running"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("expired_root", [False, True])
async def test_failed_root_waits_for_live_worker_before_next_run(
    session_factory: async_sessionmaker[AsyncSession],
    expired_root: bool,
) -> None:
    """Root 失败后保留执行指针，直到 Worker 停止或租约过期。"""
    repository = PostgresRunRepository(session_factory)
    service = _service(repository)
    first = await service.submit(None, project_id="project-1", request_id="failed-1", message="first")
    second = await service.submit(first.session_id, request_id="failed-2", message="second")
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease_owner is not None
    now = datetime.now(timezone.utc)
    async with session_factory() as session, session.begin():
        session.add(AgentPlanModel(plan_id="plan-failure", run_id=first.run_id, session_id=first.session_id,
            status="running", user_goal="goal", current_version=1, active_round_id="round-failure",
            created_at=now.isoformat(), updated_at=now.isoformat()))
        await session.flush()
        session.add_all([
            AgentTaskModel(task_id="task-failure", plan_id="plan-failure", run_id=first.run_id,
                title="task", objective="work", status="running", active_execution_id="execution-failure",
                created_at=now.isoformat(), updated_at=now.isoformat()),
            PlanVersionModel(plan_id="plan-failure", version=1, dag_json={}, created_at=now),
        ])
        await session.flush()
        session.add(PlanRoundModel(round_id="round-failure", plan_id="plan-failure", plan_version=1,
            round_number=1, status="running", expected_task_count=1, created_at=now))
        await session.flush()
        session.add(RoundTaskModel(round_id="round-failure", task_id="task-failure", status="running", active_execution_id="execution-failure"))
        await session.flush()
        session.add(TaskExecutionModel(execution_id="execution-failure", round_id="round-failure", task_id="task-failure",
            attempt=1, worker_thread_id="thread-failure", status="running", lease_owner="worker", lease_until=now + timedelta(minutes=5),
            fencing_token=1, created_at=now, updated_at=now))
        await session.flush()
        session.add(WorkerJobModel(job_id="job-failure", dedupe_key="job-failure", job_type="execute", execution_id="execution-failure",
            status="running", lease_owner="worker", lease_until=now + timedelta(minutes=5), available_at=now, created_at=now, updated_at=now))

    if expired_root:
        async with session_factory() as session, session.begin():
            run = await session.get(AgentRunModel, first.run_id)
            run.execution_lease_until = (now - timedelta(seconds=1)).isoformat()
        assert await repository.reconcile_expired_execution_leases() == 1
    else:
        assert await repository.fail(first.run_id, {"code": "root_failed"}, owner=invocation.lease_owner, generation=invocation.execution_generation)
        assert await repository.release_execution_lease(first.run_id, owner=invocation.lease_owner, generation=invocation.execution_generation)
    workers = PostgresWorkerJobRepository(session_factory)
    assert await workers.reconcile_cancellations() == []
    assert await repository.claim_next_run() is None
    async with session_factory() as session, session.begin():
        run = await session.get(AgentRunModel, first.run_id)
        session_row = await session.get(SessionModel, first.session_id)
        job = await session.get(WorkerJobModel, "job-failure")
        assert run.status == "stopping" and session_row.active_run_id == first.run_id
        assert job.status == "cancel_requested"
        job.lease_until = now - timedelta(seconds=1)
    assert len(await workers.reconcile_cancellations()) == 1
    async with session_factory() as session:
        run = await session.get(AgentRunModel, first.run_id)
        job = await session.get(WorkerJobModel, "job-failure")
        events = (await session.execute(select(OrchestrationOutboxModel).where(
            OrchestrationOutboxModel.correlation_id == first.run_id,
            OrchestrationOutboxModel.event_type == "RunFailed",
        ))).scalars().all()
        expected_error = "root_execution_lease_expired" if expired_root else "root_failed"
        assert run.status == "failed" and json.loads(run.error_json)["code"] == expected_error
        assert job.status == "cancelled" and len(events) == 1
    next_invocation = await repository.claim_next_run()
    assert next_invocation is not None and next_invocation.run_id == second.run_id
