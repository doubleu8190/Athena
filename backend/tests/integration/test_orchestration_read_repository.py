"""PostgreSQL 验证 canonical Run 编排快照读取。"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from infrastructure.persistence.postgres.models import (
    AgentEventModel,
    AgentRunModel,
    ApprovalRecordModel,
    Base,
    MemoryProcessingJobModel,
    MemorySourceSuppressionModel,
    MessageModel,
    ProjectModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    WorkerContractModel,
    WorkerInstanceModel,
    WorkerApprovalRequestModel,
    SessionModel,
)
from infrastructure.persistence.postgres.repositories import PostgresOrchestrationRepository

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_snapshot_reads_worker_plans_executions_and_approval_batch() -> None:
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"orchestration_read_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    now = datetime.now(timezone.utc)
    run_id, plan_id, task_id, execution_id = [str(uuid4()) for _ in range(4)]
    plan2_id, task2_id, execution2_id, task3_id, execution3_id = [
        str(uuid4()) for _ in range(5)
    ]
    request_id, pause_id, approval_id = str(uuid4()), str(uuid4()), str(uuid4())
    approval_batch_item_id, approval_batch_item_record_id = "item-read-batch-2", str(uuid4())
    request2_id, pause2_id, approval2_id = str(uuid4()), str(uuid4()), str(uuid4())
    created = False
    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        tables = [
            AgentRunModel.__table__, AgentEventModel.__table__, MessageModel.__table__,
            ProjectModel.__table__, SessionModel.__table__,
            MemoryProcessingJobModel.__table__, MemorySourceSuppressionModel.__table__,
            ApprovalRecordModel.__table__, OrchestratorPlanModel.__table__,
            OrchestratorTaskModel.__table__, OrchestratorExecutionModel.__table__,
            WorkerContractModel.__table__, WorkerInstanceModel.__table__,
            WorkerApprovalRequestModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(sync_connection, tables=tables)
            )
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session, session.begin():
            session.add(ProjectModel(
                id="project-read-1", name="Read project", created_at=now, updated_at=now,
            ))
            session.add(SessionModel(
                id="session-read-1", project_id="project-read-1", title="Read session",
                created_at=now, updated_at=now, last_activity_at=now,
            ))
            session.add(AgentRunModel(
                run_id=run_id, message_id=f"message-{run_id}", sequence=1,
                session_id="session-read-1", status="running", request_json={},
                root_thread_id="root-thread-1", cancellation_epoch=2,
                cancellation_external_effect_status="none", created_at=now, updated_at=now,
            ))
            session.add(WorkerContractModel(
                contract_id="contract-read-1", revision=1, can_do_json=["write reports"],
                cannot_do_json=["external publication"], input_schema_json={"type": "object"},
                output_schema_json={"type": "object"}, fingerprint_hash="contract-hash",
                created_at=now,
            ))
            session.add(WorkerInstanceModel(
                instance_id="instance-read-1", service_principal="test-service",
                instance_key="instance-key-read-1", contract_id="contract-read-1", contract_revision=1,
                base_url="https://worker.invalid", execution_timeout_seconds=60,
                max_concurrency=1, status="ready", accepting_tasks=True, created_at=now, updated_at=now,
            ))
            session.add(OrchestratorPlanModel(
                plan_id=plan_id, run_id=run_id, sequence=1,
                idempotency_key="plan-read-1", request_hash="hash-plan", goal="Prepare a report",
                status="running", expected_task_count=1, terminal_task_count=1,
                root_wait_generation=0, created_at=now, completed_at=now,
            ))
            session.add(OrchestratorPlanModel(
                plan_id=plan2_id, run_id=run_id, sequence=2,
                idempotency_key="plan-read-2", request_hash="hash-plan-2", goal="Validate errors",
                status="failed", expected_task_count=2, terminal_task_count=2,
                root_wait_generation=0, created_at=now, completed_at=now,
            ))
            await session.flush()
            session.add(OrchestratorTaskModel(
                task_id=task_id, operation_id=str(uuid4()), plan_id=plan_id, task_ref="research",
                contract_id="contract-read-1", contract_revision=1,
                objective="Find the source material", input_json={"query": "source"},
                acceptance_criteria="At least one source", status="completed",
                output_json=["source-1"], error_json=None, execution_generation=1,
                cancellation_epoch=2, active_execution_id=execution_id, created_at=now, updated_at=now,
            ))
            session.add(OrchestratorTaskModel(
                task_id=task2_id, operation_id=str(uuid4()), plan_id=plan2_id, task_ref="validate",
                contract_id="contract-read-1", contract_revision=1,
                objective="Validate error", input_json={"query": "error"},
                acceptance_criteria="Error is visible", status="failed", output_json=None,
                error_json={"code": "WORKER_ERROR", "message": "failed"}, execution_generation=1,
                cancellation_epoch=0, active_execution_id=execution2_id, created_at=now, updated_at=now,
            ))
            session.add(OrchestratorTaskModel(
                task_id=task3_id, operation_id=str(uuid4()), plan_id=plan2_id, task_ref="scalar-output",
                contract_id="contract-read-1", contract_revision=1,
                objective="Return a scalar", input_json={"query": "scalar"},
                acceptance_criteria="A scalar is visible", status="completed",
                output_json="scalar-output", error_json=None, execution_generation=1,
                cancellation_epoch=0, active_execution_id=execution3_id, created_at=now, updated_at=now,
            ))
            await session.flush()
            session.add(OrchestratorExecutionModel(
                execution_id=execution_id, task_id=task_id, instance_id="instance-read-1", generation=1,
                status="completed", execution_timeout_seconds=60, cancellation_epoch=2,
                capacity_reserved=False, started_at=now, deadline_at=now + timedelta(seconds=60),
                result_json=["source-1"], error_json=None, completed_at=now,
                created_at=now, updated_at=now,
            ))
            session.add(OrchestratorExecutionModel(
                execution_id=execution2_id, task_id=task2_id, instance_id=None, generation=1,
                status="failed", execution_timeout_seconds=60, cancellation_epoch=0,
                capacity_reserved=False, started_at=now, deadline_at=now + timedelta(seconds=60),
                result_json=None, error_json={"code": "WORKER_ERROR", "message": "failed"},
                completed_at=now, created_at=now, updated_at=now,
            ))
            session.add(OrchestratorExecutionModel(
                execution_id=execution3_id, task_id=task3_id, instance_id=None, generation=1,
                status="completed", execution_timeout_seconds=60, cancellation_epoch=0,
                capacity_reserved=False, started_at=now, deadline_at=now + timedelta(seconds=60),
                result_json="scalar-output", error_json=None, completed_at=now,
                created_at=now, updated_at=now,
            ))
            request_json = {"items": [{
                "item_id": "item-read-1", "tool_name": "write_file", "summary": "Write report",
                "risk_level": "high", "arguments": {"content": "report"},
                "arguments_sha256": "a" * 64,
            }, {
                "item_id": approval_batch_item_id, "tool_name": "write_file", "summary": "Write metadata",
                "risk_level": "medium", "arguments": {"content": "metadata"},
                "arguments_sha256": "c" * 64,
            }]}
            session.add(WorkerApprovalRequestModel(
                receipt_id=str(uuid4()), instance_id="instance-read-1", approval_request_id=request_id,
                pause_id=pause_id, run_id=run_id, plan_id=plan_id, task_id=task_id,
                execution_id=execution_id, generation=1, cancellation_epoch=2,
                request_hash="hash-request", request_json=request_json, status="pending",
                expires_at=now + timedelta(seconds=120), received_at=now,
                receipt_json={"approval_batch_id": "batch-read-1"},
            ))
            session.add(ApprovalRecordModel(
                approval_id=approval_id, session_id="session-read-1", run_id=run_id,
                approval_batch_id="batch-read-1", approval_request_id=request_id, pause_id=pause_id,
                item_id="item-read-1", plan_id=plan_id, task_id=task_id, execution_id=execution_id,
                generation=1, cancellation_epoch=2, tool_call_id="item-read-1", tool_name="write_file",
                arguments_json={"content": "report"}, risk_level="high", arguments_sha256="a" * 64,
                status="pending", created_at=now, expires_at=now + timedelta(seconds=120),
            ))
            session.add(ApprovalRecordModel(
                approval_id=approval_batch_item_record_id, session_id="session-read-1", run_id=run_id,
                approval_batch_id="batch-read-1", approval_request_id=request_id, pause_id=pause_id,
                item_id=approval_batch_item_id, plan_id=plan_id, task_id=task_id, execution_id=execution_id,
                generation=1, cancellation_epoch=2, tool_call_id=approval_batch_item_id, tool_name="write_file",
                arguments_json={"content": "metadata"}, risk_level="medium", arguments_sha256="c" * 64,
                status="pending", created_at=now, expires_at=now + timedelta(seconds=120),
            ))
            session.add(WorkerApprovalRequestModel(
                receipt_id=str(uuid4()), instance_id="instance-read-1", approval_request_id=request2_id,
                pause_id=pause2_id, run_id=run_id, plan_id=plan2_id, task_id=task2_id,
                execution_id=execution2_id, generation=1, cancellation_epoch=0,
                request_hash="hash-request-2", request_json={"items": [{
                    "item_id": "item-read-2", "tool_name": "read_file", "summary": "Read evidence",
                    "risk_level": "medium", "arguments": {"path": "evidence.txt"},
                    "arguments_sha256": "b" * 64,
                }]}, status="resolved", expires_at=now + timedelta(seconds=120), received_at=now,
                receipt_json={"approval_batch_id": None},
            ))
            session.add(ApprovalRecordModel(
                approval_id=approval2_id, session_id="session-read-1", run_id=run_id,
                approval_batch_id=None, approval_request_id=request2_id, pause_id=pause2_id,
                item_id="item-read-2", plan_id=plan2_id, task_id=task2_id, execution_id=execution2_id,
                generation=1, cancellation_epoch=0, tool_call_id="item-read-2", tool_name="read_file",
                arguments_json={"path": "evidence.txt"}, risk_level="medium", arguments_sha256="b" * 64,
                status="resolved", decision="approved", created_at=now, decided_at=now,
                expires_at=now + timedelta(seconds=120),
            ))

        repository = PostgresOrchestrationRepository(session_factory)
        snapshot = await repository.get_run_orchestration_snapshot(run_id)
        assert snapshot is not None
        assert "plan" not in snapshot
        assert len(snapshot["worker_plans"]) == 2
        assert snapshot["worker_plans"][0]["tasks"][0]["output"] == ["source-1"]
        plan2_tasks = {
            task["task_ref"]: task for task in snapshot["worker_plans"][1]["tasks"]
        }
        assert plan2_tasks["validate"]["error"]["code"] == "WORKER_ERROR"
        assert plan2_tasks["scalar-output"]["output"] == "scalar-output"
        execution = snapshot["worker_plans"][0]["tasks"][0]["executions"][0]
        assert execution["completed_at"] == now
        assert execution["capacity_reserved"] is False
        assert len(snapshot["approvals"]) == 2
        approval_by_request = {
            item["approval_request_id"]: item for item in snapshot["approvals"]
        }
        approval = approval_by_request[request_id]
        approval2 = approval_by_request[request2_id]
        assert approval["approval_request_id"] == request_id
        assert approval["approval_batch_id"] == "batch-read-1"
        assert approval["items"][0]["arguments"] == {"content": "report"}
        assert approval["items"][0]["status"] == "pending"
        assert approval2["approval_request_id"] == request2_id
        assert approval2["approval_batch_id"] is None
        assert approval2["items"][0]["arguments"] == {"path": "evidence.txt"}
        assert approval2["items"][0]["decision"] == "approved"
        assert await repository.get_run_orchestration_snapshot("missing") is None
    finally:
        await test_engine.dispose()
        if created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
