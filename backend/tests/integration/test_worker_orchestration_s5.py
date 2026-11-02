"""S5 超时重试、取消收敛与 PostgreSQL 并发验收。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import func, select

from domain.orchestration.worker_entities import (
    WorkerExecutionError,
    WorkerPlanSubmission,
    WorkerResultSubmission,
    WorkerStopConfirmation,
    WorkerTaskSpec,
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
    WorkerContractModel,
    WorkerInstanceModel,
    WorkerApprovalRequestModel,
    WorkerResultReceiptModel,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
    WorkerPlanRejected,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from application.orchestration.worker_timeouts import WorkerTimeoutService
from tests.integration.test_worker_orchestration_s2 import worker_store


async def _seed_single_execution(
    session_factory,
    *,
    now: datetime,
    queued: bool,
) -> tuple[str, str, str, str, str | None]:
    """建立一个可独立测试队列或执行超时的 Run/Plan/Task/Execution。"""
    run_id, plan_id, task_id, execution_id = (str(uuid4()) for _ in range(4))
    contract_id = str(uuid4())
    instance_id = None if queued else str(uuid4())
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
        if instance_id is not None:
            session.add(
                WorkerInstanceModel(
                    instance_id=instance_id,
                    service_principal="test-service",
                    instance_key=f"worker-{instance_id}",
                    worker_name="Test Worker",
                    contract_id=contract_id,
                    contract_revision=1,
                    base_url="https://worker.invalid",
                    execution_timeout_seconds=1,
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
                idempotency_key=f"plan-{plan_id}",
                request_hash=f"hash-{plan_id}",
                goal="S5 timeout boundary",
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
                task_ref="task-1",
                contract_id=contract_id,
                contract_revision=1,
                objective="test timeout",
                input_json={},
                acceptance_criteria="complete",
                status="pending" if queued else "running",
                execution_generation=1,
                cancellation_epoch=0,
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
                generation=1,
                status="queued" if queued else "running",
                execution_timeout_seconds=None if queued else 1,
                cancellation_epoch=0,
                capacity_reserved=False if queued else True,
                started_at=None if queued else now - timedelta(seconds=2),
                deadline_at=None if queued else now - timedelta(seconds=1),
                created_at=now - timedelta(seconds=301) if queued else now - timedelta(seconds=2),
                updated_at=now,
            )
        )
        session.add(
            OrchestratorWorkerCommandModel(
                command_id=str(uuid4()),
                execution_id=execution_id,
                instance_id=instance_id,
                generation=1,
                idempotency_key=str(uuid4()),
                command_type="execute",
                request_hash=f"execute-{execution_id}",
                command_json={"execution_id": execution_id},
                status="awaiting_assignment" if queued else "awaiting_result",
                attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    return run_id, plan_id, task_id, execution_id, instance_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_result_timeout_and_task_cancel_race_converges_once(worker_store) -> None:
    """结果、两个超时扫描器和逻辑取消竞争时只终结一次并只唤醒 Root 一次。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id = (str(uuid4()) for _ in range(4))
    contract_id, instance_id = str(uuid4()), str(uuid4())
    repository = PostgresWorkerOrchestrationRepository(
        session_factory,
        clock=lambda: now,
        max_execution_attempts=3,
        execution_retry_initial_seconds=2,
        execution_retry_max_seconds=4,
        execution_retry_jitter_ratio=0,
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
                root_wait_generation=0,
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
        await session.flush()
        session.add(
            WorkerInstanceModel(
                instance_id=instance_id,
                service_principal="test-service",
                instance_key=f"worker-{instance_id}",
                worker_name="Test Worker",
                contract_id=contract_id,
                contract_revision=1,
                base_url="https://worker.invalid",
                execution_timeout_seconds=1,
                max_concurrency=1,
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
                idempotency_key=f"plan-{plan_id}",
                request_hash="plan-hash",
                goal="timeout race",
                status="running",
                expected_task_count=1,
                terminal_task_count=0,
                root_wait_generation=1,
                created_at=now,
            )
        )
        session.add(
            OrchestratorTaskModel(
                task_id=task_id,
                operation_id=str(uuid4()),
                plan_id=plan_id,
                task_ref="task-1",
                contract_id=contract_id,
                contract_revision=1,
                objective="test timeout",
                input_json={},
                acceptance_criteria="complete",
                status="running",
                execution_generation=1,
                cancellation_epoch=0,
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
                generation=1,
                status="running",
                worker_name="Test Worker",
                execution_timeout_seconds=1,
                cancellation_epoch=0,
                capacity_reserved=True,
                started_at=now - timedelta(seconds=2),
                deadline_at=now - timedelta(seconds=1),
                created_at=now - timedelta(seconds=2),
                updated_at=now - timedelta(seconds=2),
            )
        )
        session.add(
            OrchestratorWorkerCommandModel(
                command_id=str(uuid4()),
                execution_id=execution_id,
                instance_id=instance_id,
                generation=1,
                idempotency_key=str(uuid4()),
                command_type="execute",
                request_hash="execute-hash",
                command_json={"command_id": str(uuid4())},
                status="awaiting_result",
                attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    outcomes = await asyncio.gather(
        repository.timeout_execution(execution_id=execution_id),
        repository.timeout_execution(execution_id=execution_id),
        repository.record_result(
            submission=WorkerResultSubmission(
                execution_id=execution_id,
                instance_id=instance_id,
                generation=1,
                result_id=str(uuid4()),
                status="completed",
                output={"ok": True},
            )
        ),
        repository.cancel_task(run_id=run_id, task_id=task_id),
    )
    assert outcomes[3] is not None

    async with session_factory() as session:
        executions = list(
            (
                await session.scalars(
                    select(OrchestratorExecutionModel).where(
                        OrchestratorExecutionModel.task_id == task_id
                    )
                )
            ).all()
        )
        receipts = list(
            (
                await session.scalars(
                    select(WorkerResultReceiptModel).where(
                        WorkerResultReceiptModel.execution_id == execution_id
                    )
                )
            ).all()
        )
        commands = list(
            (
                await session.scalars(
                    select(OrchestratorWorkerCommandModel).where(
                        OrchestratorWorkerCommandModel.execution_id == execution_id
                    )
                )
            ).all()
        )
        queued_retry = await session.scalar(
            select(OrchestratorWorkerCommandModel)
            .join(
                OrchestratorExecutionModel,
                OrchestratorExecutionModel.execution_id
                == OrchestratorWorkerCommandModel.execution_id,
            )
            .where(
                OrchestratorExecutionModel.task_id == task_id,
                OrchestratorWorkerCommandModel.command_type == "execute",
                OrchestratorWorkerCommandModel.generation == 2,
            )
        )
        task = await session.get(OrchestratorTaskModel, task_id)
        cancellation_event = await session.scalar(
            select(OrchestrationOutboxModel).where(
                OrchestrationOutboxModel.dedupe_key
                == f"execution:{execution_id}:cancelling:1"
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
        assert len(executions) in {1, 2}
        assert len({receipt.result_id for receipt in receipts}) == len(receipts)
        assert all(receipt.disposition in {"recorded", "retry_scheduled", "ignored"} for receipt in receipts)
        cancel_commands = [
            command for command in commands if command.command_type == "cancel"
        ]
        assert len(cancel_commands) == 2
        assert {command.command_json["reason"] for command in cancel_commands} == {
            "execution_timed_out",
            "task_cancelled",
        }
        assert task is not None and task.status == "cancelled"
        assert cancellation_event is not None
        assert cancellation_event.event_type == "TaskCancelling"
        assert cancellation_event.payload_json["execution_id"] == execution_id
        assert cancellation_event.payload_json["worker_name"] == "Test Worker"
        assert cancellation_event.payload_json["status"] == "cancelling"
        assert task.cancellation_epoch == 1
        assert executions[0].status in {"failed", "cancelled", "cancelling"}
        assert executions[0].capacity_reserved or any(
            command.command_type == "cancel" and command.status == "stopped"
            for command in commands
        )
        assert len(resume_jobs) == 1
        assert resume_jobs[0].status in {"pending", "leased", "completed"}
        if queued_retry is not None:
            assert queued_retry.available_at == now + timedelta(seconds=2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stop_active_persists_cancel_for_reserved_worker_execution(worker_store) -> None:
    """用户停止 Run 时为仍占用 Worker 容量的 Execution 写入取消命令。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id = (str(uuid4()) for _ in range(4))
    contract_id, instance_id = str(uuid4()), str(uuid4())
    session_id = f"session-{run_id}"

    async with session_factory() as session, session.begin():
        session.add(
            ProjectModel(
                id=f"project-{run_id}", name="S5 stop", created_at=now, updated_at=now
            )
        )
        session.add(
            SessionModel(
                id=session_id,
                project_id=f"project-{run_id}",
                title="S5 stop",
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
                instance_key=f"worker-{instance_id}",
                worker_name="Test Worker",
                contract_id=contract_id,
                contract_revision=1,
                base_url="https://worker.invalid",
                execution_timeout_seconds=30,
                max_concurrency=1,
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
                idempotency_key=f"plan-{plan_id}",
                request_hash="plan-hash",
                goal="cancel active worker",
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
                task_ref="task-1",
                contract_id=contract_id,
                contract_revision=1,
                objective="cancel with run",
                input_json={},
                acceptance_criteria="stopped",
                status="running",
                execution_generation=1,
                cancellation_epoch=0,
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
                generation=1,
                status="running",
                execution_timeout_seconds=30,
                cancellation_epoch=0,
                capacity_reserved=True,
                started_at=now,
                deadline_at=now + timedelta(seconds=30),
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
    assert not replayed and result.status.value == "stopping"
    async with session_factory() as session:
        cancel_command = await session.scalar(
            select(OrchestratorWorkerCommandModel).where(
                OrchestratorWorkerCommandModel.execution_id == execution_id,
                OrchestratorWorkerCommandModel.command_type == "cancel",
            )
        )
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        task = await session.get(OrchestratorTaskModel, task_id)
        assert cancel_command is not None and cancel_command.status == "pending"
        assert cancel_command.command_json["reason"] == "run_cancelled"
        assert execution is not None and execution.capacity_reserved
        assert task is not None and task.status == "cancelled"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_queue_timeout_fails_with_capability_unavailable(worker_store) -> None:
    """无可用 Worker 的执行排队到期后形成明确终态并唤醒 Root。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    _, plan_id, task_id, execution_id, _ = await _seed_single_execution(
        session_factory, now=now, queued=True
    )
    repository = PostgresWorkerOrchestrationRepository(
        session_factory, clock=lambda: now
    )

    assert await repository.expire_queued_execution(
        execution_id=execution_id, queue_max_wait_seconds=300
    )
    async with session_factory() as session:
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        task = await session.get(OrchestratorTaskModel, task_id)
        plan = await session.get(OrchestratorPlanModel, plan_id)
        resume_jobs = list(
            (
                await session.scalars(
                    select(OrchestratorRootResumeJobModel).where(
                        OrchestratorRootResumeJobModel.plan_id == plan_id
                    )
                )
            ).all()
        )
        assert execution is not None and execution.error_json["code"] == "CAPABILITY_UNAVAILABLE"
        assert task is not None and task.status == "failed"
        assert plan is not None and plan.terminal_task_count == 1
        assert len(resume_jobs) == 1
    assert not await repository.expire_queued_execution(
        execution_id=execution_id, queue_max_wait_seconds=300
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_plan_without_worker_times_out_and_fences_task(worker_store) -> None:
    """首次 Plan 排队无 Worker 时，活动 execution fence 允许超时收束。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, contract_id = (str(uuid4()) for _ in range(3))
    clock = [now]
    repository = PostgresWorkerOrchestrationRepository(
        session_factory,
        id_factory=lambda: str(uuid4()),
        clock=lambda: clock[0],
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
                input_schema_json={"type": "object"},
                output_schema_json={"type": "object"},
                fingerprint_hash=f"contract-{contract_id}",
                created_at=now,
            )
        )

    await repository.submit_plan(
        WorkerPlanSubmission(
            run_id=run_id,
            plan_id=plan_id,
            sequence=1,
            idempotency_key=f"plan-{plan_id}",
            goal="等待 Worker 容量",
            tasks=(
                WorkerTaskSpec(
                    task_ref="task-1",
                    contract_id=contract_id,
                    contract_revision=1,
                    objective="执行任务",
                    input={},
                    acceptance_criteria="完成",
                ),
            ),
            created_at=now,
        )
    )
    async with session_factory() as session:
        task = await session.scalar(
            select(OrchestratorTaskModel).where(
                OrchestratorTaskModel.plan_id == plan_id
            )
        )
        assert task is not None
        execution = await session.scalar(
            select(OrchestratorExecutionModel).where(
                OrchestratorExecutionModel.task_id == task.task_id
            )
        )
        assert task is not None and execution is not None
        assert task.active_execution_id == execution.execution_id

    clock[0] = now + timedelta(seconds=301)
    candidates = await repository.find_timeout_candidates(
        batch_size=10, queue_max_wait_seconds=300
    )
    assert execution.execution_id in candidates
    assert await repository.expire_queued_execution(
        execution_id=execution.execution_id, queue_max_wait_seconds=300
    )
    async with session_factory() as session:
        task = await session.get(OrchestratorTaskModel, task.task_id)
        execution = await session.get(OrchestratorExecutionModel, execution.execution_id)
        assert task is not None and task.status == "failed"
        assert execution is not None and execution.error_json["code"] == "CAPABILITY_UNAVAILABLE"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_late_execution_result_does_not_consume_pending_resume(worker_store) -> None:
    """审批等待期的迟到结果不得把仍待派发的同代 ResumeCommand 标为完成。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    approval_id, pause_id, resume_id = (str(uuid4()) for _ in range(3))
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        task = await session.get(OrchestratorTaskModel, task_id)
        assert execution is not None and task is not None and instance_id is not None
        execution.status = "waiting_approval"
        execution.current_approval_request_id = approval_id
        execution.current_pause_id = pause_id
        task.status = "waiting_approval"
        session.add(
            WorkerApprovalRequestModel(
                receipt_id=str(uuid4()),
                instance_id=instance_id,
                approval_request_id=approval_id,
                pause_id=pause_id,
                run_id=run_id,
                plan_id=plan_id,
                task_id=task_id,
                execution_id=execution_id,
                generation=1,
                cancellation_epoch=0,
                request_hash="approval-request-hash",
                request_json={},
                status="resuming",
                expires_at=now + timedelta(minutes=1),
                received_at=now,
                receipt_json={},
                resume_command_id=resume_id,
            )
        )
        session.add(
            OrchestratorWorkerCommandModel(
                command_id=resume_id,
                execution_id=execution_id,
                instance_id=instance_id,
                generation=1,
                idempotency_key=resume_id,
                command_type="resume",
                request_hash="resume-command-hash",
                command_json={"execution_id": execution_id},
                status="pending",
                attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    repository = PostgresWorkerOrchestrationRepository(
        session_factory, clock=lambda: now
    )
    receipt = await repository.record_result(
        submission=WorkerResultSubmission(
            execution_id=execution_id,
            instance_id=instance_id,
            generation=1,
            result_id=str(uuid4()),
            status="completed",
            output={"ok": True},
        )
    )
    assert receipt is not None and receipt.disposition == "ignored"
    async with session_factory() as session:
        resume_command = await session.get(OrchestratorWorkerCommandModel, resume_id)
        execution_command = await session.scalar(
            select(OrchestratorWorkerCommandModel).where(
                OrchestratorWorkerCommandModel.execution_id == execution_id,
                OrchestratorWorkerCommandModel.command_type == "execute",
            )
        )
        assert resume_command is not None and resume_command.status == "pending"
        assert execution_command is not None and execution_command.status == "awaiting_result"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuilt_timeout_services_share_one_atomic_timeout_decision(worker_store) -> None:
    """两个独立扫描器重建服务后仍只产生一次超时事实和一次 Root 唤醒。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    _, plan_id, task_id, execution_id, _ = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    repository_a = PostgresWorkerOrchestrationRepository(
        session_factory, clock=lambda: now, max_execution_attempts=1
    )
    repository_b = PostgresWorkerOrchestrationRepository(
        session_factory, clock=lambda: now, max_execution_attempts=1
    )
    service_a = WorkerTimeoutService(repository_a)
    service_b = WorkerTimeoutService(repository_b)

    handled = await asyncio.gather(
        service_a.scan_once(batch_size=100, queue_max_wait_seconds=300),
        service_b.scan_once(batch_size=100, queue_max_wait_seconds=300),
    )
    assert 1 <= sum(handled) <= 2
    async with session_factory() as session:
        receipts = list(
            (
                await session.scalars(
                    select(WorkerResultReceiptModel).where(
                        WorkerResultReceiptModel.execution_id == execution_id
                    )
                )
            ).all()
        )
        cancel_commands = list(
            (
                await session.scalars(
                    select(OrchestratorWorkerCommandModel).where(
                        OrchestratorWorkerCommandModel.execution_id == execution_id,
                        OrchestratorWorkerCommandModel.command_type == "cancel",
                    )
                )
            ).all()
        )
        plan = await session.get(OrchestratorPlanModel, plan_id)
        task = await session.get(OrchestratorTaskModel, task_id)
        resume_jobs = list(
            (
                await session.scalars(
                    select(OrchestratorRootResumeJobModel).where(
                        OrchestratorRootResumeJobModel.plan_id == plan_id
                    )
                )
            ).all()
        )
        assert len(receipts) == 1
        assert len(cancel_commands) == 1
        assert task is not None and task.status == "failed"
        assert plan is not None and plan.terminal_task_count == 1
        assert len(resume_jobs) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_last_attempt_timeout_persists_cancel_until_stop_confirmation(worker_store) -> None:
    """最后一次尝试超时仍发送取消指令，且仅停止确认释放容量。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    current_time = [now]
    _, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    assert instance_id is not None
    repository = PostgresWorkerOrchestrationRepository(
        session_factory,
        clock=lambda: current_time[0],
        max_execution_attempts=1,
        execution_retry_initial_seconds=2,
        execution_retry_max_seconds=4,
        execution_retry_jitter_ratio=0,
    )

    timeout_result_id = str(
        uuid5(NAMESPACE_URL, f"athena:execution-timeout:{execution_id}")
    )
    late_worker = WorkerResultSubmission(
        execution_id=execution_id,
        instance_id=instance_id,
        generation=1,
        result_id=timeout_result_id,
        status="completed",
        output={"ok": True},
    )
    worker_receipt = await repository.record_result(submission=late_worker)
    assert worker_receipt.disposition == "ignored"

    receipt = await repository.timeout_execution(execution_id=execution_id)
    assert receipt is not None and receipt.disposition == "recorded"
    timeout_replay = await repository.timeout_execution(execution_id=execution_id)
    assert timeout_replay is not None and timeout_replay.replayed
    replay = await repository.record_result(submission=late_worker)
    assert replay.replayed and replay.disposition == "ignored"
    async with session_factory() as session:
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        task = await session.get(OrchestratorTaskModel, task_id)
        cancel_command = await session.scalar(
            select(OrchestratorWorkerCommandModel).where(
                OrchestratorWorkerCommandModel.execution_id == execution_id,
                OrchestratorWorkerCommandModel.command_type == "cancel",
            )
        )
        assert execution is not None and execution.status == "failed"
        assert execution.completed_at is not None
        timeout_completed_at = execution.completed_at
        assert execution.capacity_reserved
        assert task is not None and task.status == "failed"
        assert cancel_command is not None and cancel_command.status == "pending"
        assert cancel_command.command_json["reason"] == "execution_timed_out"

    current_time[0] = now + timedelta(seconds=10)
    stopped = await repository.confirm_stopped(
        WorkerStopConfirmation(
            instance_id=instance_id,
            stop_confirmation_id=str(uuid4()),
            cancel_command_id=cancel_command.command_id,
            execution_id=execution_id,
            generation=1,
            cancellation_epoch=0,
            reason="execution_timed_out",
            stopped_at=now,
        )
    )
    assert stopped.disposition == "capacity_released"
    async with session_factory() as session:
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        plan = await session.get(OrchestratorPlanModel, plan_id)
        assert execution is not None and not execution.capacity_reserved
        assert execution.status == "failed"
        assert execution.completed_at == timeout_completed_at
        assert execution.updated_at > execution.completed_at
        assert plan is not None and plan.terminal_task_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_and_plan_submission_share_atomic_queue_capacity(worker_store) -> None:
    """不同 Run 的并发重试与新 Plan 共用一个原子待派发容量上限。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    seeded = [
        await _seed_single_execution(session_factory, now=now, queued=False)
        for _ in range(2)
    ]
    execution_ids = [entry[3] for entry in seeded]
    instance_ids = [entry[4] for entry in seeded]
    assert all(instance_ids)
    execution_ids = [str(value) for value in execution_ids]
    instance_ids = [str(value) for value in instance_ids]
    repository = PostgresWorkerOrchestrationRepository(
        session_factory,
        clock=lambda: now,
        max_execution_attempts=3,
        execution_retry_initial_seconds=2,
        execution_retry_max_seconds=4,
        execution_retry_jitter_ratio=0,
        queue_max_pending_tasks=1,
    )

    async with session_factory() as session, session.begin():
        contract_id = str(uuid4())
        plan_run_id = str(uuid4())
        session.add(
            AgentRunModel(
                run_id=plan_run_id,
                session_id=f"session-{plan_run_id}",
                message_id=f"message-{plan_run_id}",
                sequence=1,
                status="running",
                request_json={},
                root_thread_id=f"root-{plan_run_id}",
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
                input_schema_json={"type": "object"},
                output_schema_json={"type": "object"},
                fingerprint_hash=f"contract-{contract_id}",
                created_at=now,
            )
        )
        for execution_id in execution_ids:
            execution = await session.get(OrchestratorExecutionModel, execution_id)
            assert execution is not None
            execution.deadline_at = now + timedelta(minutes=1)
        await session.flush()

    contract_id = str(contract_id)
    plan_submission = WorkerPlanSubmission(
        run_id=plan_run_id,
        plan_id=str(uuid4()),
        sequence=1,
        idempotency_key=str(uuid4()),
        goal="与执行重试竞争同一队列上限",
        tasks=(
            WorkerTaskSpec(
                task_ref="new-plan-task",
                contract_id=contract_id,
                contract_revision=1,
                objective="等待队列容量",
                input={},
                acceptance_criteria="只在有容量时入队",
            ),
        ),
        created_at=now,
    )
    result_submissions = [
        WorkerResultSubmission(
            execution_id=execution_id,
            instance_id=instance_id,
            generation=1,
            result_id=str(uuid4()),
            status="failed",
            error=WorkerExecutionError(
                code="WORKER_UPSTREAM_TIMEOUT",
                message=f"upstream timeout {index}",
                retryable=True,
                details={"source": "test"},
            ),
        )
        for index, (execution_id, instance_id) in enumerate(
            zip(execution_ids, instance_ids, strict=True)
        )
    ]

    async def submit_plan_outcome():
        try:
            return await repository.submit_plan(plan_submission)
        except WorkerPlanRejected as exc:
            return exc

    retry_receipts = await asyncio.gather(
        repository.record_result(submission=result_submissions[0]),
        repository.record_result(submission=result_submissions[1]),
    )
    retry_scheduled = [item for item in retry_receipts if item.disposition == "retry_scheduled"]
    queue_rejected = [item for item in retry_receipts if item.reason == "worker_error_final"]
    assert len(retry_scheduled) == 1
    assert len(queue_rejected) == 1
    plan_result = await submit_plan_outcome()
    assert isinstance(plan_result, WorkerPlanRejected)
    assert str(plan_result) == "worker_queue_capacity_exceeded"

    rejected_index = retry_receipts.index(queue_rejected[0])
    replay = await repository.record_result(
        submission=result_submissions[rejected_index]
    )
    assert replay.replayed and replay.reason == "worker_error_final"

    async with session_factory() as session:
        queued_execution_ids = list(
            (
                await session.scalars(
                    select(OrchestratorExecutionModel.execution_id)
                    .join(
                        OrchestratorTaskModel,
                        OrchestratorTaskModel.task_id
                        == OrchestratorExecutionModel.task_id,
                    )
                    .where(
                        OrchestratorExecutionModel.status == "queued",
                        OrchestratorTaskModel.status.in_(
                            ("pending", "retry_pending")
                        ),
                    )
                )
            ).all()
        )
        assert queued_execution_ids == [retry_scheduled[0].next_execution_id]
        queued_count = await session.scalar(
            select(func.count(func.distinct(OrchestratorTaskModel.task_id)))
            .join(
                OrchestratorExecutionModel,
                OrchestratorExecutionModel.task_id == OrchestratorTaskModel.task_id,
            )
            .where(
                OrchestratorExecutionModel.status == "queued",
                OrchestratorTaskModel.status.in_(("pending", "retry_pending")),
            )
        )
        assert queued_count == 1
        failed_task = await session.get(OrchestratorTaskModel, seeded[rejected_index][2])
        failed_execution = await session.get(
            OrchestratorExecutionModel, execution_ids[rejected_index]
        )
        failed_plan = await session.get(OrchestratorPlanModel, seeded[rejected_index][1])
        resume_jobs = list(
            (
                await session.scalars(
                    select(OrchestratorRootResumeJobModel).where(
                        OrchestratorRootResumeJobModel.plan_id
                        == seeded[rejected_index][1]
                    )
                )
            ).all()
        )
        assert failed_task is not None and failed_task.status == "failed"
        assert failed_task.error_json["code"] == "WORKER_UPSTREAM_TIMEOUT"
        assert failed_task.error_json["details"]["source"] == "test"
        assert failed_task.error_json["details"]["queue_capacity"]["code"] == "CAPABILITY_UNAVAILABLE"
        assert failed_execution is not None and failed_execution.status == "failed"
        assert failed_plan is not None and failed_plan.terminal_task_count == 1
        assert len(resume_jobs) == 1
