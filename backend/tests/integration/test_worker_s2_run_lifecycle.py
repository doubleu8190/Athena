"""S2 下普通 Run 终态与多 Plan 停止的 PostgreSQL 验收。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select

from domain.runs import RunCommand, RunStartPayload
from application.orchestration.worker_stop_confirmation import (
    WorkerStopConfirmationService,
)
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    MessageModel,
    MemoryProcessingJobModel,
    OrchestrationOutboxModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    ProjectModel,
    SessionModel,
    WorkerContractModel,
    WorkerInstanceModel,
)
from infrastructure.persistence.postgres.repositories.lease_center import (
    PostgresLeaseCenter,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)
from tests.integration.test_worker_orchestration_s2 import worker_store

_PROTOCOL_SCHEMA = json.loads(
    (Path(__file__).parents[2] / "docs" / "worker-protocol-v1.schema.json").read_text()
)


def _validate_s1_schema(value: dict[str, object], schema_name: str) -> None:
    """使用官方 S1 JSON Schema 与格式检查器验证协议文档。"""
    validator = Draft202012Validator(
        {
            "$schema": _PROTOCOL_SCHEMA["$schema"],
            "$defs": _PROTOCOL_SCHEMA["$defs"],
            "$ref": f"#/$defs/{schema_name}",
        },
        format_checker=FormatChecker(),
    )
    validator.validate(value)


async def _enqueue_and_claim(
    session_factory,
    repository: PostgresRunRepository,
    *,
    run_id: str,
) -> tuple[str, str, RunCommand]:
    """通过入队和领取公开 API 建立带真实执行租约的 Run。"""
    now = datetime.now(timezone.utc)
    session_id = f"session-{run_id}"
    project_id = f"project-{run_id}"
    async with session_factory() as session, session.begin():
        session.add(
            ProjectModel(
                id=project_id,
                name="S2 lifecycle",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SessionModel(
                id=session_id,
                project_id=project_id,
                title="S2 lifecycle",
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
        )
    message_id = f"message-{run_id}"
    await repository.enqueue_run(
        request_id=f"enqueue-{run_id}",
        parameters={"run_id": run_id, "message_id": message_id},
        session_id=session_id,
        project_id=project_id,
        run_id=run_id,
        payload=RunStartPayload(message="记住本轮请求", message_id=message_id),
    )
    invocation = await repository.claim_next_run()
    assert invocation is not None and invocation.run_id == run_id
    assert invocation.lease is not None
    return session_id, message_id, invocation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normal_run_completion_commits_answer_memory_job_and_outbox(worker_store) -> None:
    """普通 Run 成功时在同一终态事务提交回答、记忆作业和事件。"""
    session_factory, _ = worker_store
    repository = PostgresRunRepository(session_factory)
    run_id = str(uuid4())
    _, message_id, invocation = await _enqueue_and_claim(
        session_factory, repository, run_id=run_id
    )
    started_at = datetime.now(timezone.utc)
    answer = "本轮成功回答"
    result = {
        "content": answer,
        "answer_stream": {
            "message_id": f"assistant:{run_id}",
            "stream_id": f"stream:{run_id}",
            "started_at": started_at.isoformat(),
            "last_chunk_id": 1,
        },
    }

    assert await repository.complete(run_id, result, lease=invocation.lease)
    assert await repository.complete(run_id, result, lease=invocation.lease) is False

    async with session_factory() as session:
        run = await session.get(AgentRunModel, run_id)
        user_message = await session.get(MessageModel, message_id)
        assistant_message = await session.get(MessageModel, f"assistant:{run_id}")
        memory_job = await session.scalar(
            select(MemoryProcessingJobModel).where(
                MemoryProcessingJobModel.turn_id == run_id
            )
        )
        outbox = await session.scalar(
            select(OrchestrationOutboxModel).where(
                OrchestrationOutboxModel.dedupe_key == f"run:{run_id}:completed"
            )
        )
        session_row = await session.get(SessionModel, f"session-{run_id}")

    assert run is not None and run.status == "completed"
    assert run.response_json == result
    assert session_row is not None and session_row.active_run_id is None
    assert user_message is not None and user_message.content == "记住本轮请求"
    assert assistant_message is not None and assistant_message.content == answer
    assert memory_job is not None
    assert memory_job.user_text == "记住本轮请求"
    assert memory_job.assistant_text == answer
    assert outbox is not None and outbox.event_type == "RunCompleted"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normal_run_failure_finalizes_and_enqueues_empty_answer_memory_job(
    worker_store,
) -> None:
    """普通 Run 失败时释放会话执行权并原子排队失败记忆作业和事件。"""
    session_factory, _ = worker_store
    repository = PostgresRunRepository(session_factory)
    run_id = str(uuid4())
    session_id, _, invocation = await _enqueue_and_claim(
        session_factory, repository, run_id=run_id
    )
    error = {"code": "root_execution_failed", "message": "执行失败"}

    assert await repository.fail(run_id, error, lease=invocation.lease)

    async with session_factory() as session:
        run = await session.get(AgentRunModel, run_id)
        session_row = await session.get(SessionModel, session_id)
        memory_job = await session.scalar(
            select(MemoryProcessingJobModel).where(
                MemoryProcessingJobModel.turn_id == run_id
            )
        )
        outbox = await session.scalar(
            select(OrchestrationOutboxModel).where(
                OrchestrationOutboxModel.dedupe_key == f"run:{run_id}:failed"
            )
        )

    assert run is not None and run.status == "failed" and run.error_json == error
    assert session_row is not None and session_row.active_run_id is None
    assert memory_job is not None
    assert memory_job.user_text == "记住本轮请求"
    assert memory_job.assistant_text == ""
    assert outbox is not None and outbox.event_type == "RunFailed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stop_cancels_only_active_plan_work_and_waits_for_worker_confirmation(
    worker_store,
) -> None:
    """停止保留多条历史 Plan，只撤销活动任务并等 Worker 确认释放容量。"""
    session_factory, _ = worker_store
    run_repository = PostgresRunRepository(session_factory)
    run_id = str(uuid4())
    session_id, _, invocation = await _enqueue_and_claim(
        session_factory, run_repository, run_id=run_id
    )
    assert invocation.lease is not None
    now = datetime.now(timezone.utc)
    contract_id = str(uuid4())
    instance_id = str(uuid4())
    prior_plan_ids = [str(uuid4()), str(uuid4())]
    prior_task_ids = [str(uuid4()), str(uuid4())]
    active_plan_id = str(uuid4())
    active_task_id = str(uuid4())
    execution_id = str(uuid4())
    queued_execution_id = str(uuid4())
    queued_command_id = str(uuid4())

    async with session_factory() as session, session.begin():
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
        for sequence, plan_id, task_id in zip(
            (1, 2), prior_plan_ids, prior_task_ids, strict=True
        ):
            session.add(
                OrchestratorPlanModel(
                    plan_id=plan_id,
                    run_id=run_id,
                    sequence=sequence,
                    idempotency_key=f"historical-{sequence}",
                    request_hash=f"historical-hash-{sequence}",
                    goal=f"历史 Plan {sequence}",
                    status="completed",
                    expected_task_count=1,
                    terminal_task_count=1,
                    created_at=now - timedelta(minutes=sequence),
                    completed_at=now - timedelta(minutes=sequence),
                )
            )
            session.add(
                OrchestratorTaskModel(
                    task_id=task_id,
                    operation_id=str(uuid4()),
                    plan_id=plan_id,
                    task_ref=f"historical-task-{sequence}",
                    contract_id=contract_id,
                    contract_revision=1,
                    objective="已完成的历史任务",
                    input_json={},
                    acceptance_criteria="已完成",
                    status="completed",
                    execution_generation=1,
                    cancellation_epoch=0,
                    created_at=now - timedelta(minutes=sequence),
                    updated_at=now - timedelta(minutes=sequence),
                )
            )
        session.add(
            OrchestratorPlanModel(
                plan_id=active_plan_id,
                run_id=run_id,
                sequence=3,
                idempotency_key="active-plan",
                request_hash="active-plan-hash",
                goal="当前活动 Plan",
                status="running",
                expected_task_count=1,
                terminal_task_count=0,
                root_wait_generation=3,
                created_at=now,
            )
        )
        session.add(
            OrchestratorTaskModel(
                task_id=active_task_id,
                operation_id=str(uuid4()),
                plan_id=active_plan_id,
                task_ref="active-task",
                contract_id=contract_id,
                contract_revision=1,
                objective="等待 Worker 执行",
                input_json={},
                acceptance_criteria="收到确认",
                status="running",
                execution_generation=1,
                cancellation_epoch=0,
                active_execution_id=execution_id,
                created_at=now,
                updated_at=now,
            )
        )
        # Execution 外键依赖以上三个 Plan 的 Task，先显式落库再插入尝试。
        await session.flush()
        session.add(
            OrchestratorExecutionModel(
                execution_id=execution_id,
                task_id=active_task_id,
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
            OrchestratorExecutionModel(
                execution_id=queued_execution_id,
                task_id=active_task_id,
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

    stopped, replayed = await run_repository.stop_active(
        session_id,
        request_id=f"stop-{run_id}",
        expected_run_id=run_id,
        displayed_session_seq=2,
    )
    assert not replayed and stopped.status.value == "stopping"

    async with session_factory() as session:
        stored_plans = list(
            (
                await session.execute(
                    select(OrchestratorPlanModel)
                    .where(OrchestratorPlanModel.run_id == run_id)
                    .order_by(OrchestratorPlanModel.sequence)
                )
            ).scalars()
        )
        stored_tasks = list(
            (
                await session.execute(
                    select(OrchestratorTaskModel)
                    .where(OrchestratorTaskModel.task_id.in_((*prior_task_ids, active_task_id)))
                )
            ).scalars()
        )
        active_execution = await session.get(OrchestratorExecutionModel, execution_id)
        queued_execution = await session.get(
            OrchestratorExecutionModel, queued_execution_id
        )
        queued_command = await session.get(
            OrchestratorWorkerCommandModel, queued_command_id
        )
        cancel_command = await session.scalar(
            select(OrchestratorWorkerCommandModel).where(
                OrchestratorWorkerCommandModel.execution_id == execution_id,
                OrchestratorWorkerCommandModel.command_type == "cancel",
            )
        )

    assert [plan.status for plan in stored_plans] == ["completed", "completed", "cancelled"]
    task_status_by_id = {task.task_id: task.status for task in stored_tasks}
    assert task_status_by_id == {
        prior_task_ids[0]: "completed",
        prior_task_ids[1]: "completed",
        active_task_id: "cancelled",
    }
    assert active_execution is not None
    assert active_execution.status == "cancelling"
    assert active_execution.capacity_reserved is True
    assert queued_execution is not None and queued_execution.status == "cancelled"
    assert queued_command is not None and queued_command.status == "superseded"
    assert cancel_command is not None and cancel_command.status == "pending"
    assert cancel_command.command_json["command_id"] == cancel_command.command_id
    _validate_s1_schema(cancel_command.command_json, "CancelCommand")

    confirmation_id = str(uuid4())
    worker_repository = PostgresWorkerOrchestrationRepository(session_factory)
    raw_stop_body: dict[str, object] = {
        "protocol_version": "1.0",
        "stop_confirmation_id": confirmation_id,
        "cancel_command_id": cancel_command.command_id,
        "execution_id": execution_id,
        "generation": 1,
        "cancellation_epoch": 1,
        "reason": "run_cancelled",
        "stopped": True,
        "stopped_at": now.isoformat(),
    }
    _validate_s1_schema(raw_stop_body, "StopConfirmation")
    stop_receipt = await WorkerStopConfirmationService(
        worker_repository
    ).confirm(raw_stop_body, instance_id=instance_id)
    assert stop_receipt.disposition == "capacity_released"
    assert await PostgresLeaseCenter(session_factory).acknowledge_stopped(
        invocation.lease
    )
    assert await run_repository.finalize_stop(run_id)

    async with session_factory() as session:
        run = await session.get(AgentRunModel, run_id)
        session_row = await session.get(SessionModel, session_id)
        released_execution = await session.get(
            OrchestratorExecutionModel, execution_id
        )
    assert run is not None and run.status == "cancelled"
    assert session_row is not None and session_row.active_run_id is None
    assert released_execution is not None
    assert released_execution.capacity_reserved is False
