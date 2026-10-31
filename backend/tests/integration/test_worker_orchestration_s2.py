"""S2 Worker 逐轮领域与事务仓储的 PostgreSQL 验收。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import IntegrityError

from domain.orchestration.worker_entities import (
    WorkerPlanSubmission,
    WorkerRegistrationIntent,
    WorkerExecutionError,
    WorkerResultSubmission,
    WorkerStopConfirmation,
    WorkerTaskSpec,
)
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    AgentEventModel,
    ApprovalRecordModel,
    Base,
    OrchestrationOutboxModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorRootResumeJobModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    WorkerContractModel,
    WorkerCommandReceiptModel,
    WorkerInstanceModel,
    WorkerRegistrationReceiptModel,
    WorkerResultReceiptModel,
    WorkerStopConfirmationModel,
    ExecutionLeaseModel,
    IdempotencyReceiptModel,
    MemoryProcessingJobModel,
    MemorySourceSuppressionModel,
    MessageModel,
    ProjectModel,
    SessionModel,
)
from application.orchestration.worker_protocol_models import (
    WorkerRegistrationResponseModel,
    WorkerResultReceiptModel as WorkerResultReceiptProtocolModel,
    WorkerStopConfirmationReceiptModel,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
    WorkerIdempotencyConflict,
    WorkerPlanRejected,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")

_S2_TABLES = [
    ProjectModel.__table__,
    SessionModel.__table__,
    AgentRunModel.__table__,
    AgentEventModel.__table__,
    MessageModel.__table__,
    MemoryProcessingJobModel.__table__,
    MemorySourceSuppressionModel.__table__,
    IdempotencyReceiptModel.__table__,
    ApprovalRecordModel.__table__,
    OrchestrationOutboxModel.__table__,
    WorkerContractModel.__table__,
    WorkerInstanceModel.__table__,
    WorkerRegistrationReceiptModel.__table__,
    OrchestratorPlanModel.__table__,
    OrchestratorTaskModel.__table__,
    OrchestratorExecutionModel.__table__,
    WorkerResultReceiptModel.__table__,
    WorkerStopConfirmationModel.__table__,
    OrchestratorRootResumeJobModel.__table__,
    OrchestratorWorkerCommandModel.__table__,
    WorkerCommandReceiptModel.__table__,
    ExecutionLeaseModel.__table__,
]


@pytest_asyncio.fixture
async def worker_store():
    """在随机 schema 中创建 S2 表，并在用例结束后只清理该 schema。

    参数：无。

    返回：临时异步 session factory 与 schema 名称。

    异常：
    - pytest.skip：未配置专用测试 PostgreSQL URL 时跳过。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"worker_s2_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=_S2_TABLES
                )
            )
        yield async_sessionmaker(test_engine, expire_on_commit=False), schema
    finally:
        await test_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_tasks_complete_concurrently_and_resume_once(worker_store) -> None:
    """并发完成不同 Task 时计数不丢失，相同结果重放返回原回执。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id = str(uuid4())
    plan_id = str(uuid4())
    contract_id = str(uuid4())
    instance_id = str(uuid4())
    execution_ids: list[str] = []
    task_ids: list[str] = []

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
        session.add(
            WorkerContractModel(
                contract_id=contract_id,
                revision=1,
                can_do_json=["test"],
                cannot_do_json=["none"],
                input_schema_json={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                output_schema_json={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                fingerprint_hash=f"contract-{contract_id}",
                created_at=now,
            )
        )
        session.add(
            WorkerInstanceModel(
                instance_id=instance_id,
                service_principal="test-service",
                instance_key=str(uuid4()),
                contract_id=contract_id,
                contract_revision=1,
                base_url="https://worker.invalid",
                execution_timeout_seconds=60,
                max_concurrency=2,
                status="ready",
                accepting_tasks=True,
                created_at=now,
                updated_at=now,
            )
        )

    repository = PostgresWorkerOrchestrationRepository(
        session_factory,
        id_factory=lambda: str(uuid4()),
        clock=lambda: now,
    )
    submission = WorkerPlanSubmission(
        run_id=run_id,
        plan_id=plan_id,
        sequence=1,
        idempotency_key="plan-submit-1",
        goal="并行验证两个输入",
        tasks=(
            WorkerTaskSpec(
                task_ref="task-a",
                contract_id=contract_id,
                contract_revision=1,
                objective="执行 A",
                input={"value": "a"},
                acceptance_criteria="返回 ok",
            ),
            WorkerTaskSpec(
                task_ref="task-b",
                contract_id=contract_id,
                contract_revision=1,
                objective="执行 B",
                input={"value": "b"},
                acceptance_criteria="返回 ok",
            ),
        ),
        created_at=now,
    )
    receipts = await asyncio.gather(
        repository.submit_plan(submission), repository.submit_plan(submission)
    )
    assert {receipt.plan_id for receipt in receipts} == {plan_id}
    assert sorted(receipt.replayed for receipt in receipts) == [False, True]

    async with session_factory() as session, session.begin():
        task_rows = list(
            (
                await session.execute(
                    select(OrchestratorTaskModel)
                    .where(OrchestratorTaskModel.plan_id == plan_id)
                    .order_by(OrchestratorTaskModel.task_ref)
                )
            ).scalars()
        )
        task_ids = [task.task_id for task in task_rows]
        execution_rows = list(
            (
                await session.scalars(
                    select(OrchestratorExecutionModel).where(
                        OrchestratorExecutionModel.task_id.in_(task_ids)
                    )
                )
            ).all()
        )
        execution_by_task_id = {row.task_id: row for row in execution_rows}
        for task in task_rows:
            execution = execution_by_task_id[task.task_id]
            task.status = "running"
            task.execution_generation = 1
            task.active_execution_id = execution.execution_id
            task.updated_at = now
            execution_ids.append(execution.execution_id)
            execution.instance_id = instance_id
            execution.status = "running"
            execution.execution_timeout_seconds = 60
            execution.capacity_reserved = True
            execution.started_at = now
            execution.deadline_at = now + timedelta(minutes=1)
            execution.updated_at = now

    results = [
        WorkerResultSubmission(
            execution_id=execution_id,
            instance_id=instance_id,
            generation=1,
            result_id=str(uuid4()),
            status="completed",
            output={"ok": True},
        )
        for task_id, execution_id in zip(task_ids, execution_ids, strict=True)
    ]
    result_receipts = await asyncio.gather(
        repository.record_result(submission=results[0]),
        repository.record_result(submission=results[0]),
        repository.record_result(submission=results[1]),
    )
    assert result_receipts[0].receipt_id == result_receipts[1].receipt_id
    assert sum(receipt.replayed for receipt in result_receipts[:2]) == 1
    with pytest.raises(ValueError, match="worker_result_instance_mismatch"):
        await repository.record_result(
            submission=replace(
                results[0],
                instance_id=str(uuid4()),
                request_hash=repository._hash_json(asdict(results[0])),
            )
        )
    with pytest.raises(WorkerIdempotencyConflict, match="worker_result_idempotency_conflict"):
        await repository.record_result(
            submission=replace(results[0], output={"ok": False})
        )

    async with session_factory() as session:
        plan = await session.get(OrchestratorPlanModel, plan_id)
        resume_jobs = list(
            (
                await session.execute(
                    select(OrchestratorRootResumeJobModel).where(
                        OrchestratorRootResumeJobModel.run_id == run_id
                    )
                )
            ).scalars()
        )
        stored_result_receipts = list(
            (
                await session.scalars(
                    select(WorkerResultReceiptModel).where(
                        WorkerResultReceiptModel.execution_id.in_(execution_ids)
                    )
                )
            ).all()
        )
        resume_lease_rows = list(
            (
                await session.scalars(
                    select(ExecutionLeaseModel).where(
                        ExecutionLeaseModel.resource_type == "root_resume",
                        ExecutionLeaseModel.resource_id.in_(
                            [job.job_id for job in resume_jobs]
                        ),
                    )
                )
            ).all()
        )
        assert plan is not None
        assert plan.terminal_task_count == 2
        assert plan.status == "completed"
        assert len(resume_jobs) == 1
        assert len(resume_lease_rows) == 1
        assert resume_lease_rows[0].state == "pending"
        for stored in stored_result_receipts:
            validated = WorkerResultReceiptProtocolModel.model_validate(
                stored.receipt_json
            )
            assert validated.model_dump(mode="json", exclude_none=True) == stored.receipt_json


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancelled_result_is_ignored_until_stop_confirmation(worker_store) -> None:
    """逻辑取消后结果只留下 ignored 回执，容量由 StopConfirmation 释放。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id = str(uuid4())
    plan_id = str(uuid4())
    contract_id = str(uuid4())
    instance_id = str(uuid4())
    task_id = str(uuid4())
    execution_id = str(uuid4())
    timed_out_cancel_command_id = str(uuid4())
    queued_execution_id = str(uuid4())
    queued_command_id = str(uuid4())
    id_values = iter([str(uuid4()), str(uuid4()), str(uuid4())])
    repository = PostgresWorkerOrchestrationRepository(
        session_factory, id_factory=lambda: next(id_values), clock=lambda: now
    )

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
                can_do_json=["test"],
                cannot_do_json=["none"],
                input_schema_json={"type": "object"},
                output_schema_json={"type": "object"},
                fingerprint_hash=f"contract-{contract_id}",
                created_at=now,
            )
        )
        session.add(
            WorkerInstanceModel(
                instance_id=instance_id,
                service_principal="test-service",
                instance_key=str(uuid4()),
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
                idempotency_key="seed-plan",
                request_hash="seed",
                goal="取消竞争",
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
                task_ref="task-cancel",
                contract_id=contract_id,
                contract_revision=1,
                objective="执行后可取消",
                input_json={},
                acceptance_criteria="返回结果",
                status="running",
                execution_generation=1,
                cancellation_epoch=0,
                active_execution_id=execution_id,
                created_at=now,
                updated_at=now,
            )
        )

    async with session_factory() as session, session.begin():
        session.add(
            OrchestratorExecutionModel(
                execution_id=execution_id,
                task_id=task_id,
                instance_id=instance_id,
                generation=1,
                status="running",
                execution_timeout_seconds=60,
                cancellation_epoch=0,
                capacity_reserved=True,
                started_at=now,
                deadline_at=now + timedelta(minutes=1),
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            OrchestratorWorkerCommandModel(
                command_id=timed_out_cancel_command_id,
                execution_id=execution_id,
                instance_id=instance_id,
                generation=1,
                idempotency_key=timed_out_cancel_command_id,
                command_type="cancel",
                request_hash="timed-out-cancel-hash",
                command_json={
                    "protocol_version": "1.0",
                    "command_id": timed_out_cancel_command_id,
                    "execution_id": execution_id,
                    "generation": 1,
                    "cancellation_epoch": 0,
                    "reason": "execution_timed_out",
                },
                status="retry",
                attempt=1,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            OrchestratorExecutionModel(
                execution_id=queued_execution_id,
                task_id=task_id,
                instance_id=None,
                generation=2,
                status="queued",
                execution_timeout_seconds=None,
                cancellation_epoch=0,
                capacity_reserved=False,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            OrchestratorWorkerCommandModel(
                command_id=queued_command_id,
                execution_id=queued_execution_id,
                instance_id=None,
                generation=2,
                idempotency_key=queued_command_id,
                command_type="execute",
                request_hash="queued-command-hash",
                command_json={"command_id": queued_command_id},
                status="awaiting_assignment",
                attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    cancel_command_id = await repository.cancel_task(run_id=run_id, task_id=task_id)
    assert cancel_command_id is not None
    replayed_cancel_command_id = await repository.cancel_task(
        run_id=run_id, task_id=task_id
    )
    assert replayed_cancel_command_id == cancel_command_id
    async with session_factory() as session:
        queued_execution = await session.get(
            OrchestratorExecutionModel, queued_execution_id
        )
        queued_command = await session.get(
            OrchestratorWorkerCommandModel, queued_command_id
        )
        assert queued_execution is not None and queued_execution.status == "cancelled"
        assert queued_command is not None and queued_command.status == "superseded"
        timed_out_cancel_command = await session.get(
            OrchestratorWorkerCommandModel, timed_out_cancel_command_id
        )
        new_cancel_command = await session.get(
            OrchestratorWorkerCommandModel, cancel_command_id
        )
        assert timed_out_cancel_command is not None
        assert timed_out_cancel_command.status == "superseded"
        assert new_cancel_command is not None and new_cancel_command.status == "pending"
        assert new_cancel_command.command_json["reason"] == "task_cancelled"
        assert new_cancel_command.command_json["cancellation_epoch"] == 1
        task_after_replay = await session.get(OrchestratorTaskModel, task_id)
        execution_after_replay = await session.get(
            OrchestratorExecutionModel, execution_id
        )
        execution_commands = list(
            (
                await session.scalars(
                    select(OrchestratorWorkerCommandModel).where(
                        OrchestratorWorkerCommandModel.execution_id == execution_id
                    )
                )
            ).all()
        )
        logical_cancel_commands = sum(
            command.command_type == "cancel"
            and command.command_json.get("reason") == "task_cancelled"
            and command.command_json.get("cancellation_epoch") == 1
            for command in execution_commands
        )
        assert task_after_replay is not None
        assert task_after_replay.status == "cancelled"
        assert task_after_replay.cancellation_epoch == 1
        assert execution_after_replay is not None
        assert execution_after_replay.cancellation_epoch == 1
        assert logical_cancel_commands == 1

    async def add_command(command_id: str, idempotency_key: str) -> None:
        async with session_factory() as session, session.begin():
            session.add(
                OrchestratorWorkerCommandModel(
                    command_id=command_id,
                    execution_id=execution_id,
                    instance_id=instance_id,
                    generation=1,
                    idempotency_key=idempotency_key,
                    command_type="cancel",
                    request_hash="constraint-test-hash",
                    command_json={"command_id": command_id},
                    status="pending",
                    attempt=0,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )

    with pytest.raises(IntegrityError):
        await add_command(timed_out_cancel_command_id, "unique-other-key")
    with pytest.raises(IntegrityError):
        await add_command(str(uuid4()), cancel_command_id)
    async with session_factory() as session:
        stable_command = await session.get(
            OrchestratorWorkerCommandModel, timed_out_cancel_command_id
        )
        assert stable_command is not None
        assert stable_command.status == "superseded"
        assert stable_command.idempotency_key == timed_out_cancel_command_id

    async with session_factory() as session, session.begin():
        session.add(
            WorkerCommandReceiptModel(
                command_id=cancel_command_id,
                worker_command_id=cancel_command_id,
                instance_id=instance_id,
                execution_id=execution_id,
                idempotency_key="receipt-idempotency-key",
                request_hash="receipt-request-hash",
                receipt_json={"accepted": True},
                created_at=now,
            )
        )
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            session.add(
                WorkerCommandReceiptModel(
                    command_id=cancel_command_id,
                    worker_command_id=timed_out_cancel_command_id,
                    instance_id=instance_id,
                    execution_id=execution_id,
                    idempotency_key="another-receipt-key",
                    request_hash="another-receipt-hash",
                    receipt_json={"accepted": False},
                    created_at=now,
                )
            )
    async with session_factory() as session:
        stable_receipt = await session.get(
            WorkerCommandReceiptModel, cancel_command_id
        )
        assert stable_receipt is not None
        assert stable_receipt.worker_command_id == cancel_command_id
        assert stable_receipt.receipt_json == {"accepted": True}

    result = await repository.record_result(
        submission=WorkerResultSubmission(
            execution_id=execution_id,
            instance_id=instance_id,
            generation=1,
            result_id=str(uuid4()),
            status="completed",
            output={"ok": True},
        )
    )
    assert result.disposition == "ignored"
    assert result.reason == "task_cancelled"

    confirmation = WorkerStopConfirmation(
        instance_id=instance_id,
        stop_confirmation_id=str(uuid4()),
        cancel_command_id=cancel_command_id,
        execution_id=execution_id,
        generation=1,
        cancellation_epoch=1,
        reason="task_cancelled",
        stopped_at=now,
    )
    with pytest.raises(ValueError, match="worker_stop_cancel_command_unknown"):
        await repository.confirm_stopped(
            replace(confirmation, cancel_command_id=str(uuid4()))
        )
    with pytest.raises(ValueError, match="worker_stop_confirmation_command_mismatch"):
        await repository.confirm_stopped(
            replace(confirmation, instance_id=str(uuid4()))
        )
    async with session_factory() as session:
        assert (await session.get(OrchestratorExecutionModel, execution_id)).capacity_reserved
    old_command_stop = await repository.confirm_stopped(
        WorkerStopConfirmation(
            instance_id=instance_id,
            stop_confirmation_id=str(uuid4()),
            cancel_command_id=timed_out_cancel_command_id,
            execution_id=execution_id,
            generation=1,
            cancellation_epoch=0,
            reason="execution_timed_out",
            stopped_at=now,
        )
    )
    assert old_command_stop.disposition == "capacity_released"
    stopped, concurrent_replay = await asyncio.gather(
        repository.confirm_stopped(confirmation),
        repository.confirm_stopped(confirmation),
    )
    assert stopped.disposition == "already_released"
    assert sorted((stopped.replayed, concurrent_replay.replayed)) == [False, True]
    replayed_stop = await repository.confirm_stopped(confirmation)
    assert replayed_stop.replayed is True
    assert replayed_stop.received_at == stopped.received_at
    with pytest.raises(ValueError, match="worker_stop_confirmation_idempotency_conflict"):
        await repository.confirm_stopped(
            replace(confirmation, stopped_at=now + timedelta(seconds=1))
        )

    async with session_factory() as session:
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        task = await session.get(OrchestratorTaskModel, task_id)
        assert execution is not None and execution.capacity_reserved is False
        assert task is not None and task.status == "cancelled"
        persisted_receipt = await session.scalar(
            select(WorkerStopConfirmationModel.receipt_json).where(
                WorkerStopConfirmationModel.confirmation_id
                == confirmation.stop_confirmation_id
            )
        )
        assert persisted_receipt["stop_confirmation_id"] == confirmation.stop_confirmation_id
        assert persisted_receipt["cancel_command_id"] == cancel_command_id
        assert persisted_receipt["received_at"].endswith("Z")
        validated_stop_receipt = WorkerStopConfirmationReceiptModel.model_validate(
            persisted_receipt
        )
        assert validated_stop_receipt.model_dump(mode="json") == persisted_receipt


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plan_sequence_allows_reuse_of_task_ref_and_rejects_active_or_stopped_runs(
    worker_store,
) -> None:
    """Plan 可逐轮追加并复用局部 task_ref，活动计划和停止 Run 拒绝新计划。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id = str(uuid4())
    stopped_run_id = str(uuid4())
    contract_id = str(uuid4())

    async with session_factory() as session, session.begin():
        for current_run_id, status, sequence in (
            (run_id, "running", 1),
            (stopped_run_id, "stopped", 0),
        ):
            session.add(
                AgentRunModel(
                    run_id=current_run_id,
                    session_id=f"session-{current_run_id}",
                    message_id=f"message-{current_run_id}",
                    sequence=1,
                    status=status,
                    request_json={},
                    root_thread_id=f"root-{current_run_id}",
                    root_wait_generation=0,
                    orchestrator_plan_sequence=sequence,
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
                can_do_json=["test"],
                cannot_do_json=["none"],
                input_schema_json={"type": "object"},
                output_schema_json={"type": "object"},
                fingerprint_hash=f"contract-{contract_id}",
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorPlanModel(
                plan_id="prior-plan",
                run_id=run_id,
                sequence=1,
                idempotency_key="prior-plan-key",
                request_hash="prior-plan-hash",
                goal="第一轮",
                status="completed",
                expected_task_count=1,
                terminal_task_count=1,
                root_wait_generation=1,
                created_at=now,
                completed_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorTaskModel(
                task_id="prior-task",
                operation_id="prior-operation",
                plan_id="prior-plan",
                task_ref="shared-ref",
                contract_id=contract_id,
                contract_revision=1,
                objective="第一轮任务",
                input_json={},
                acceptance_criteria="完成",
                status="completed",
                execution_generation=1,
                cancellation_epoch=0,
                created_at=now,
                updated_at=now,
            )
        )

    repository = PostgresWorkerOrchestrationRepository(
        session_factory, id_factory=lambda: str(uuid4()), clock=lambda: now
    )

    def submission(*, target_run: str, target_plan: str, sequence: int) -> WorkerPlanSubmission:
        return WorkerPlanSubmission(
            run_id=target_run,
            plan_id=target_plan,
            sequence=sequence,
            idempotency_key=f"plan-{target_plan}",
            goal="下一轮",
            tasks=(
                WorkerTaskSpec(
                    task_ref="shared-ref",
                    contract_id=contract_id,
                    contract_revision=1,
                    objective="执行新一轮任务",
                    input={},
                    acceptance_criteria="完成",
                ),
            ),
            created_at=now,
        )

    receipt = await repository.submit_plan(
        submission(target_run=run_id, target_plan="plan-2", sequence=2)
    )
    assert receipt.plan_id == "plan-2"
    async with session_factory() as session:
        repeated_refs = list(
            (
                await session.scalars(
                    select(OrchestratorTaskModel.task_ref).where(
                        OrchestratorTaskModel.task_ref == "shared-ref"
                    )
                )
            ).all()
        )
    assert repeated_refs.count("shared-ref") == 2

    with pytest.raises(WorkerPlanRejected, match="worker_run_has_active_plan"):
        await repository.submit_plan(
            submission(target_run=run_id, target_plan="plan-3", sequence=3)
        )
    with pytest.raises(WorkerPlanRejected, match="worker_run_not_accepting_plans"):
        await repository.submit_plan(
            submission(target_run=stopped_run_id, target_plan="stopped-plan", sequence=1)
        )

    async with session_factory() as session:
        assert await session.get(OrchestratorPlanModel, "plan-3") is None
        assert await session.get(OrchestratorPlanModel, "stopped-plan") is None
        assert not list(
            (
                await session.scalars(
                    select(OrchestratorRootResumeJobModel).where(
                        OrchestratorRootResumeJobModel.run_id.in_((run_id, stopped_run_id))
                    )
                )
            ).all()
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_stop_without_worker_plan_finishes_using_target_schema(worker_store) -> None:
    """没有 Worker Plan 的普通 Run 可停止并完成终态清理。"""
    from infrastructure.persistence.postgres.repositories.run_repository import (
        PostgresRunRepository,
    )

    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id = str(uuid4())
    session_id = f"session-{run_id}"
    async with session_factory() as session, session.begin():
        session.add(
            ProjectModel(
                id=f"project-{run_id}",
                name="S2 run lifecycle",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SessionModel(
                id=session_id,
                project_id=f"project-{run_id}",
                title="S2 run lifecycle",
                active_run_id=run_id,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
        )
        session.add(
            AgentRunModel(
                run_id=run_id,
                session_id=session_id,
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

    repository = PostgresRunRepository(session_factory)
    result, replayed = await repository.stop_active(
        session_id,
        request_id=f"stop-{run_id}",
        expected_run_id=run_id,
        displayed_session_seq=0,
    )
    assert not replayed
    assert result.status.value == "stopping"
    assert await repository.finalize_stop(run_id)
    async with session_factory() as session:
        run = await session.get(AgentRunModel, run_id)
        session_row = await session.get(SessionModel, session_id)
        assert run is not None and run.status == "cancelled"
        assert session_row is not None and session_row.active_run_id is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shared_contract_revision_tracks_each_instances_bound_version(worker_store) -> None:
    """共享契约只在实例自身能力变化时递增，历史内容回切也产生新版本。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    repository = PostgresWorkerOrchestrationRepository(
        session_factory, id_factory=lambda: str(uuid4()), clock=lambda: now
    )

    def intent(
        *,
        request_id: str,
        instance_key: str,
        capability: str,
        base_url: str = "https://worker.invalid",
    ) -> WorkerRegistrationIntent:
        return WorkerRegistrationIntent(
            registration_request_id=request_id,
            service_principal="test-service",
            instance_key=str(uuid5(NAMESPACE_URL, instance_key)),
            base_url=base_url,
            execution_timeout_seconds=60,
            max_concurrency=2,
            can_do=(capability,),
            cannot_do=("不执行其他任务",),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )

    first_a = await repository.register_worker(
        intent(request_id="register-a-1", instance_key="instance-a", capability="A")
    )
    first_b = await repository.register_worker(
        intent(request_id="register-b-1", instance_key="instance-b", capability="A")
    )
    assert first_b.contract_id == first_a.contract_id
    changed_b = await repository.register_worker(
        intent(request_id="register-b-2", instance_key="instance-b", capability="B")
    )
    assert changed_b.contract_id == first_b.contract_id
    assert changed_b.contract_revision == 2

    unchanged_a = await repository.register_worker(
        intent(
            request_id="register-a-2",
            instance_key="instance-a",
            capability="A",
            base_url="https://worker-a.invalid",
        )
    )
    assert unchanged_a.contract_revision == 1
    changed_a = await repository.register_worker(
        intent(request_id="register-a-3", instance_key="instance-a", capability="C")
    )
    assert changed_a.contract_id == first_a.contract_id
    assert changed_a.contract_revision == 3
    changed_back = await repository.register_worker(
        intent(request_id="register-b-3", instance_key="instance-b", capability="A")
    )
    assert changed_back.contract_revision == 4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registration_idempotency_and_shared_revision_races_are_serialized(
    worker_store,
) -> None:
    """并发复用注册请求 ID 返回冲突，两个共享契约升级按版本串行化。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    repository = PostgresWorkerOrchestrationRepository(
        session_factory, id_factory=lambda: str(uuid4()), clock=lambda: now
    )

    def intent(
        request_id: str, instance_key: str, capability: str
    ) -> WorkerRegistrationIntent:
        return WorkerRegistrationIntent(
            registration_request_id=request_id,
            service_principal="test-service",
            instance_key=str(uuid5(NAMESPACE_URL, instance_key)),
            base_url="https://worker.invalid",
            execution_timeout_seconds=60,
            max_concurrency=2,
            can_do=(capability,),
            cannot_do=("不执行其他任务",),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )

    a = await repository.register_worker(intent("create-a", "a", "A"))
    b = await repository.register_worker(intent("create-b", "b", "A"))
    assert a.contract_id == b.contract_id

    outcomes = await asyncio.gather(
        repository.register_worker(intent("upgrade-a", "a", "B")),
        repository.register_worker(intent("upgrade-b", "b", "C")),
        return_exceptions=True,
    )
    assert not any(isinstance(result, Exception) for result in outcomes)
    assert sorted(result.contract_revision for result in outcomes) == [2, 3]

    same_request = await asyncio.gather(
        repository.register_worker(intent("same-request", "a", "D")),
        repository.register_worker(intent("same-request", "a", "E")),
        return_exceptions=True,
    )
    assert sum(isinstance(result, WorkerIdempotencyConflict) for result in same_request) == 1
    assert sum(not isinstance(result, Exception) for result in same_request) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_registrations_share_immutable_contract_revision(worker_store) -> None:
    """不同 Worker 实例并发注册同一能力时复用单一契约版本并持久完整响应。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    repository = PostgresWorkerOrchestrationRepository(
        session_factory, id_factory=lambda: str(uuid4()), clock=lambda: now
    )

    def intent(index: int) -> WorkerRegistrationIntent:
        return WorkerRegistrationIntent(
            registration_request_id=str(uuid4()),
            service_principal="test-service",
            instance_key=str(uuid4()),
            base_url=f"https://worker-{index}.invalid",
            execution_timeout_seconds=90,
            max_concurrency=2,
            can_do=("处理结构化输入",),
            cannot_do=("不访问输入之外的数据",),
            input_schema={"type": "object", "additionalProperties": True},
            output_schema={"type": "object", "additionalProperties": True},
        )

    receipts = await asyncio.gather(
        repository.register_worker(intent(1)),
        repository.register_worker(intent(2)),
    )
    assert receipts[0].contract_id == receipts[1].contract_id
    assert receipts[0].contract_revision == receipts[1].contract_revision == 1

    async with session_factory() as session:
        contracts = list((await session.scalars(select(WorkerContractModel))).all())
        instances = list((await session.scalars(select(WorkerInstanceModel))).all())
        registration_receipts = list(
            (await session.scalars(select(WorkerRegistrationReceiptModel))).all()
        )
    assert len(contracts) == 1
    assert len(instances) == 2
    assert len(registration_receipts) == 2
    for receipt in registration_receipts:
        response = receipt.response_json
        assert response["status"] == "inactive"
        assert response["registered_at"].endswith("Z")
        assert response["worker_instance_id"] == receipt.instance_id
        validated_response = WorkerRegistrationResponseModel.model_validate(response)
        assert validated_response.model_dump(mode="json") == response


@pytest.mark.integration
@pytest.mark.asyncio
async def test_result_classification_persists_retry_and_ignores_stale_results(worker_store) -> None:
    """重试、未知错误、输出 Schema、deadline 与错误实例都按当前协议裁定。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, contract_id, instance_id = (str(uuid4()) for _ in range(4))
    task_ids = [str(uuid4()) for _ in range(6)]
    execution_ids = [str(uuid4()) for _ in range(6)]
    repository = PostgresWorkerOrchestrationRepository(
        session_factory, id_factory=lambda: str(uuid4()), clock=lambda: now
    )

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
                can_do_json=["test"],
                cannot_do_json=["none"],
                input_schema_json={"type": "object"},
                output_schema_json={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                fingerprint_hash=f"contract-{contract_id}",
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            WorkerInstanceModel(
                instance_id=instance_id,
                service_principal="test-service",
                instance_key=str(uuid4()),
                contract_id=contract_id,
                contract_revision=1,
                base_url="https://worker.invalid",
                execution_timeout_seconds=60,
                max_concurrency=5,
                status="ready",
                accepting_tasks=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            OrchestratorPlanModel(
                plan_id=plan_id,
                run_id=run_id,
                sequence=1,
                idempotency_key="result-plan",
                request_hash="result-plan-hash",
                goal="验证结果裁定",
                status="running",
                expected_task_count=6,
                terminal_task_count=0,
                root_wait_generation=1,
                created_at=now,
            )
        )
        await session.flush()
        for index, (task_id, execution_id) in enumerate(
            zip(task_ids, execution_ids, strict=True)
        ):
            session.add(
                OrchestratorTaskModel(
                    task_id=task_id,
                    operation_id=str(uuid4()),
                    plan_id=plan_id,
                    task_ref=f"result-{index}",
                    contract_id=contract_id,
                    contract_revision=1,
                    objective=f"验证任务 {index}",
                    input_json={},
                    acceptance_criteria="按规则裁定",
                    status="running",
                    execution_generation=3 if index == 5 else 1,
                    cancellation_epoch=0,
                    active_execution_id=execution_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.flush()
        for index, (task_id, execution_id) in enumerate(
            zip(task_ids, execution_ids, strict=True)
        ):
            session.add(
                OrchestratorExecutionModel(
                    execution_id=execution_id,
                    task_id=task_id,
                    instance_id=instance_id,
                    generation=3 if index == 5 else 1,
                    status="running",
                    execution_timeout_seconds=60,
                    cancellation_epoch=0,
                    capacity_reserved=True,
                    started_at=now,
                    deadline_at=(now - timedelta(seconds=1) if index == 3 else now + timedelta(minutes=1)),
                    created_at=now,
                    updated_at=now,
                )
            )

    def result(
        index: int,
        *,
        status: str,
        output=None,
        error=None,
        worker_instance=instance_id,
        generation=1,
    ):
        return WorkerResultSubmission(
            execution_id=execution_ids[index],
            instance_id=worker_instance,
            generation=generation,
            result_id=str(uuid4()),
            status=status,
            output=output,
            error=error,
        )

    retry = await repository.record_result(
        submission=result(
            0,
            status="failed",
            error=WorkerExecutionError(
                code="WORKER_UPSTREAM_TIMEOUT",
                message="upstream timeout",
                retryable=True,
            ),
        )
    )
    assert retry.disposition == "retry_scheduled"
    assert retry.reason == "worker_error_retryable"
    assert retry.next_execution_id is not None

    unknown = await repository.record_result(
        submission=result(
            1,
            status="failed",
            error=WorkerExecutionError(
                code="VENDOR_SESSION_BROKEN",
                message="unknown vendor failure",
                retryable=True,
            ),
        )
    )
    invalid_output = await repository.record_result(
        submission=result(2, status="completed", output={"unexpected": True})
    )
    late = await repository.record_result(
        submission=result(3, status="completed", output={"ok": True})
    )
    exhausted = await repository.record_result(
        submission=result(
            5,
            status="failed",
            generation=3,
            error=WorkerExecutionError(
                code="WORKER_UPSTREAM_TIMEOUT",
                message="attempts exhausted",
                retryable=True,
            ),
        )
    )
    with pytest.raises(ValueError, match="worker_result_instance_mismatch"):
        await repository.record_result(
            submission=result(
                4,
                status="completed",
                output={"ok": True},
                worker_instance=str(uuid4()),
            )
        )

    assert unknown.disposition == "recorded"
    assert unknown.reason == "worker_error_final"
    assert invalid_output.reason == "output_schema_invalid"
    assert late.disposition == "ignored"
    assert late.reason == "execution_deadline_exceeded"
    assert exhausted.disposition == "recorded"
    assert exhausted.reason == "execution_attempts_exhausted"
    assert exhausted.next_execution_id is None

    async with session_factory() as session:
        plan = await session.get(OrchestratorPlanModel, plan_id)
        retry_task = await session.get(OrchestratorTaskModel, task_ids[0])
        retry_execution = await session.get(
            OrchestratorExecutionModel, retry.next_execution_id
        )
        retry_command = await session.scalar(
            select(OrchestratorWorkerCommandModel).where(
                OrchestratorWorkerCommandModel.execution_id == retry.next_execution_id
            )
        )
        deadline_execution = await session.get(OrchestratorExecutionModel, execution_ids[3])
        stale_receipt = await session.scalar(
            select(WorkerResultReceiptModel).where(
                WorkerResultReceiptModel.execution_id == execution_ids[4]
            )
        )
        assert plan is not None and plan.terminal_task_count == 3
        assert retry_task is not None and retry_task.status == "retry_pending"
        assert retry_execution is not None and retry_execution.generation == 2
        assert retry_command is not None and retry_command.status == "awaiting_assignment"
        assert deadline_execution is not None and deadline_execution.capacity_reserved is True
        assert stale_receipt is None
