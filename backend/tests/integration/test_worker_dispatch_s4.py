"""S4 持久派发租约、容量和结果竞态的 PostgreSQL 验收。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from domain.orchestration.worker_entities import (
    WorkerPlanSubmission,
    WorkerResultSubmission,
    WorkerTaskSpec,
)
from application.observability import RunSpanAttributes, run_span_id
from application.orchestration.worker_protocol_models import (
    WorkerCommandReceiptModel,
    WorkerTaskCommandModel,
)
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorRootResumeJobModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    OrchestrationOutboxModel,
    ProjectModel,
    SessionModel,
    TraceSpanModel,
    WorkerContractModel,
    WorkerInstanceModel,
    WorkerResultReceiptModel,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)
from infrastructure.persistence.postgres.repositories.trace_locking import (
    lock_trace_span_scope,
)
from infrastructure.integrations.worker_http import PinnedWorkerHttpClient
from tests.integration.test_worker_orchestration_s2 import worker_store
from workers.worker_task_dispatcher import WorkerTaskDispatcher


async def _submit_plan(
    session_factory,
    *,
    task_contracts: tuple[str, ...],
    available_contracts: tuple[str, ...],
    instance_capacity: int = 8,
    instance_timeout_seconds: int = 60,
):
    """建立隔离 Run/契约/实例后经正式 Plan API 写入待派发命令。"""
    now = datetime.now(timezone.utc)
    run_id, plan_id = str(uuid4()), str(uuid4())
    async with session_factory() as session, session.begin():
        session.add(
            AgentRunModel(
                run_id=run_id,
                session_id=f"session-{run_id}",
                message_id=f"message-{run_id}",
                sequence=1,
                status="running",
                request_json={},
                root_thread_id=f"root-{run_id}",
                root_wait_generation=0,
                orchestrator_plan_sequence=0,
                cancellation_epoch=0,
                cancellation_external_effect_status="none",
                created_at=now,
                updated_at=now,
            )
        )
        for contract_id in set(task_contracts) | set(available_contracts):
            session.add(
                WorkerContractModel(
                    contract_id=contract_id,
                    revision=1,
                    can_do_json=[f"capability-{contract_id}"],
                    cannot_do_json=["arbitrary code"],
                    input_schema_json={"type": "object"},
                    output_schema_json={"type": "object"},
                    fingerprint_hash=str(uuid4()),
                    created_at=now,
                )
            )
        for contract_id in available_contracts:
            session.add(
                WorkerInstanceModel(
                    instance_id=f"instance-{contract_id}",
                    service_principal="worker-service",
                    instance_key=str(uuid4()),
                    worker_name="Test Worker",
                    contract_id=contract_id,
                    contract_revision=1,
                    base_url="https://worker.invalid",
                    execution_timeout_seconds=instance_timeout_seconds,
                    max_concurrency=instance_capacity,
                    status="ready",
                    accepting_tasks=True,
                    health_identity_valid=True,
                    created_at=now,
                    updated_at=now,
                )
            )
    repository = PostgresWorkerOrchestrationRepository(session_factory)
    await repository.submit_plan(
        WorkerPlanSubmission(
            run_id=run_id,
            plan_id=plan_id,
            sequence=1,
            idempotency_key=f"plan:{plan_id}",
            goal="验证持久 Worker 派发",
            tasks=tuple(
                WorkerTaskSpec(
                    task_ref=f"task-{index}",
                    contract_id=contract_id,
                    contract_revision=1,
                    objective="执行任务",
                    input={},
                    acceptance_criteria="返回对象",
                )
                for index, contract_id in enumerate(task_contracts)
            ),
            created_at=now,
        )
    )
    return repository, plan_id


async def _claim_and_start(repository, *, owner: str = "test-dispatcher"):
    lease = await repository.claim_dispatch_command(
        owner_id=owner, lease_seconds=15
    )
    assert lease is not None
    start = await repository.start_dispatch(
        lease,
        lease_seconds=15,
        assignment_retry_seconds=0.1,
    )
    return lease, start


async def _finalize_first_send(repository, lease, start, *, lease_seconds: float = 15):
    assert start is not None

    def build_command(final_start):
        assert final_start.deadline_at is not None
        draft = {
            key: value
            for key, value in final_start.command.items()
            if key != "assignment_required"
        }
        return WorkerTaskCommandModel.model_validate(
            {
                **draft,
                "execution_grant": {
                    "token": "g" * 32,
                    "expires_at": final_start.deadline_at,
                },
            }
        ).model_dump(mode="json", exclude_none=True)

    finalized = await repository.prepare_first_dispatch_send(
        lease,
        lease_seconds=lease_seconds,
        command_builder=build_command,
    )
    assert finalized is not None
    return finalized


@pytest.mark.integration
@pytest.mark.asyncio
async def test_result_before_http_receipt_keeps_terminal_command_and_releases_once(
    worker_store,
) -> None:
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    repository, plan_id = await _submit_plan(
        session_factory,
        task_contracts=(contract_id,),
        available_contracts=(contract_id,),
    )
    lease, start = await _claim_and_start(repository)
    assert start is not None
    start = await _finalize_first_send(repository, lease, start)
    result_id = str(uuid4())
    await repository.record_result(
        submission=WorkerResultSubmission(
            execution_id=start.execution_id,
            instance_id=start.instance_id,
            generation=1,
            result_id=result_id,
            status="completed",
            output={"ok": True},
        )
    )
    receipt = WorkerCommandReceiptModel(
        protocol_version="1.0",
        command_id=lease.command_id,
        receipt_id=str(uuid4()),
        disposition="accepted",
        received_at=datetime.now(timezone.utc),
    )
    assert not await repository.record_dispatch_receipt(lease, receipt)

    async with session_factory() as session:
        command = await session.get(OrchestratorWorkerCommandModel, lease.command_id)
        execution = await session.get(OrchestratorExecutionModel, start.execution_id)
        task = await session.scalar(
            select(OrchestratorTaskModel).where(
                OrchestratorTaskModel.plan_id == plan_id
            )
        )
        resume_jobs = list(
            (
                await session.scalars(
                    select(OrchestratorRootResumeJobModel).where(
                        OrchestratorRootResumeJobModel.plan_id == plan_id
                    )
                )
            ).all()
        )
        result_receipts = list(
            (await session.scalars(select(WorkerResultReceiptModel))).all()
        )
    assert command is not None and command.status == "result_received"
    assert execution is not None and not execution.capacity_reserved
    assert task is not None and task.status == "completed"
    assert len(resume_jobs) == 1
    assert len(result_receipts) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_first_execution_deadline_starts_after_slow_pre_send_preparation(
    worker_store,
) -> None:
    """分配后准备延迟超过 Worker timeout，不消耗首次发送的执行时限。"""
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    repository, _ = await _submit_plan(
        session_factory,
        task_contracts=(contract_id,),
        available_contracts=(contract_id,),
        instance_timeout_seconds=1,
    )
    lease, assigned_start = await _claim_and_start(repository)
    assert assigned_start is not None
    assert assigned_start.started_at is None
    assert assigned_start.deadline_at is None

    async with session_factory() as session:
        assigned_at = await session.scalar(select(func.clock_timestamp()))
    assert assigned_at is not None
    await asyncio.sleep(1.1)

    first_send = await _finalize_first_send(repository, lease, assigned_start)

    assert first_send.started_at is not None
    assert first_send.deadline_at is not None
    assert first_send.started_at >= assigned_at + timedelta(seconds=1)
    assert first_send.deadline_at == first_send.started_at + timedelta(seconds=1)
    task_command = WorkerTaskCommandModel.model_validate(first_send.command)
    assert task_command.execution_grant.expires_at == first_send.deadline_at
    async with session_factory() as session:
        execution = await session.get(
            OrchestratorExecutionModel, first_send.execution_id
        )
        command = await session.get(
            OrchestratorWorkerCommandModel, first_send.command_id
        )
    assert execution is not None and execution.started_at == first_send.started_at
    assert execution.deadline_at == first_send.deadline_at
    assert command is not None and command.send_attempt_count == 1
    assert "assignment_required" not in command.command_json


@pytest.mark.integration
@pytest.mark.asyncio
async def test_later_permanent_rejection_keeps_capacity_after_uncertain_delivery(
    worker_store,
) -> None:
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    repository, plan_id = await _submit_plan(
        session_factory,
        task_contracts=(contract_id,),
        available_contracts=(contract_id,),
    )
    first_lease, first_start = await _claim_and_start(repository)
    assert first_start is not None
    first_start = await _finalize_first_send(repository, first_lease, first_start)
    assert await repository.retry_dispatch_command(first_lease, delay_seconds=0)

    second_lease, second_start = await _claim_and_start(repository)
    assert second_start is not None
    assert await repository.prepare_dispatch_send(second_lease, lease_seconds=15)
    assert not await repository.fail_dispatch_command(
        second_lease,
        error_code="AUTHENTICATION_FAILED",
        message="credential rejected after an earlier uncertain send",
        root_replan=False,
    )

    async with session_factory() as session:
        command = await session.get(
            OrchestratorWorkerCommandModel, first_lease.command_id
        )
        execution = await session.get(
            OrchestratorExecutionModel, first_start.execution_id
        )
        task = await session.scalar(
            select(OrchestratorTaskModel).where(
                OrchestratorTaskModel.plan_id == plan_id
            )
        )
    assert command is not None and command.status == "awaiting_result"
    assert command.send_attempt_count == 2
    assert command.delivery_uncertain_before_send is True
    assert execution is not None and execution.capacity_reserved
    assert task is not None and task.status == "running"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_permanent_dispatch_failure_emits_terminal_event(worker_store) -> None:
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    repository, _ = await _submit_plan(
        session_factory,
        task_contracts=(contract_id,),
        available_contracts=(contract_id,),
    )
    lease, start = await _claim_and_start(repository)
    assert start is not None

    assert await repository.fail_dispatch_command(
        lease,
        error_code="AUTHENTICATION_FAILED",
        message="credential rejected before send",
        root_replan=False,
    )

    async with session_factory() as session:
        event = await session.scalar(
            select(OrchestrationOutboxModel).where(
                OrchestrationOutboxModel.dedupe_key
                == f"execution:{start.execution_id}:dispatch-failed:{lease.command_id}"
            )
        )
    assert event is not None and event.event_type == "TaskFailed"
    assert event.payload_json["execution_id"] == start.execution_id
    assert event.payload_json["worker_name"]
    assert event.payload_json["error"]["code"] == "AUTHENTICATION_FAILED"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deadline_after_repository_clock", "retry_delay", "expected_status"),
    [(40, 20, "retry"), (10, 20, "awaiting_result")],
    ids=["app-clock-ahead-does-not-drop-retry", "retry-would-cross-deadline"],
)
async def test_retry_deadline_uses_repository_clock_and_keeps_capacity(
    worker_store,
    deadline_after_repository_clock: int,
    retry_delay: int,
    expected_status: str,
) -> None:
    """使用偏离实际时钟的仓储时钟，验证重试和 deadline 在同一事务裁决。"""
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    repository, plan_id = await _submit_plan(
        session_factory,
        task_contracts=(contract_id,),
        available_contracts=(contract_id,),
    )
    lease, start = await _claim_and_start(repository)
    assert start is not None
    start = await _finalize_first_send(repository, lease, start)

    async with session_factory() as session:
        actual_database_now = await session.scalar(select(func.clock_timestamp()))
    assert actual_database_now is not None
    repository_now = actual_database_now - timedelta(seconds=30)
    deadline_at = repository_now + timedelta(
        seconds=deadline_after_repository_clock
    )
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, lease.execution_id)
        assert execution is not None
        execution.deadline_at = deadline_at
    repository._clock = lambda: repository_now

    assert await repository.retry_dispatch_command(
        lease, delay_seconds=retry_delay
    )

    async with session_factory() as session:
        command = await session.get(OrchestratorWorkerCommandModel, lease.command_id)
        execution = await session.get(OrchestratorExecutionModel, lease.execution_id)
        task = await session.scalar(
            select(OrchestratorTaskModel).where(
                OrchestratorTaskModel.plan_id == plan_id
            )
        )
    assert command is not None and command.status == expected_status
    assert execution is not None and execution.status == "running"
    assert execution.capacity_reserved
    assert task is not None and task.status == "running"
    if expected_status == "retry":
        assert command.available_at == repository_now + timedelta(seconds=retry_delay)
        assert command.attempt == 1
    else:
        assert command.attempt == 0


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_body", "headers"),
    [
        (b"{}", {"content-length": "2048"}),
        (b"\xff", {}),
        (b'{"code":"A","code":"B"}', {}),
        (b"{", {}),
        (b'{"value":1e400}', {}),
    ],
    ids=["oversize", "invalid-utf8", "duplicate-key", "invalid-json", "non-jcs"],
)
async def test_invalid_http_response_keeps_task_active_and_capacity_reserved(
    worker_store, monkeypatch, response_body: bytes, headers: dict[str, str]
) -> None:
    """真实命令发送后解码失败时只安排同命令重投并保留容量。"""
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    repository, plan_id = await _submit_plan(
        session_factory,
        task_contracts=(contract_id,),
        available_contracts=(contract_id,),
    )

    class Response:
        status_code = 202

        def __init__(self) -> None:
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def aiter_bytes(self):
            yield response_body

    class Client:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        def stream(self, *args, **kwargs):
            return Response()

    class GrantService:
        async def prepare_execution_grant_context(self, execution_id: str) -> object:
            return object()

        def issue_prepared_execution_grant(self, context, start) -> str:
            return "g" * 32

    http = PinnedWorkerHttpClient(
        allowed_origins=("https://worker.invalid",),
        allowed_networks=("127.0.0.0/8",),
        orchestrator_token="orchestrator-token",
        timeout_seconds=5,
    )

    async def resolve_worker(hostname: str, port: int) -> tuple[str, ...]:
        return ("127.0.0.1",)

    monkeypatch.setattr(http, "_resolve_and_check", resolve_worker)
    monkeypatch.setattr("infrastructure.integrations.worker_http.httpx.AsyncClient", Client)
    dispatcher = WorkerTaskDispatcher(
        repository,
        GrantService(),  # type: ignore[arg-type]
        http,
        worker_id=f"decode-test-{uuid4()}",
        max_concurrency=1,
        request_timeout_seconds=5,
        max_response_bytes=1024,
        max_retries=8,
        retry_initial_seconds=1,
        retry_max_seconds=30,
        retry_jitter_ratio=0,
    )
    lease = await repository.claim_dispatch_command(
        owner_id="decode-test-dispatcher", lease_seconds=15
    )
    assert lease is not None

    await dispatcher._dispatch(lease)

    async with session_factory() as session:
        command = await session.get(OrchestratorWorkerCommandModel, lease.command_id)
        execution = await session.get(OrchestratorExecutionModel, lease.execution_id)
        task = await session.scalar(
            select(OrchestratorTaskModel).where(
                OrchestratorTaskModel.plan_id == plan_id
            )
        )
    assert command is not None and command.status == "retry"
    assert command.send_attempt_count == 1
    assert execution is not None and execution.status == "running"
    assert execution.capacity_reserved
    assert task is not None and task.status == "running"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capacity_wait_yields_queue_to_another_available_contract(worker_store) -> None:
    session_factory, _ = worker_store
    unavailable_contract, ready_contract = str(uuid4()), str(uuid4())
    repository, plan_id = await _submit_plan(
        session_factory,
        task_contracts=(unavailable_contract, ready_contract),
        available_contracts=(ready_contract,),
    )
    async with session_factory() as session, session.begin():
        tasks = list(
            (
                await session.scalars(
                    select(OrchestratorTaskModel).where(
                        OrchestratorTaskModel.plan_id == plan_id
                    )
                )
            ).all()
        )
        first_task = next(task for task in tasks if task.contract_id == unavailable_contract)
        first_execution = await session.scalar(
            select(OrchestratorExecutionModel).where(
                OrchestratorExecutionModel.task_id == first_task.task_id
            )
        )
        assert first_execution is not None
        first_command = await session.scalar(
            select(OrchestratorWorkerCommandModel).where(
                OrchestratorWorkerCommandModel.execution_id == first_execution.execution_id
            )
        )
        assert first_command is not None
        first_command.available_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    lease, start = await _claim_and_start(repository)
    assert start is None
    next_lease, next_start = await _claim_and_start(repository)
    assert next_lease.command_id != lease.command_id
    assert next_start is not None
    assert next_start.command["contract_id"] == ready_contract


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parallel_dispatchers_never_reserve_more_than_worker_capacity(
    worker_store,
) -> None:
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    repository, plan_id = await _submit_plan(
        session_factory,
        task_contracts=(contract_id, contract_id),
        available_contracts=(contract_id,),
        instance_capacity=1,
    )
    leases = await asyncio.gather(
        *(
            repository.claim_dispatch_command(
                owner_id=f"dispatcher-{index}", lease_seconds=15
            )
            for index in range(2)
        )
    )
    assert all(lease is not None for lease in leases)
    assert leases[0].command_id != leases[1].command_id
    starts = await asyncio.gather(
        *(
            repository.start_dispatch(
                lease,
                lease_seconds=15,
                assignment_retry_seconds=0.1,
            )
            for lease in leases
        )
    )
    assert sum(start is not None for start in starts) == 1
    async with session_factory() as session:
        reserved = list(
            (
                await session.scalars(
                    select(OrchestratorExecutionModel).where(
                        OrchestratorExecutionModel.capacity_reserved.is_(True)
                    )
                )
            ).all()
        )
        tasks = list(
            (
                await session.scalars(
                    select(OrchestratorTaskModel).where(
                        OrchestratorTaskModel.plan_id == plan_id
                    )
                )
            ).all()
        )
    assert len(reserved) == 1
    assert sum(task.status == "running" for task in tasks) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_dispatch_lease_is_fenced_and_deadline_is_not_reset(worker_store) -> None:
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    repository, _ = await _submit_plan(
        session_factory,
        task_contracts=(contract_id,),
        available_contracts=(contract_id,),
    )
    old_lease = await repository.claim_dispatch_command(
        owner_id="dispatcher-old", lease_seconds=1
    )
    assert old_lease is not None
    first_start = await repository.start_dispatch(
        old_lease,
        lease_seconds=1,
        assignment_retry_seconds=0.1,
    )
    assert first_start is not None
    first_start = await _finalize_first_send(
        repository, old_lease, first_start, lease_seconds=1
    )
    await asyncio.sleep(1.1)
    new_lease = await repository.claim_dispatch_command(
        owner_id="dispatcher-new", lease_seconds=15
    )
    assert new_lease is not None and new_lease.command_id == old_lease.command_id
    assert await repository.prepare_dispatch_send(old_lease, lease_seconds=15) is False
    second_start = await repository.start_dispatch(
        new_lease,
        lease_seconds=15,
        assignment_retry_seconds=0.1,
    )
    assert second_start is not None
    assert second_start.deadline_at == first_start.deadline_at
    assert second_start.started_at == first_start.started_at
    assert second_start.command == first_start.command
    assert await repository.prepare_dispatch_send(new_lease, lease_seconds=15)


async def _wait_until_backend_waits_for_advisory(
    session_factory,
    *,
    timeout_seconds: float = 5,
) -> None:
    """等待 dispatcher 在测试事务持有的 advisory lock 上进入可观测等待。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        async with session_factory() as session:
            blocked = await session.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' "
                    "AND wait_event = 'advisory' "
                    "AND pid <> pg_backend_pid()"
                    ")"
                )
            )
        if blocked:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("dispatcher_did_not_wait_for_trace_advisory")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_and_trace_writes_use_the_same_run_lock_order(
    worker_store,
) -> None:
    """Trace 先持 advisory 时，dispatcher 必须等锁而不能先持 Run 行锁。"""
    session_factory, _ = worker_store
    contract_id = str(uuid4())
    repository, plan_id = await _submit_plan(
        session_factory,
        task_contracts=(contract_id,),
        available_contracts=(contract_id,),
    )
    now = datetime.now(timezone.utc)
    async with session_factory() as session, session.begin():
        plan = await session.get(OrchestratorPlanModel, plan_id)
        assert plan is not None
        run = await session.get(AgentRunModel, plan.run_id)
        assert run is not None
        command = await session.scalar(
            select(OrchestratorWorkerCommandModel)
            .join(
                OrchestratorExecutionModel,
                OrchestratorWorkerCommandModel.execution_id
                == OrchestratorExecutionModel.execution_id,
            )
            .join(
                OrchestratorTaskModel,
                OrchestratorExecutionModel.task_id
                == OrchestratorTaskModel.task_id,
            )
            .where(
                OrchestratorTaskModel.plan_id == plan_id,
                OrchestratorWorkerCommandModel.command_type == "execute",
            )
        )
        assert command is not None
        project_id = f"project-{uuid4().hex}"
        session.add(
            ProjectModel(
                id=project_id,
                name="Trace lock order",
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        await session.flush()
        session.add(
            SessionModel(
                id=run.session_id,
                project_id=project_id,
                title="Trace lock order",
                active_run_id=None,
                last_run_sequence=0,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
                deleted_time=None,
            )
        )
        await session.flush()
        session.add(
            TraceSpanModel(
                span_id=run_span_id(run.run_id),
                session_id=run.session_id,
                run_id=run.run_id,
                parent_span_id=None,
                span_type="run",
                name="run.execute",
                status="running",
                started_at=now,
                completed_at=None,
                duration_ms=None,
                attributes_json=RunSpanAttributes().model_dump(mode="json"),
                error_json=None,
                sequence=1,
            )
        )
        run_id = run.run_id
        session_id = run.session_id
        command_id = command.command_id

    lease = await repository.claim_dispatch_command(
        owner_id="trace-lock-dispatcher",
        lease_seconds=15,
    )
    assert lease is not None and lease.command_id == command_id

    trace_advisory_acquired = asyncio.Event()
    allow_trace_insert = asyncio.Event()

    async def append_root_trace_after_lock() -> None:
        """先持有 Run advisory，再写入 Root 子 span。"""
        async with session_factory() as trace_session, trace_session.begin():
            await lock_trace_span_scope(trace_session, run_id)
            trace_advisory_acquired.set()
            await allow_trace_insert.wait()
            sequence = int(
                await trace_session.scalar(
                    select(
                        func.coalesce(func.max(TraceSpanModel.sequence), 0)
                    ).where(TraceSpanModel.run_id == run_id)
                )
                or 0
            ) + 1
            trace_session.add(
                TraceSpanModel(
                    span_id=f"root-node:{uuid4().hex}",
                    session_id=session_id,
                    run_id=run_id,
                    parent_span_id=run_span_id(run_id),
                    span_type="root_node",
                    name="concurrent_trace_regression",
                    status="completed",
                    started_at=now,
                    completed_at=now,
                    duration_ms=0,
                    attributes_json={
                        "span_type": "root_node",
                        "node_name": "concurrent_trace_regression",
                    },
                    error_json=None,
                    sequence=sequence,
                )
            )

    trace_task = asyncio.create_task(append_root_trace_after_lock())
    dispatch_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(trace_advisory_acquired.wait(), timeout=5)
        dispatch_task = asyncio.create_task(
            repository.start_dispatch(
                lease,
                lease_seconds=15,
                assignment_retry_seconds=0.1,
            )
        )
        # 直接观察 PostgreSQL 的 Lock/advisory 等待状态，不使用固定 sleep；
        # 旧顺序即使已持 Run 行锁，也会在随后申请 advisory 时到达同一状态。
        await _wait_until_backend_waits_for_advisory(
            session_factory,
        )

        # 修复后的 dispatcher 仍停在 advisory；此时 Root 写 span 可取得 Run 的
        # 外键共享锁并提交。旧顺序中 dispatcher 已持 Run 行锁，这两次等待会
        # 形成 advisory 与 Run 行锁的闭环，由 PostgreSQL 稳定检测为死锁。
        allow_trace_insert.set()
        start, _ = await asyncio.wait_for(
            asyncio.gather(dispatch_task, trace_task),
            timeout=5,
        )
    finally:
        allow_trace_insert.set()
        tasks = [trace_task]
        if dispatch_task is not None:
            tasks.append(dispatch_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert start is not None
    async with session_factory() as session:
        command = await session.get(OrchestratorWorkerCommandModel, command_id)
        execution = await session.get(
            OrchestratorExecutionModel, lease.execution_id
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
    assert command is not None and command.instance_id is not None
    assert execution is not None and execution.status == "running"
    assert {span.span_type for span in spans} == {
        "run",
        "root_node",
        "worker_execution",
    }
