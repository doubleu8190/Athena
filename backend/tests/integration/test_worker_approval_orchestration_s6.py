"""S6 远程审批批次与恢复命令的 PostgreSQL 事务验收。"""

from __future__ import annotations

import hashlib
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import rfc8785
from sqlalchemy import func, select, text

from application.orchestration.worker_approval_requests import WorkerApprovalRequestService
from application.orchestration.worker_protocol_models import (
    WorkerResumeCommandModel,
    WorkerResumeReceiptModel,
)
from domain.approval import ApprovalDecision, ApprovalStatus
from domain.orchestration.worker_entities import WorkerDispatchLease, WorkerResultSubmission
from infrastructure.persistence.postgres.models import (
    ApprovalRecordModel,
    AgentRunModel,
    OrchestratorExecutionModel,
    OrchestrationOutboxModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    WorkerApprovalRequestModel,
)
from infrastructure.persistence.postgres.repositories.approval_repository import (
    PostgresApprovalRepository,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)
from application.orchestration.worker_timeouts import WorkerTimeoutService
from tests.integration.test_worker_orchestration_s2 import worker_store
from tests.integration.test_worker_orchestration_s5 import _seed_single_execution


@pytest.mark.integration
@pytest.mark.asyncio
async def test_remote_batch_partial_decisions_expiry_create_one_resume_without_resetting_time(
    worker_store,
) -> None:
    """部分用户决定不恢复，最后一个到期决定只生成一条恢复且保留暂停剩余时长。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    assert instance_id is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        db_now = await session.scalar(select(func.clock_timestamp()))
        assert db_now is not None
        execution.deadline_at = db_now + timedelta(seconds=90)
        execution.started_at = now - timedelta(seconds=30)
    repo = PostgresWorkerOrchestrationRepository(session_factory)
    approval_repo = PostgresApprovalRepository(session_factory)
    items = [
        {
            "item_id": str(uuid4()),
            "tool_name": "reference.approve",
            "summary": f"Approval {index}",
            "risk_level": "medium",
            "arguments": {"index": index},
        }
        for index in range(3)
    ]
    body_items = [
        {
            **item,
            "arguments_sha256": hashlib.sha256(
                rfc8785.dumps(item["arguments"])
            ).hexdigest(),
        }
        for item in items
    ]
    receipt = await WorkerApprovalRequestService(repo).submit(
        {
            "protocol_version": "1.0",
            "approval_request_id": str(uuid4()),
            "pause_id": str(uuid4()),
            "run_id": run_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "generation": 1,
            "cancellation_epoch": 0,
            "items": body_items,
        },
        instance_id=instance_id,
    )
    remaining_after_pause: float
    async with session_factory() as session:
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        assert execution.status == "waiting_approval"
        assert execution.deadline_at is None
        remaining_after_pause = execution.remaining_seconds
        approvals = (
            await session.scalars(
                select(ApprovalRecordModel)
                .where(ApprovalRecordModel.execution_id == execution_id)
                .order_by(ApprovalRecordModel.created_at)
            )
        ).all()
        assert len(approvals) == 3
    assert remaining_after_pause > 0
    assert await approval_repo.resolve(
        approvals[0].approval_id, decision=ApprovalDecision.APPROVED
    )
    assert await approval_repo.resolve(
        approvals[1].approval_id, decision=ApprovalDecision.DENIED
    )
    async with session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(OrchestratorWorkerCommandModel)
            .where(OrchestratorWorkerCommandModel.command_type == "resume")
        ) == 0
        third_id = approvals[2].approval_id
        await session.rollback()
    async with session_factory() as session, session.begin():
        third = await session.get(ApprovalRecordModel, third_id)
        assert third is not None
        third.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await approval_repo.expire_due_approvals() == 1
    async with session_factory() as session:
        commands = (
            await session.scalars(
                select(OrchestratorWorkerCommandModel).where(
                    OrchestratorWorkerCommandModel.command_type == "resume"
                )
            )
        ).all()
        request = await session.scalar(
            select(WorkerApprovalRequestModel).where(
                WorkerApprovalRequestModel.execution_id == execution_id
            )
        )
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        decisions = commands[0].command_json["decisions"]
        decision_by_item = {item["item_id"]: item["decision"] for item in decisions}
        assert len(commands) == 1
        assert len(decisions) == 3
        assert decision_by_item[items[0]["item_id"]] == "approved"
        assert decision_by_item[items[1]["item_id"]] == "denied"
        assert decision_by_item[items[2]["item_id"]] == "expired"
        assert hashlib.sha256(rfc8785.dumps(decisions)).hexdigest() == commands[0].command_json[
            "decision_set_sha256"
        ]
        assert request is not None and request.resume_command_id == commands[0].command_id
        assert request.resume_wait_deadline_at is not None
        assert execution is not None and execution.remaining_seconds == pytest.approx(
            remaining_after_pause
        )
        assert execution.deadline_at is None
        assert receipt.status == "accepted"

    # The terminal result can arrive before the Resume HTTP receipt. It must
    # settle the resume command, and the later receipt must not undo that state.
    lease = WorkerDispatchLease(
        command_id=commands[0].command_id,
        execution_id=execution_id,
        lease_owner="resume-dispatcher",
        lease_token=str(uuid4()),
        attempt=0,
        command=commands[0].command_json,
        instance_id=instance_id,
        instance_key=f"worker-{instance_id}",
        service_principal="test-service",
        base_url="https://worker.invalid",
        command_type="resume",
    )
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        task = await session.get(OrchestratorTaskModel, task_id)
        command = await session.get(
            OrchestratorWorkerCommandModel, commands[0].command_id
        )
        assert execution is not None and task is not None and command is not None
        now = await session.scalar(select(func.clock_timestamp()))
        execution.status = "running"
        execution.started_at = now
        execution.deadline_at = now + timedelta(seconds=remaining_after_pause)
        execution.capacity_reserved = True
        task.status = "running"
        command.status = "dispatching"
        command.lease_owner = lease.lease_owner
        command.lease_token = lease.lease_token
        command.lease_until = now + timedelta(seconds=30)

    repo = PostgresWorkerOrchestrationRepository(session_factory)
    await repo.record_result(
        submission=WorkerResultSubmission(
            execution_id=execution_id,
            instance_id=instance_id,
            generation=1,
            result_id=str(uuid4()),
            status="completed",
            output={"echo": {"approved": True}},
        )
    )
    late_receipt = WorkerResumeReceiptModel(
        command_id=commands[0].command_id,
        receipt_id=str(uuid4()),
        disposition="accepted",
        received_at=datetime.now(timezone.utc),
    )
    assert not await repo.record_resume_receipt(lease, late_receipt)
    async with session_factory() as session:
        command = await session.get(
            OrchestratorWorkerCommandModel, commands[0].command_id
        )
        assert command is not None and command.status == "result_received"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_approval_replay_preserves_pause_and_late_request_is_rejected(
    worker_store,
) -> None:
    """单项回执映射稳定；重放不续期，已到期执行不能再建立审批暂停。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    assert instance_id is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        execution.deadline_at = now + timedelta(seconds=75)
    repo = PostgresWorkerOrchestrationRepository(session_factory)
    approval_repo = PostgresApprovalRepository(session_factory)
    item = {
        "item_id": str(uuid4()),
        "tool_name": "reference.approve",
        "summary": "Approve one item",
        "risk_level": "low",
        "arguments": {"value": "safe"},
    }
    item["arguments_sha256"] = hashlib.sha256(
        rfc8785.dumps(item["arguments"])
    ).hexdigest()
    request = {
        "protocol_version": "1.0",
        "approval_request_id": str(uuid4()),
        "pause_id": str(uuid4()),
        "run_id": run_id,
        "plan_id": plan_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "generation": 1,
        "cancellation_epoch": 0,
        "items": [item],
    }
    service = WorkerApprovalRequestService(repo)
    first = await service.submit(request, instance_id=instance_id)
    assert first.approval_batch_id is None
    assert len(first.items) == 1
    approval_id = first.items[0].approval_id
    async with session_factory() as session:
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        remaining = execution.remaining_seconds
        request_row = await session.scalar(
            select(WorkerApprovalRequestModel).where(
                WorkerApprovalRequestModel.execution_id == execution_id
            )
        )
        assert request_row is not None
        original_expiry = request_row.expires_at
    replay = await service.submit(request, instance_id=instance_id)
    assert replay.disposition == "duplicate"
    assert replay.items == first.items
    assert await approval_repo.resolve(
        approval_id, decision=ApprovalDecision.APPROVED
    )
    async with session_factory() as session:
        request_row = await session.scalar(
            select(WorkerApprovalRequestModel).where(
                WorkerApprovalRequestModel.execution_id == execution_id
            )
        )
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        approvals = (
            await session.scalars(
                select(ApprovalRecordModel).where(
                    ApprovalRecordModel.execution_id == execution_id
                )
            )
        ).all()
        command = await session.scalar(
            select(OrchestratorWorkerCommandModel).where(
                OrchestratorWorkerCommandModel.command_type == "resume",
                OrchestratorWorkerCommandModel.execution_id == execution_id,
            )
        )
        assert request_row is not None and request_row.expires_at == original_expiry
        assert execution is not None and execution.remaining_seconds == pytest.approx(
            remaining
        )
        assert len(approvals) == 1 and approvals[0].approval_id == approval_id
        assert command is not None
        assert command.command_json["approval_id"] == approval_id
        assert command.command_json["approval_batch_id"] is None
        assert command.command_json["decisions"][0]["item_id"] == item["item_id"]
        assert hashlib.sha256(
            rfc8785.dumps(command.command_json["decisions"])
        ).hexdigest() == command.command_json["decision_set_sha256"]

    # A second running execution whose first dispatch deadline has elapsed is
    # rejected before any approval or pause row is committed.
    late_run, late_plan, late_task, late_execution, late_instance = (
        await _seed_single_execution(session_factory, now=now, queued=False)
    )
    assert late_instance is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, late_execution)
        assert execution is not None
        execution.deadline_at = now - timedelta(seconds=1)
    late_request = {
        **request,
        "approval_request_id": str(uuid4()),
        "pause_id": str(uuid4()),
        "run_id": late_run,
        "plan_id": late_plan,
        "task_id": late_task,
        "execution_id": late_execution,
    }
    with pytest.raises(ValueError, match="worker_approval_execution_not_eligible"):
        await service.submit(late_request, instance_id=late_instance)
    async with session_factory() as session:
        late_execution_row = await session.get(
            OrchestratorExecutionModel, late_execution
        )
        late_request_row = await session.scalar(
            select(WorkerApprovalRequestModel).where(
                WorkerApprovalRequestModel.execution_id == late_execution
            )
        )
        assert late_execution_row is not None
        assert late_execution_row.status == "running"
        assert late_request_row is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_wins_against_approval_resolution_without_resume_command(
    worker_store,
) -> None:
    """取消与用户批准通过 Run 锁串行，取消胜出后不再创建 ResumeCommand。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    assert instance_id is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        execution.deadline_at = now + timedelta(seconds=90)
    repo = PostgresWorkerOrchestrationRepository(session_factory)
    item = {
        "item_id": str(uuid4()),
        "tool_name": "reference.approve",
        "summary": "Approval before cancellation",
        "risk_level": "medium",
        "arguments": {"id": "cancel-race"},
    }
    item["arguments_sha256"] = hashlib.sha256(
        rfc8785.dumps(item["arguments"])
    ).hexdigest()
    await WorkerApprovalRequestService(repo).submit(
        {
            "protocol_version": "1.0",
            "approval_request_id": str(uuid4()),
            "pause_id": str(uuid4()),
            "run_id": run_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "generation": 1,
            "cancellation_epoch": 0,
            "items": [item],
        },
        instance_id=instance_id,
    )
    async with session_factory() as session:
        approval = await session.scalar(
            select(ApprovalRecordModel).where(
                ApprovalRecordModel.execution_id == execution_id
            )
        )
        assert approval is not None
        approval_id = approval.approval_id
    await repo.cancel_task(run_id=run_id, task_id=task_id)
    assert not await PostgresApprovalRepository(session_factory).resolve(
        approval_id, decision=ApprovalDecision.APPROVED
    )
    async with session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(OrchestratorWorkerCommandModel)
            .where(
                OrchestratorWorkerCommandModel.execution_id == execution_id,
                OrchestratorWorkerCommandModel.command_type == "resume",
            )
        ) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resume_wait_timeout_finalizes_execution_and_releases_capacity(
    worker_store,
) -> None:
    """全部决定后的恢复等待独立计时，到期后失败并释放原实例容量。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    assert instance_id is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        execution.deadline_at = now + timedelta(seconds=60)
    repo = PostgresWorkerOrchestrationRepository(session_factory)
    item = {
        "item_id": str(uuid4()),
        "tool_name": "reference.approve",
        "summary": "Approval waiting timeout",
        "risk_level": "low",
        "arguments": {"id": "resume-wait"},
    }
    item["arguments_sha256"] = hashlib.sha256(
        rfc8785.dumps(item["arguments"])
    ).hexdigest()
    await WorkerApprovalRequestService(repo).submit(
        {
            "protocol_version": "1.0",
            "approval_request_id": str(uuid4()),
            "pause_id": str(uuid4()),
            "run_id": run_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "generation": 1,
            "cancellation_epoch": 0,
            "items": [item],
        },
        instance_id=instance_id,
    )
    async with session_factory() as session:
        approval = await session.scalar(
            select(ApprovalRecordModel).where(
                ApprovalRecordModel.execution_id == execution_id
            )
        )
        assert approval is not None
        approval_id = approval.approval_id
    assert await PostgresApprovalRepository(session_factory).resolve(
        approval_id, decision=ApprovalDecision.DENIED
    )
    async with session_factory() as session, session.begin():
        request = await session.scalar(
            select(WorkerApprovalRequestModel).where(
                WorkerApprovalRequestModel.execution_id == execution_id
            )
        )
        assert request is not None
        request.resume_wait_deadline_at = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
    timeout_service = WorkerTimeoutService(repo)
    assert await timeout_service.scan_once(batch_size=10) == 1
    async with session_factory() as session:
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        task = await session.get(OrchestratorTaskModel, task_id)
        assert execution is not None and task is not None
        assert execution.status == "failed"
        assert execution.capacity_reserved is False
        assert task.status == "failed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_second_pause_uses_new_resume_and_ignores_old_receipt(
    worker_store,
) -> None:
    """同一 Execution 再次暂停时，旧 Resume 回执不能影响新暂停。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    assert instance_id is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        db_now = await session.scalar(select(func.clock_timestamp()))
        assert db_now is not None
        execution.deadline_at = db_now + timedelta(seconds=90)
    repo = PostgresWorkerOrchestrationRepository(session_factory)
    approval_repo = PostgresApprovalRepository(session_factory)

    def make_request(label: str) -> dict[str, object]:
        arguments = {"label": label}
        return {
            "protocol_version": "1.0",
            "approval_request_id": str(uuid4()),
            "pause_id": str(uuid4()),
            "run_id": run_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "generation": 1,
            "cancellation_epoch": 0,
            "items": [
                {
                    "item_id": str(uuid4()),
                    "tool_name": "reference.approve",
                    "summary": label,
                    "risk_level": "low",
                    "arguments": arguments,
                    "arguments_sha256": hashlib.sha256(
                        rfc8785.dumps(arguments)
                    ).hexdigest(),
                }
            ],
        }

    first = await WorkerApprovalRequestService(repo).submit(
        make_request("first-pause"), instance_id=instance_id
    )
    first_approval_id = first.items[0].approval_id
    assert await approval_repo.resolve(
        first_approval_id, decision=ApprovalDecision.APPROVED
    )
    first_lease = await repo.claim_dispatch_command(
        owner_id="resume-test", lease_seconds=30, command_types=("resume",)
    )
    assert first_lease is not None
    first_start = await repo.prepare_resume_dispatch(first_lease, lease_seconds=30)
    assert first_start is not None

    def build_resume(start):
        assert start.deadline_at is not None
        draft = {
            key: value
            for key, value in start.command.items()
            if key != "assignment_required"
        }
        return WorkerResumeCommandModel.model_validate(
            {
                **draft,
                "execution_grant": {
                    "token": "test-grant-token-value-123456",
                    "expires_at": start.deadline_at,
                },
            }
        ).model_dump(mode="json", exclude_none=True)

    first_start = await repo.prepare_first_resume_send(
        first_lease,
        lease_seconds=30,
        command_builder=build_resume,
    )
    assert first_start is not None
    first_command_id = first_start.command_id
    first_receipt = WorkerResumeReceiptModel(
        command_id=first_command_id,
        receipt_id=str(uuid4()),
        disposition="accepted",
        received_at=datetime.now(timezone.utc),
    )
    assert await repo.record_resume_receipt(first_lease, first_receipt)

    second = await WorkerApprovalRequestService(repo).submit(
        make_request("second-pause"), instance_id=instance_id
    )
    second_approval_id = second.items[0].approval_id
    assert await approval_repo.resolve(
        second_approval_id, decision=ApprovalDecision.DENIED
    )
    async with session_factory() as session:
        commands = (
            await session.scalars(
                select(OrchestratorWorkerCommandModel)
                .where(
                    OrchestratorWorkerCommandModel.execution_id == execution_id,
                    OrchestratorWorkerCommandModel.command_type == "resume",
                )
                .order_by(OrchestratorWorkerCommandModel.created_at)
            )
        ).all()
        assert len(commands) == 2
        assert commands[0].command_id == first_command_id
        assert commands[0].status == "superseded"
        assert commands[1].command_id != first_command_id
        old_request = await session.scalar(
            select(WorkerApprovalRequestModel).where(
                WorkerApprovalRequestModel.resume_command_id == first_command_id
            )
        )
        assert old_request is not None
    assert not await repo.record_resume_receipt(first_lease, first_receipt)
    async with session_factory() as session:
        current = await session.scalar(
            select(WorkerApprovalRequestModel).where(
                WorkerApprovalRequestModel.execution_id == execution_id,
                WorkerApprovalRequestModel.resume_command_id != first_command_id,
            )
        )
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert current is not None
        assert execution is not None
        assert execution.status == "waiting_approval"
        assert current.pause_id != old_request.pause_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_member_resolves_concurrently_and_creates_one_resume(
    worker_store,
) -> None:
    """两个批次成员并发决定时由 Run 锁串行，最终只创建一个恢复命令。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    assert instance_id is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        db_now = await session.scalar(select(func.clock_timestamp()))
        assert db_now is not None
        execution.deadline_at = db_now + timedelta(seconds=90)
    repo = PostgresWorkerOrchestrationRepository(session_factory)
    approval_repo = PostgresApprovalRepository(session_factory)
    items = []
    for index in range(2):
        arguments = {"index": index}
        items.append(
            {
                "item_id": str(uuid4()),
                "tool_name": "reference.approve",
                "summary": f"Concurrent {index}",
                "risk_level": "medium",
                "arguments": arguments,
                "arguments_sha256": hashlib.sha256(
                    rfc8785.dumps(arguments)
                ).hexdigest(),
            }
        )
    await WorkerApprovalRequestService(repo).submit(
        {
            "protocol_version": "1.0",
            "approval_request_id": str(uuid4()),
            "pause_id": str(uuid4()),
            "run_id": run_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "generation": 1,
            "cancellation_epoch": 0,
            "items": items,
        },
        instance_id=instance_id,
    )
    async with session_factory() as session:
        approvals = (
            await session.scalars(
                select(ApprovalRecordModel)
                .where(ApprovalRecordModel.execution_id == execution_id)
                .order_by(ApprovalRecordModel.item_id)
            )
        ).all()
        assert len(approvals) == 2
        approval_ids = [row.approval_id for row in approvals]
    outcomes = await asyncio.gather(
        approval_repo.resolve(approval_ids[0], decision=ApprovalDecision.APPROVED),
        approval_repo.resolve(approval_ids[1], decision=ApprovalDecision.DENIED),
    )
    assert outcomes == [True, True]
    async with session_factory() as session:
        commands = (
            await session.scalars(
                select(OrchestratorWorkerCommandModel).where(
                    OrchestratorWorkerCommandModel.execution_id == execution_id,
                    OrchestratorWorkerCommandModel.command_type == "resume",
                )
            )
        ).all()
        assert len(commands) == 1
        assert len(commands[0].command_json["decisions"]) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_commits_parent_state_while_resolution_waits_on_run_lock(
    worker_store,
) -> None:
    """决议已预读身份但等待 Run 锁时，取消提交后决议不写任何事实。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    assert instance_id is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        execution.deadline_at = now + timedelta(seconds=90)
    repo = PostgresWorkerOrchestrationRepository(session_factory)
    approval_repo = PostgresApprovalRepository(session_factory)
    arguments = {"race": True}
    request_body = {
        "protocol_version": "1.0",
        "approval_request_id": str(uuid4()),
        "pause_id": str(uuid4()),
        "run_id": run_id,
        "plan_id": plan_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "generation": 1,
        "cancellation_epoch": 0,
        "items": [
            {
                "item_id": str(uuid4()),
                "tool_name": "reference.approve",
                "summary": "Cancel race",
                "risk_level": "high",
                "arguments": arguments,
                "arguments_sha256": hashlib.sha256(
                    rfc8785.dumps(arguments)
                ).hexdigest(),
            }
        ],
    }
    await WorkerApprovalRequestService(repo).submit(
        request_body, instance_id=instance_id
    )
    async with session_factory() as session:
        approval = await session.scalar(
            select(ApprovalRecordModel).where(
                ApprovalRecordModel.execution_id == execution_id
            )
        )
        assert approval is not None
        approval_id = approval.approval_id

    async with session_factory() as lock_session:
        await lock_session.begin()
        await lock_session.execute(
            select(AgentRunModel)
            .where(AgentRunModel.run_id == run_id)
            .with_for_update()
        )
        resolution = asyncio.create_task(
            approval_repo.resolve(approval_id, decision=ApprovalDecision.APPROVED)
        )
        await asyncio.sleep(0.05)
        assert not resolution.done()
        await lock_session.execute(
            text("UPDATE agent_runs SET status='stopped' WHERE run_id=:run_id"),
            {"run_id": run_id},
        )
        await lock_session.commit()
        assert await resolution is False
    async with session_factory() as session:
        approval = await session.get(ApprovalRecordModel, approval_id)
        outbox_count = await session.scalar(
            select(func.count()).select_from(OrchestrationOutboxModel).where(
                OrchestrationOutboxModel.event_type == "ApprovalResolved",
                OrchestrationOutboxModel.payload_json["approval_id"].as_string()
                == approval_id,
            )
        )
        assert approval is not None and approval.status == ApprovalStatus.PENDING.value
        assert outbox_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resume_ack_loss_retries_frozen_body_and_deadline(worker_store) -> None:
    """首次恢复回执丢失后重投沿用冻结内容、授权和 deadline。"""
    session_factory, _ = worker_store
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=datetime.now(timezone.utc), queued=False
    )
    assert instance_id is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        db_now = await session.scalar(select(func.clock_timestamp()))
        assert db_now is not None
        execution.deadline_at = db_now + timedelta(seconds=90)

    repo = PostgresWorkerOrchestrationRepository(session_factory)
    approval_repo = PostgresApprovalRepository(session_factory)
    arguments = {"ack_loss": True}
    request = await WorkerApprovalRequestService(repo).submit(
        {
            "protocol_version": "1.0",
            "approval_request_id": str(uuid4()),
            "pause_id": str(uuid4()),
            "run_id": run_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "generation": 1,
            "cancellation_epoch": 0,
            "items": [
                {
                    "item_id": str(uuid4()),
                    "tool_name": "reference.approve",
                    "summary": "Lost resume acknowledgement",
                    "risk_level": "low",
                    "arguments": arguments,
                    "arguments_sha256": hashlib.sha256(
                        rfc8785.dumps(arguments)
                    ).hexdigest(),
                }
            ],
        },
        instance_id=instance_id,
    )
    assert await approval_repo.resolve(
        request.items[0].approval_id, decision=ApprovalDecision.APPROVED
    )

    first_lease = await repo.claim_dispatch_command(
        owner_id="resume-ack-loss", lease_seconds=30, command_types=("resume",)
    )
    assert first_lease is not None
    first = await repo.prepare_resume_dispatch(first_lease, lease_seconds=30)
    assert first is not None and "assignment_required" in first.command

    def build_resume(start):
        assert start.deadline_at is not None
        draft = {
            key: value
            for key, value in start.command.items()
            if key != "assignment_required"
        }
        return WorkerResumeCommandModel.model_validate(
            {
                **draft,
                "execution_grant": {
                    "token": "stable-resume-grant-token-123456",
                    "expires_at": start.deadline_at,
                },
            }
        ).model_dump(mode="json", exclude_none=True)

    first = await repo.prepare_first_resume_send(
        first_lease, lease_seconds=30, command_builder=build_resume
    )
    assert first is not None
    frozen_body = first.command
    frozen_hash = first.request_hash
    frozen_deadline = first.deadline_at
    assert frozen_deadline is not None
    async with session_factory() as session:
        stored_request = await session.scalar(
            select(WorkerApprovalRequestModel).where(
                WorkerApprovalRequestModel.approval_request_id
                == str(request.approval_request_id)
            )
        )
        assert stored_request is not None
        assert stored_request.resume_wait_deadline_at is None

    # 回执丢失相当于只安排相同命令重投，不代表 Worker 未执行恢复。
    assert await repo.retry_dispatch_command(first_lease, delay_seconds=0)
    retry_lease = await repo.claim_dispatch_command(
        owner_id="resume-ack-loss", lease_seconds=30, command_types=("resume",)
    )
    assert retry_lease is not None
    retry = await repo.prepare_resume_dispatch(retry_lease, lease_seconds=30)
    assert retry is not None
    assert retry.command == frozen_body
    assert retry.request_hash == frozen_hash
    assert retry.deadline_at == frozen_deadline
    async with session_factory() as session:
        command = await session.get(OrchestratorWorkerCommandModel, first.command_id)
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert command is not None and execution is not None
        assert command.send_attempt_count == 2
        assert execution.deadline_at == frozen_deadline
        assert execution.capacity_reserved is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_opposite_decisions_and_old_pause_replay_are_idempotent(
    worker_store,
) -> None:
    """跨标签页的相反决定只能有一个赢家，赢家重放安全且副作用唯一。"""
    session_factory, _ = worker_store
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id, instance_id = await _seed_single_execution(
        session_factory, now=now, queued=False
    )
    assert instance_id is not None
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        db_now = await session.scalar(select(func.clock_timestamp()))
        assert db_now is not None
        execution.deadline_at = db_now + timedelta(seconds=90)

    repository = PostgresWorkerOrchestrationRepository(session_factory)
    await WorkerApprovalRequestService(repository).submit(
        {
            "protocol_version": "1.0",
            "approval_request_id": str(uuid4()),
            "pause_id": str(uuid4()),
            "run_id": run_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "generation": 1,
            "cancellation_epoch": 0,
            "items": [
                {
                    "item_id": str(uuid4()),
                    "tool_name": "reference.approve",
                    "summary": "Concurrent approval decision",
                    "risk_level": "high",
                    "arguments": {},
                    "arguments_sha256": hashlib.sha256(rfc8785.dumps({})).hexdigest(),
                }
            ],
        },
        instance_id=instance_id,
    )
    async with session_factory() as session:
        approval = await session.scalar(
            select(ApprovalRecordModel).where(
                ApprovalRecordModel.execution_id == execution_id
            )
        )
        assert approval is not None
        approval_id = approval.approval_id

    approvals = PostgresApprovalRepository(session_factory)
    outcomes = await asyncio.gather(
        approvals.resolve(approval_id, decision=ApprovalDecision.APPROVED),
        approvals.resolve(approval_id, decision=ApprovalDecision.DENIED),
    )
    assert sorted(outcomes) == [False, True]
    winning_decision = (
        ApprovalDecision.APPROVED if outcomes[0] else ApprovalDecision.DENIED
    )
    # 模拟同一 execution 已进入后续 pause；旧审批的不可变身份仍可重放。
    async with session_factory() as session, session.begin():
        execution = await session.get(OrchestratorExecutionModel, execution_id)
        assert execution is not None
        execution.current_approval_request_id = str(uuid4())
        execution.current_pause_id = str(uuid4())
    assert await approvals.resolve(approval_id, decision=winning_decision)
    losing_decision = (
        ApprovalDecision.DENIED
        if winning_decision is ApprovalDecision.APPROVED
        else ApprovalDecision.APPROVED
    )
    assert not await approvals.resolve(approval_id, decision=losing_decision)

    async with session_factory() as session:
        approval = await session.get(ApprovalRecordModel, approval_id)
        outbox_count = await session.scalar(
            select(func.count()).select_from(OrchestrationOutboxModel).where(
                OrchestrationOutboxModel.event_type == "ApprovalResolved",
                OrchestrationOutboxModel.payload_json["approval_id"].as_string()
                == approval_id,
            )
        )
        resume_count = await session.scalar(
            select(func.count()).select_from(OrchestratorWorkerCommandModel).where(
                OrchestratorWorkerCommandModel.execution_id == execution_id,
                OrchestratorWorkerCommandModel.command_type == "resume",
            )
        )
    assert approval is not None and approval.decision == winning_decision.value
    assert outbox_count == 1
    assert resume_count == 1
