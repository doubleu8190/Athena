"""PostgreSQL 回归：Run 锁序、Trace 收尾与 Root resume 租约收敛。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from application.observability import (
    RootNodeSpanAttributes,
    RunSpanAttributes,
    run_span_id,
)
from domain.leases import LeaseGrant, LeaseKey
from domain.orchestration import RootPlanResumeLease
from domain.runs import RunStartPayload
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    Base,
    ExecutionLeaseModel,
    OrchestratorPlanModel,
    OrchestratorRootResumeJobModel,
    ProjectModel,
    SessionModel,
    TraceSpanModel,
)
from infrastructure.persistence.postgres.repositories import (
    observability_repository as observability_module,
)
from infrastructure.persistence.postgres.repositories.lease_center import (
    PostgresLeaseCenter,
)
from infrastructure.persistence.postgres.repositories.observability_repository import (
    PostgresObservabilityRepository,
)
from infrastructure.persistence.postgres.repositories.root_resume_job_repository import (
    PostgresRootResumeJobRepository,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from infrastructure.persistence.postgres.repositories.trace_locking import (
    lock_trace_span_scope,
)


@pytest_asyncio.fixture
async def run_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """创建只包含本次测试表的隔离 schema。"""
    database_url = os.getenv("ATHENA_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"run_trace_lock_{uuid4().hex}"
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        # 隔离业务表，同时保留 public 中的 vector/zhparser 扩展类型。
        url.update_query_dict({"options": f"-c search_path={schema},public"})
    )
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(sync, checkfirst=False)
            )
        yield async_sessionmaker(test_engine, expire_on_commit=False)
    finally:
        await test_engine.dispose()
        if created:
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA "{schema}" CASCADE')
                )
        await admin_engine.dispose()


async def _seed_queued_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[PostgresRunRepository, str, str]:
    """建立 Project/Session 并提交一条待领取 Run。"""
    now = datetime.now(timezone.utc)
    project_id = f"project-{uuid4().hex}"
    session_id = f"session-{uuid4().hex}"
    run_id = f"run-{uuid4().hex}"
    async with session_factory() as session, session.begin():
        session.add(
            ProjectModel(
                id=project_id,
                name="Run trace lock",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            SessionModel(
                id=session_id,
                project_id=project_id,
                title="Run trace lock",
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
        )
    repository = PostgresRunRepository(session_factory)
    await repository.enqueue_run(
        request_id=f"request-{uuid4().hex}",
        parameters={"message": "验证 Trace 锁序"},
        session_id=session_id,
        project_id=project_id,
        run_id=run_id,
        payload=RunStartPayload(
            message="验证 Trace 锁序",
            message_id=f"message-{uuid4().hex}",
        ),
    )
    return repository, session_id, run_id


async def _seed_completed_plan_and_resume_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    job_status: str,
) -> tuple[str, str]:
    """创建已完成 Plan 与对应恢复作业，由数据库触发器创建 pending lease。"""
    now = datetime.now(timezone.utc)
    plan_id = f"plan-{uuid4().hex}"
    resume_job_id = f"resume-{uuid4().hex}"
    async with session_factory() as session, session.begin():
        session.add(
            OrchestratorPlanModel(
                plan_id=plan_id,
                run_id=run_id,
                sequence=1,
                idempotency_key=f"idempotency-{plan_id}",
                request_hash="0" * 64,
                status="completed",
                goal="验证 Root resume 租约收敛",
                expected_task_count=0,
                terminal_task_count=0,
                root_wait_generation=1,
                created_at=now,
                completed_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorRootResumeJobModel(
                job_id=resume_job_id,
                run_id=run_id,
                plan_id=plan_id,
                root_wait_generation=1,
                status=job_status,
                attempt=1 if job_status == "running" else 0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
    return plan_id, resume_job_id


async def _set_lease_expired(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    resume_job_id: str,
) -> None:
    """把触发器创建的 pending lease 确定性改为已过期的 active grant。"""
    async with session_factory() as session, session.begin():
        updated = await session.execute(
            text(
                """
                UPDATE execution_leases
                SET state = 'active',
                    owner_id = :owner_id,
                    fencing_token = 7,
                    lease_until = clock_timestamp() - interval '1 second',
                    updated_at = clock_timestamp()
                WHERE resource_type = 'root_resume'
                  AND resource_id = :resume_job_id
                """
            ),
            {
                "owner_id": "expired-root-resume-worker",
                "resume_job_id": resume_job_id,
            },
        )
        assert updated.rowcount == 1


def _completion_result(run_id: str, now: datetime) -> dict[str, object]:
    """构造可通过 Run 完成校验的最小最终回答。"""
    return {
        "content": "Root resume 租约收敛完成",
        "answer_stream": {
            "message_id": f"assistant:{run_id}",
            "stream_id": f"answer-{run_id}",
            "started_at": now.isoformat(),
            "last_chunk_id": 1,
        },
    }


def _root_resume_lease(
    *,
    session_id: str,
    run_id: str,
    plan_id: str,
    resume_job_id: str,
    grant: LeaseGrant,
) -> RootPlanResumeLease:
    """把租约 grant 包装成恢复作业执行端口使用的身份。"""
    return RootPlanResumeLease(
        job_id=resume_job_id,
        run_id=run_id,
        session_id=session_id,
        plan_id=plan_id,
        sequence=1,
        root_wait_generation=1,
        interrupt_id=None,
        checkpoint_id=None,
        lease=grant,
    )


async def _assert_terminal_resume_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    run_id: str,
    resume_job_id: str,
) -> None:
    """断言 Run、Session、job 与 lease 在同一最终状态上收敛。"""
    async with session_factory() as session:
        session_row = await session.get(SessionModel, session_id)
        run = await session.get(AgentRunModel, run_id)
        resume_job = await session.get(
            OrchestratorRootResumeJobModel, resume_job_id
        )
        resume_lease = await session.get(
            ExecutionLeaseModel, ("root_resume", resume_job_id)
        )
    assert run is not None and run.status == "completed"
    assert session_row is not None and session_row.active_run_id is None
    assert resume_job is not None and resume_job.status == "completed"
    assert resume_lease is not None and resume_lease.state == "completed"
    assert resume_lease.owner_id is None
    assert resume_lease.lease_until is None


async def _assert_closed_grant_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    run_id: str,
    plan_id: str,
    resume_job_id: str,
    grant: LeaseGrant,
) -> None:
    """确认 Run 终态后旧 grant 无法续租、释放或再次完成恢复作业。"""
    lease_center = PostgresLeaseCenter(session_factory)
    assert await lease_center.renew(grant, lease_seconds=30) is None
    assert await lease_center.finish(grant, completed=True) is False
    assert await PostgresRootResumeJobRepository(session_factory).complete(
        _root_resume_lease(
            session_id=session_id,
            run_id=run_id,
            plan_id=plan_id,
            resume_job_id=resume_job_id,
            grant=grant,
        )
    ) is False


async def _wait_until_blocked_by(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    blocker_pid: int,
    timeout_seconds: float = 5,
) -> int:
    """等待并返回被指定会话 advisory lock 阻塞的后端 PID。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        async with session_factory() as session:
            blocked_pid = await session.scalar(
                text(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE CAST(:blocker_pid AS integer) "
                    "= ANY(pg_blocking_pids(pid)) "
                    "AND wait_event_type = 'Lock' "
                    "AND wait_event = 'advisory' "
                    "LIMIT 1"
                ),
                {"blocker_pid": blocker_pid},
            )
        if blocked_pid is not None:
            return int(blocked_pid)
        await asyncio.sleep(0.02)
    raise AssertionError("run_trace_lock_not_observed")


async def _wait_until_blocked_by_lock(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    blocker_pid: int,
    timeout_seconds: float = 5,
) -> int:
    """等待并返回被指定事务行锁阻塞的后端 PID。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        async with session_factory() as session:
            blocked_pid = await session.scalar(
                text(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE CAST(:blocker_pid AS integer) "
                    "= ANY(pg_blocking_pids(pid)) "
                    "AND wait_event_type = 'Lock' "
                    "AND wait_event = 'transactionid' "
                    "LIMIT 1"
                ),
                {"blocker_pid": blocker_pid},
            )
        if blocked_pid is not None:
            return int(blocked_pid)
        await asyncio.sleep(0.02)
    raise AssertionError("lease_row_lock_wait_not_observed")


async def _probe_domain_row_with_nowait(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    target: str,
    statement: str,
    parameters: dict[str, str],
    blocked_pid: int,
) -> None:
    """用独立短事务证明 Run 写事务尚未持有指定领域行锁。"""
    try:
        async with session_factory() as probe, probe.begin():
            row_id = await probe.scalar(text(statement), parameters)
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "55P03":
            pytest.fail(
                "run_transaction_locked_domain_row_before_trace_advisory "
                f"target={target} blocked_pid={blocked_pid}",
                pytrace=False,
            )
        pytest.fail(
            "domain_lock_probe_failed "
            f"target={target} blocked_pid={blocked_pid} "
            f"sqlstate={sqlstate or 'unknown'} "
            f"error={type(exc.orig).__name__}",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            "domain_lock_probe_failed "
            f"target={target} blocked_pid={blocked_pid} "
            f"error={type(exc).__name__}",
            pytrace=False,
        )
    if row_id is None:
        pytest.fail(
            f"domain_lock_probe_target_missing target={target}",
            pytrace=False,
        )


async def _assert_run_transaction_has_no_domain_row_locks(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    run_id: str,
    resume_job_id: str | None,
    blocked_pid: int,
) -> None:
    """分别探测 Session、Run 和可选 ResumeJob，成功后立即释放。"""
    await _probe_domain_row_with_nowait(
        session_factory,
        target="sessions",
        statement=(
            "SELECT id FROM sessions "
            "WHERE id = :session_id FOR UPDATE NOWAIT"
        ),
        parameters={"session_id": session_id},
        blocked_pid=blocked_pid,
    )
    await _probe_domain_row_with_nowait(
        session_factory,
        target="agent_runs",
        statement=(
            "SELECT run_id FROM agent_runs "
            "WHERE run_id = :run_id FOR UPDATE NOWAIT"
        ),
        parameters={"run_id": run_id},
        blocked_pid=blocked_pid,
    )
    if resume_job_id is not None:
        await _probe_domain_row_with_nowait(
            session_factory,
            target="orchestrator_root_resume_jobs",
            statement=(
                "SELECT job_id FROM orchestrator_root_resume_jobs "
                "WHERE job_id = :job_id FOR UPDATE NOWAIT"
            ),
            parameters={"job_id": resume_job_id},
            blocked_pid=blocked_pid,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_waits_for_concurrent_trace_before_locking_run_rows(
    run_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claim 与根 span 创建竞争时，不得在 advisory 前先取得 Run 行锁。"""
    repository, session_id, run_id = await _seed_queued_run(run_factory)
    observability = PostgresObservabilityRepository(run_factory)
    trace_acquired = asyncio.Event()
    release_trace = asyncio.Event()
    holder_pid: int | None = None
    original_trace_lock = observability_module.lock_trace_span_scope

    async def paused_trace_lock(session, current_run_id: str):
        nonlocal holder_pid
        await original_trace_lock(session, current_run_id)
        holder_pid = int(await session.scalar(text("SELECT pg_backend_pid()")))
        trace_acquired.set()
        await release_trace.wait()

    monkeypatch.setattr(
        observability_module,
        "lock_trace_span_scope",
        paused_trace_lock,
    )
    now = datetime.now(timezone.utc)
    trace_task = asyncio.create_task(
        observability.start_span(
            span_id=run_span_id(run_id),
            session_id=session_id,
            run_id=run_id,
            parent_span_id=None,
            name="run.execute",
            started_at=now,
            attributes=RunSpanAttributes(),
        )
    )
    claim_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(trace_acquired.wait(), timeout=5)
        assert holder_pid is not None
        claim_task = asyncio.create_task(repository.claim_next_run())
        blocked_pid = await _wait_until_blocked_by(
            run_factory,
            blocker_pid=holder_pid,
        )
        await _assert_run_transaction_has_no_domain_row_locks(
            run_factory,
            session_id=session_id,
            run_id=run_id,
            resume_job_id=None,
            blocked_pid=blocked_pid,
        )
        release_trace.set()
        invocation, _ = await asyncio.gather(claim_task, trace_task)
    finally:
        release_trace.set()
        tasks = [trace_task]
        if claim_task is not None:
            tasks.append(claim_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert invocation is not None and invocation.run_id == run_id
    async with run_factory() as session:
        spans = list(
            (
                await session.scalars(
                    select(TraceSpanModel).where(
                        TraceSpanModel.run_id == run_id
                    )
                )
            ).all()
        )
    assert [span.sequence for span in spans] == [1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_finalization_waits_for_concurrent_trace_before_locking_run_rows(
    run_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态收尾与子 span 创建竞争时，必须先等 advisory 再锁 Run/Trace。"""
    repository, session_id, run_id = await _seed_queued_run(run_factory)
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease is not None

    # 让 Run 进入可最终收尾状态，同时结束 Root lease 以避免等待执行中调用。
    assert await PostgresLeaseCenter(run_factory).acknowledge_stopped(
        invocation.lease
    )
    async with run_factory() as session, session.begin():
        run = await session.get(AgentRunModel, run_id, with_for_update=True)
        assert run is not None
        run.status = "stopping"
        run.updated_at = datetime.now(timezone.utc)
        lease = await session.get(
            ExecutionLeaseModel,
            ("root_execution", run_id),
            with_for_update=True,
        )
        assert lease is not None
        # acknowledge_stopped 默认表示正常退出释放，而非任务成功完成。
        assert lease.state == "released"
        assert lease.owner_id is None

    observability = PostgresObservabilityRepository(run_factory)
    trace_acquired = asyncio.Event()
    release_trace = asyncio.Event()
    holder_pid: int | None = None
    original_trace_lock = observability_module.lock_trace_span_scope

    async def paused_trace_lock(session, current_run_id: str):
        nonlocal holder_pid
        await original_trace_lock(session, current_run_id)
        holder_pid = int(await session.scalar(text("SELECT pg_backend_pid()")))
        trace_acquired.set()
        await release_trace.wait()

    monkeypatch.setattr(
        observability_module,
        "lock_trace_span_scope",
        paused_trace_lock,
    )
    now = datetime.now(timezone.utc)
    trace_task = asyncio.create_task(
        observability.start_span(
            span_id=f"root-node:{uuid4().hex}",
            session_id=session_id,
            run_id=run_id,
            parent_span_id=run_span_id(run_id),
            name="concurrent_node",
            started_at=now,
            attributes=RootNodeSpanAttributes(node_name="concurrent_node"),
        )
    )
    finalize_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(trace_acquired.wait(), timeout=5)
        assert holder_pid is not None
        finalize_task = asyncio.create_task(repository.finalize_stop(run_id))
        blocked_pid = await _wait_until_blocked_by(
            run_factory,
            blocker_pid=holder_pid,
        )
        await _assert_run_transaction_has_no_domain_row_locks(
            run_factory,
            session_id=session_id,
            run_id=run_id,
            resume_job_id=None,
            blocked_pid=blocked_pid,
        )
        release_trace.set()
        finalized, child = await asyncio.gather(finalize_task, trace_task)
    finally:
        release_trace.set()
        tasks = [trace_task]
        if finalize_task is not None:
            tasks.append(finalize_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert finalized is True
    async with run_factory() as session:
        run = await session.get(AgentRunModel, run_id)
        child_row = await session.get(TraceSpanModel, child.span_id)
    assert run is not None and run.status == "cancelled"
    assert child_row is not None and child_row.status == "cancelled"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_waits_for_concurrent_trace_before_locking_domain_rows(
    run_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常完成与子 span 创建竞争时，必须先等 advisory 再锁领域行。"""
    repository, session_id, run_id = await _seed_queued_run(run_factory)
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease is not None

    now = datetime.now(timezone.utc)
    plan_id = f"plan-{uuid4().hex}"
    resume_job_id = f"resume-{uuid4().hex}"
    async with run_factory() as session, session.begin():
        session.add(
            OrchestratorPlanModel(
                plan_id=plan_id,
                run_id=run_id,
                sequence=1,
                idempotency_key=f"idempotency-{plan_id}",
                # request_hash 是 Plan 去重所需的固定长度摘要占位值。
                request_hash="0" * 64,
                status="completed",
                goal="验证正常完成锁序",
                expected_task_count=0,
                terminal_task_count=0,
                root_wait_generation=1,
                created_at=now,
                completed_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorRootResumeJobModel(
                job_id=resume_job_id,
                run_id=run_id,
                plan_id=plan_id,
                root_wait_generation=1,
                status="pending",
                attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    observability = PostgresObservabilityRepository(run_factory)
    trace_acquired = asyncio.Event()
    release_trace = asyncio.Event()
    holder_pid: int | None = None
    original_trace_lock = observability_module.lock_trace_span_scope

    async def paused_trace_lock(session, current_run_id: str):
        nonlocal holder_pid
        await original_trace_lock(session, current_run_id)
        holder_pid = int(await session.scalar(text("SELECT pg_backend_pid()")))
        trace_acquired.set()
        await release_trace.wait()

    monkeypatch.setattr(
        observability_module,
        "lock_trace_span_scope",
        paused_trace_lock,
    )
    trace_task = asyncio.create_task(
        observability.start_span(
            span_id=f"root-node:{uuid4().hex}",
            session_id=session_id,
            run_id=run_id,
            parent_span_id=run_span_id(run_id),
            name="complete_concurrent_node",
            started_at=now,
            attributes=RootNodeSpanAttributes(node_name="complete_concurrent_node"),
        )
    )
    complete_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(trace_acquired.wait(), timeout=5)
        assert holder_pid is not None
        complete_task = asyncio.create_task(
            repository.complete(
                run_id,
                {
                    "content": "并发完成成功",
                    "answer_stream": {
                        "message_id": f"assistant:{run_id}",
                        "stream_id": f"answer-{run_id}",
                        "started_at": now.isoformat(),
                        "last_chunk_id": 1,
                    },
                },
                lease=invocation.lease,
            )
        )
        blocked_pid = await _wait_until_blocked_by(
            run_factory,
            blocker_pid=holder_pid,
        )
        await _assert_run_transaction_has_no_domain_row_locks(
            run_factory,
            session_id=session_id,
            run_id=run_id,
            resume_job_id=resume_job_id,
            blocked_pid=blocked_pid,
        )
        release_trace.set()
        completed, child = await asyncio.gather(complete_task, trace_task)
    finally:
        release_trace.set()
        tasks = [trace_task]
        if complete_task is not None:
            tasks.append(complete_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert completed is True
    async with run_factory() as session:
        run = await session.get(AgentRunModel, run_id)
        session_row = await session.get(SessionModel, session_id)
        resume_job = await session.get(
            OrchestratorRootResumeJobModel, resume_job_id
        )
        resume_lease = await session.get(
            ExecutionLeaseModel, ("root_resume", resume_job_id)
        )
        spans = list(
            (
                await session.scalars(
                    select(TraceSpanModel)
                    .where(TraceSpanModel.run_id == run_id)
                    .order_by(TraceSpanModel.sequence)
                )
            ).all()
        )
    assert run is not None and run.status == "completed"
    assert session_row is not None and session_row.active_run_id is None
    assert resume_job is not None and resume_job.status == "completed"
    assert resume_lease is not None and resume_lease.state == "completed"
    assert resume_lease.owner_id is None
    assert resume_lease.lease_until is None
    assert [span.span_id for span in spans] == [
        run_span_id(run_id),
        child.span_id,
    ]
    assert all(span.status == "completed" for span in spans)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_closes_running_root_resume_job_lease(run_factory) -> None:
    """Run 正常完成折叠 running 恢复作业时，必须同事务完成其 fenced lease。"""
    repository, session_id, run_id = await _seed_queued_run(run_factory)
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease is not None

    now = datetime.now(timezone.utc)
    plan_id, resume_job_id = await _seed_completed_plan_and_resume_job(
        run_factory,
        run_id=run_id,
        job_status="running",
    )
    async with run_factory() as session, session.begin():
        old_grant = await PostgresLeaseCenter(
            run_factory
        ).acquire_in_transaction(
            session,
            LeaseKey("root_resume", resume_job_id),
            owner_id="root-resume-worker",
            lease_seconds=30,
        )
        assert old_grant is not None

    completed = await repository.complete(
        run_id,
        _completion_result(run_id, now),
        lease=invocation.lease,
    )

    assert completed is True
    await _assert_terminal_resume_state(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        resume_job_id=resume_job_id,
    )
    await _assert_closed_grant_rejected(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        plan_id=plan_id,
        resume_job_id=resume_job_id,
        grant=old_grant,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_closes_pending_root_resume_job_and_pending_lease(
    run_factory,
) -> None:
    """pending job 及其触发器创建的 pending lease 必须随 Run 一起完成。"""
    repository, session_id, run_id = await _seed_queued_run(run_factory)
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease is not None
    _plan_id, resume_job_id = await _seed_completed_plan_and_resume_job(
        run_factory,
        run_id=run_id,
        job_status="pending",
    )
    async with run_factory() as session:
        pending_lease = await session.get(
            ExecutionLeaseModel, ("root_resume", resume_job_id)
        )
    assert pending_lease is not None and pending_lease.state == "pending"
    assert pending_lease.owner_id is None
    assert pending_lease.lease_until is None

    completed = await repository.complete(
        run_id,
        _completion_result(run_id, datetime.now(timezone.utc)),
        lease=invocation.lease,
    )

    assert completed is True
    await _assert_terminal_resume_state(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        resume_job_id=resume_job_id,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_closes_expired_active_root_resume_lease(
    run_factory,
) -> None:
    """running job 的过期 active grant 应先关闭，且旧 grant 永久失效。"""
    repository, session_id, run_id = await _seed_queued_run(run_factory)
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease is not None
    plan_id, resume_job_id = await _seed_completed_plan_and_resume_job(
        run_factory,
        run_id=run_id,
        job_status="running",
    )
    await _set_lease_expired(
        run_factory,
        resume_job_id=resume_job_id,
    )
    expired_grant = LeaseGrant(
        key=LeaseKey("root_resume", resume_job_id),
        owner_id="expired-root-resume-worker",
        fencing_token=7,
        lease_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    completed = await repository.complete(
        run_id,
        _completion_result(run_id, datetime.now(timezone.utc)),
        lease=invocation.lease,
    )

    assert completed is True
    await _assert_terminal_resume_state(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        resume_job_id=resume_job_id,
    )
    await _assert_closed_grant_rejected(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        plan_id=plan_id,
        resume_job_id=resume_job_id,
        grant=expired_grant,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_converges_while_root_resume_lease_is_renewing(
    run_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续期持有 lease 行锁时，Run _finish 等待后仍完成 job 与 lease。"""
    repository, session_id, run_id = await _seed_queued_run(run_factory)
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease is not None
    plan_id, resume_job_id = await _seed_completed_plan_and_resume_job(
        run_factory,
        run_id=run_id,
        job_status="running",
    )
    competition_center = PostgresLeaseCenter(run_factory)
    async with run_factory() as session, session.begin():
        old_grant = await competition_center.acquire_in_transaction(
            session,
            LeaseKey("root_resume", resume_job_id),
            owner_id="renewing-root-resume-worker",
            lease_seconds=30,
        )
        assert old_grant is not None

    renew_updated = asyncio.Event()
    release_renew = asyncio.Event()
    renew_holder_pid: int | None = None
    original_renew = competition_center.renew_in_transaction

    async def paused_renew(session, grant, *, lease_seconds):
        nonlocal renew_holder_pid
        renewed = await original_renew(
            session,
            grant,
            lease_seconds=lease_seconds,
        )
        renew_holder_pid = int(
            await session.scalar(text("SELECT pg_backend_pid()"))
        )
        renew_updated.set()
        await release_renew.wait()
        return renewed

    monkeypatch.setattr(
        competition_center,
        "renew_in_transaction",
        paused_renew,
    )
    renew_task = asyncio.create_task(
        competition_center.renew(old_grant, lease_seconds=60),
        name="root-resume-renew-competitor",
    )
    complete_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(renew_updated.wait(), timeout=5)
        assert renew_holder_pid is not None
        complete_task = asyncio.create_task(
            repository.complete(
                run_id,
                _completion_result(run_id, datetime.now(timezone.utc)),
                lease=invocation.lease,
            ),
            name="root-resume-finish-during-renew",
        )
        await _wait_until_blocked_by_lock(
            run_factory,
            blocker_pid=renew_holder_pid,
        )
        release_renew.set()
        completed, renewed = await asyncio.gather(
            complete_task,
            renew_task,
        )
    finally:
        release_renew.set()
        for task in (renew_task, complete_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (renew_task, complete_task) if task is not None),
            return_exceptions=True,
        )

    assert completed is True
    assert renewed is not None
    await _assert_terminal_resume_state(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        resume_job_id=resume_job_id,
    )
    await _assert_closed_grant_rejected(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        plan_id=plan_id,
        resume_job_id=resume_job_id,
        grant=old_grant,
    )
    await _assert_closed_grant_rejected(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        plan_id=plan_id,
        resume_job_id=resume_job_id,
        grant=renewed,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_converges_while_root_resume_lease_is_completed(
    run_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """另一事务完成 lease 时，Run _finish 等待后以最终 completed 状态收敛。"""
    repository, session_id, run_id = await _seed_queued_run(run_factory)
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease is not None
    plan_id, resume_job_id = await _seed_completed_plan_and_resume_job(
        run_factory,
        run_id=run_id,
        job_status="running",
    )
    competition_center = PostgresLeaseCenter(run_factory)
    async with run_factory() as session, session.begin():
        old_grant = await competition_center.acquire_in_transaction(
            session,
            LeaseKey("root_resume", resume_job_id),
            owner_id="completing-root-resume-worker",
            lease_seconds=30,
        )
        assert old_grant is not None

    lease_completed = asyncio.Event()
    release_complete = asyncio.Event()
    completion_holder_pid: int | None = None
    original_finish = competition_center.finish_in_transaction

    async def paused_finish(session, grant, *, completed=False):
        nonlocal completion_holder_pid
        finished = await original_finish(
            session,
            grant,
            completed=completed,
        )
        completion_holder_pid = int(
            await session.scalar(text("SELECT pg_backend_pid()"))
        )
        lease_completed.set()
        await release_complete.wait()
        return finished

    monkeypatch.setattr(
        competition_center,
        "finish_in_transaction",
        paused_finish,
    )
    lease_finish_task = asyncio.create_task(
        competition_center.finish(old_grant, completed=True),
        name="root-resume-complete-competitor",
    )
    complete_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(lease_completed.wait(), timeout=5)
        assert completion_holder_pid is not None
        complete_task = asyncio.create_task(
            repository.complete(
                run_id,
                _completion_result(run_id, datetime.now(timezone.utc)),
                lease=invocation.lease,
            ),
            name="root-resume-finish-after-lease-completed",
        )
        await _wait_until_blocked_by_lock(
            run_factory,
            blocker_pid=completion_holder_pid,
        )
        release_complete.set()
        completed, competitor_finished = await asyncio.gather(
            complete_task,
            lease_finish_task,
        )
    finally:
        release_complete.set()
        for task in (lease_finish_task, complete_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (lease_finish_task, complete_task)
                if task is not None
            ),
            return_exceptions=True,
        )

    assert completed is True
    assert competitor_finished is True
    await _assert_terminal_resume_state(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        resume_job_id=resume_job_id,
    )
    await _assert_closed_grant_rejected(
        run_factory,
        session_id=session_id,
        run_id=run_id,
        plan_id=plan_id,
        resume_job_id=resume_job_id,
        grant=old_grant,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_rolls_back_when_running_resume_lease_is_missing(
    run_factory,
) -> None:
    """running job 缺失 lease 时收敛失败必须回滚整个 Run 终态事务。"""
    repository, session_id, run_id = await _seed_queued_run(run_factory)
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.lease is not None
    _plan_id, resume_job_id = await _seed_completed_plan_and_resume_job(
        run_factory,
        run_id=run_id,
        job_status="running",
    )
    async with run_factory() as session, session.begin():
        lease = await session.get(
            ExecutionLeaseModel,
            ("root_resume", resume_job_id),
            with_for_update=True,
        )
        assert lease is not None
        await session.delete(lease)

    with pytest.raises(RuntimeError, match="root_resume_lease_missing"):
        await repository.complete(
            run_id,
            _completion_result(run_id, datetime.now(timezone.utc)),
            lease=invocation.lease,
        )

    async with run_factory() as session:
        session_row = await session.get(SessionModel, session_id)
        run = await session.get(AgentRunModel, run_id)
        resume_job = await session.get(
            OrchestratorRootResumeJobModel, resume_job_id
        )
        lease = await session.get(
            ExecutionLeaseModel,
            ("root_resume", resume_job_id),
        )
    assert session_row is not None and session_row.active_run_id == run_id
    assert run is not None and run.status == "running"
    assert resume_job is not None and resume_job.status == "running"
    assert lease is None
