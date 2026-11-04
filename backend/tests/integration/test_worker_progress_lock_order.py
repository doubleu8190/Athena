"""PostgreSQL 并发回归：Worker Progress 与终态写事务不得形成锁环。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from application.observability import WorkerExecutionSpanAttributes
from domain.orchestration.worker_entities import (
    WorkerResultSubmission,
    WorkerStopConfirmation,
)
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    Base,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    ProjectModel,
    SessionModel,
    TraceSpanModel,
    WorkerContractModel,
    WorkerInstanceModel,
)
from infrastructure.persistence.postgres.repositories.progress_locking import (
    lock_progress_session,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _SeededExecution:
    """并发锁序测试所需的完整 Worker 父级身份。"""

    project_id: str
    session_id: str
    run_id: str
    plan_id: str
    task_id: str
    execution_id: str
    instance_id: str
    contract_id: str
    cancel_command_id: str | None = None


@pytest_asyncio.fixture
async def progress_lock_store() -> AsyncIterator[
    async_sessionmaker[AsyncSession]
]:
    """创建只属于当前测试的 PostgreSQL 隔离 schema。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"worker_progress_lock_{uuid4().hex}"
    url = make_url(_DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        # 临时 schema 隔离业务表，同时保留 public 中的扩展类型。
        url.update_query_dict({"options": f"-c search_path={schema},public"}),
        pool_size=8,
        max_overflow=2,
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


async def _seed_execution(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    task_status: str = "running",
    execution_status: str = "running",
    cancel_command: bool = False,
) -> _SeededExecution:
    """按真实外键顺序建立 Run、Plan、Task、Execution 和 Trace。"""
    now = datetime.now(timezone.utc)
    project_id = str(uuid4())
    session_id = str(uuid4())
    run_id = str(uuid4())
    plan_id = str(uuid4())
    task_id = str(uuid4())
    execution_id = str(uuid4())
    instance_id = str(uuid4())
    contract_id = str(uuid4())
    cancel_command_id = str(uuid4()) if cancel_command else None
    async with session_factory() as session, session.begin():
        session.add(
            ProjectModel(
                id=project_id,
                name="Worker Progress lock order",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            SessionModel(
                id=session_id,
                project_id=project_id,
                title="Worker Progress lock order",
                active_run_id=run_id,
                last_run_sequence=1,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
        )
        session.add(
            AgentRunModel(
                run_id=run_id,
                session_id=session_id,
                message_id=str(uuid4()),
                sequence=1,
                status="running",
                request_json={},
                root_thread_id=f"run:{run_id}",
                root_wait_generation=1,
                orchestrator_plan_sequence=1,
                cancellation_epoch=0,
                cancellation_external_effect_status="none",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            WorkerContractModel(
                contract_id=contract_id,
                revision=1,
                can_do_json=["执行测试任务"],
                cannot_do_json=[],
                input_schema_json={"type": "object"},
                output_schema_json={"type": "object"},
                fingerprint_hash="0" * 64,
                created_at=now,
            )
        )
        # 模型之间没有 relationship，显式 flush 父表，避免同一次 flush 中
        # SQLAlchemy 先插入带外键的 Worker 实例。
        await session.flush()
        session.add(
            WorkerInstanceModel(
                instance_id=instance_id,
                service_principal="worker-progress-lock",
                instance_key=str(uuid4()),
                worker_name="Worker Progress lock",
                contract_id=contract_id,
                contract_revision=1,
                base_url="https://worker.invalid",
                execution_timeout_seconds=60,
                max_concurrency=1,
                status="ready",
                accepting_tasks=True,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorPlanModel(
                plan_id=plan_id,
                run_id=run_id,
                sequence=1,
                idempotency_key=f"plan:{plan_id}",
                request_hash="1" * 64,
                goal="验证 Progress 锁序",
                status="running",
                expected_task_count=1,
                terminal_task_count=0,
                root_wait_generation=1,
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorTaskModel(
                task_id=task_id,
                operation_id=str(uuid4()),
                plan_id=plan_id,
                task_ref="progress-lock-task",
                contract_id=contract_id,
                contract_revision=1,
                objective="验证 Progress 锁序",
                input_json={},
                acceptance_criteria="并发事务正常收敛",
                status=task_status,
                execution_generation=1,
                cancellation_epoch=1 if task_status == "cancelled" else 0,
                active_execution_id=execution_id,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorExecutionModel(
                execution_id=execution_id,
                task_id=task_id,
                instance_id=instance_id,
                worker_name="Worker Progress lock",
                generation=1,
                status=execution_status,
                cancellation_epoch=1 if task_status == "cancelled" else 0,
                capacity_reserved=True,
                created_at=now,
                updated_at=now,
            )
        )
        # Execution 是 Command 的父记录；没有 relationship 时也必须显式 flush，
        # 否则测试夹具可能先插入 Command 并触发外键错误。
        await session.flush()
        session.add(
            TraceSpanModel(
                span_id=f"worker:{execution_id}",
                session_id=session_id,
                run_id=run_id,
                parent_span_id=None,
                span_type="worker_execution",
                name="worker.execute",
                status="running",
                started_at=now,
                attributes_json=WorkerExecutionSpanAttributes(
                    execution_id=execution_id,
                    task_id=task_id,
                    worker_id=instance_id,
                ).model_dump(mode="json"),
                sequence=1,
            )
        )
        if cancel_command_id is not None:
            command_body = {
                "protocol_version": "1.0",
                "command_id": cancel_command_id,
                "execution_id": execution_id,
                "generation": 1,
                "cancellation_epoch": 1,
                "reason": "task_cancelled",
            }
            session.add(
                OrchestratorWorkerCommandModel(
                    command_id=cancel_command_id,
                    execution_id=execution_id,
                    instance_id=instance_id,
                    generation=1,
                    idempotency_key=cancel_command_id,
                    command_type="cancel",
                    request_hash="2" * 64,
                    command_json=command_body,
                    status="pending",
                    attempt=0,
                    send_attempt_count=0,
                    delivery_uncertain_before_send=False,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
    return _SeededExecution(
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        plan_id=plan_id,
        task_id=task_id,
        execution_id=execution_id,
        instance_id=instance_id,
        contract_id=contract_id,
        cancel_command_id=cancel_command_id,
    )


async def _wait_until_blocked_by(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    blocker_pid: int,
    timeout_seconds: float = 5,
) -> int:
    """等待目标事务被指定 PostgreSQL 会话阻塞。"""
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
                    "LIMIT 1"
                ),
                {"blocker_pid": blocker_pid},
            )
        if blocked_pid is not None:
            return int(blocked_pid)
        await asyncio.sleep(0.02)
    raise AssertionError("worker_progress_lock_wait_not_observed")


async def _assert_run_row_not_locked(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    blocked_pid: int,
) -> None:
    """证明竞争事务尚未在 Progress 屏障前取得 Run 行锁。"""
    try:
        async with session_factory() as probe, probe.begin():
            await probe.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == run_id)
                .with_for_update(nowait=True)
            )
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        pytest.fail(
            "worker_transaction_locked_run_before_progress_scope "
            f"blocked_pid={blocked_pid} "
            f"sqlstate={sqlstate or 'unknown'}",
            pytrace=False,
        )


async def _run_against_progress_boundary(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    seed: _SeededExecution,
    operation: Callable[[], Awaitable[_T]],
) -> _T:
    """模拟 Progress 已持 advisory + Execution 锁，验证目标先等待该边界。"""
    progress_ready = asyncio.Event()
    release_progress = asyncio.Event()
    holder_pid: int | None = None

    async def hold_progress_boundary() -> None:
        nonlocal holder_pid
        async with session_factory() as session, session.begin():
            await lock_progress_session(session, seed.session_id)
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(
                    OrchestratorExecutionModel.execution_id
                    == seed.execution_id
                )
                .with_for_update()
            )
            assert execution is not None
            holder_pid = int(
                await session.scalar(text("SELECT pg_backend_pid()"))
            )
            progress_ready.set()
            await release_progress.wait()

    holder = asyncio.create_task(hold_progress_boundary())
    operation_task: asyncio.Task[_T] | None = None
    try:
        await asyncio.wait_for(progress_ready.wait(), timeout=5)
        assert holder_pid is not None
        operation_task = asyncio.create_task(operation())
        blocked_pid = await _wait_until_blocked_by(
            session_factory,
            blocker_pid=holder_pid,
        )
        await _assert_run_row_not_locked(
            session_factory,
            run_id=seed.run_id,
            blocked_pid=blocked_pid,
        )
        release_progress.set()
        result, _ = await asyncio.gather(operation_task, holder)
        return result
    finally:
        release_progress.set()
        pending = [holder]
        if operation_task is not None:
            pending.append(operation_task)
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_result_waits_for_progress_scope_before_locking_run(
    progress_lock_store,
) -> None:
    """Result 终态写 Trace 前必须先等待 Progress Scope，不能先锁 Run。"""
    seed = await _seed_execution(progress_lock_store)
    repository = PostgresWorkerOrchestrationRepository(progress_lock_store)
    submission = WorkerResultSubmission(
        execution_id=seed.execution_id,
        instance_id=seed.instance_id,
        generation=1,
        result_id=str(uuid4()),
        status="completed",
        output={"ok": True},
    )

    receipt = await _run_against_progress_boundary(
        progress_lock_store,
        seed=seed,
        operation=lambda: repository.record_result(submission=submission),
    )

    assert receipt.disposition == "recorded"
    async with progress_lock_store() as session:
        execution = await session.get(
            OrchestratorExecutionModel, seed.execution_id
        )
        trace = await session.get(TraceSpanModel, f"worker:{seed.execution_id}")
    assert execution is not None and execution.status == "completed"
    assert trace is not None and trace.status == "completed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_task_waits_for_progress_scope_before_locking_run(
    progress_lock_store,
) -> None:
    """逻辑取消等待 Progress Scope 后才锁 Run/Task/Execution。"""
    seed = await _seed_execution(progress_lock_store)
    repository = PostgresWorkerOrchestrationRepository(progress_lock_store)

    cancel_command_id = await _run_against_progress_boundary(
        progress_lock_store,
        seed=seed,
        operation=lambda: repository.cancel_task(
            run_id=seed.run_id,
            task_id=seed.task_id,
        ),
    )

    assert cancel_command_id is not None
    async with progress_lock_store() as session:
        task = await session.get(OrchestratorTaskModel, seed.task_id)
        execution = await session.get(
            OrchestratorExecutionModel, seed.execution_id
        )
        command = await session.get(
            OrchestratorWorkerCommandModel, cancel_command_id
        )
    assert task is not None and task.status == "cancelled"
    assert execution is not None and execution.status == "cancelling"
    assert command is not None and command.status == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stop_confirmation_waits_for_progress_scope_before_locking_run(
    progress_lock_store,
) -> None:
    """停止确认等待 Progress Scope 后才锁 Run/Task/Execution。"""
    seed = await _seed_execution(
        progress_lock_store,
        task_status="cancelled",
        execution_status="cancelling",
        cancel_command=True,
    )
    assert seed.cancel_command_id is not None
    repository = PostgresWorkerOrchestrationRepository(progress_lock_store)
    confirmation = WorkerStopConfirmation(
        instance_id=seed.instance_id,
        stop_confirmation_id=str(uuid4()),
        cancel_command_id=seed.cancel_command_id,
        execution_id=seed.execution_id,
        generation=1,
        cancellation_epoch=1,
        reason="task_cancelled",
        stopped_at=datetime.now(timezone.utc),
    )

    receipt = await _run_against_progress_boundary(
        progress_lock_store,
        seed=seed,
        operation=lambda: repository.confirm_stopped(confirmation),
    )

    assert receipt.disposition == "capacity_released"
    async with progress_lock_store() as session:
        execution = await session.get(
            OrchestratorExecutionModel, seed.execution_id
        )
        command = await session.get(
            OrchestratorWorkerCommandModel, seed.cancel_command_id
        )
        trace = await session.get(TraceSpanModel, f"worker:{seed.execution_id}")
    assert execution is not None and execution.status == "cancelled"
    assert execution.capacity_reserved is False
    assert command is not None and command.status == "stopped"
    assert trace is not None and trace.status == "cancelled"
