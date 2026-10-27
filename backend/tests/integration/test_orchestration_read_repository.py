"""PostgreSQL 验证 Run 快照和 Plan Version 读取。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    ExecutionLeaseModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresOrchestrationRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_snapshot_and_plan_version_read_postgres_facts() -> None:
    """快照关联 Round 成员、attempt、审批和取消字段，并读取原版本 DAG。"""
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
    schema_created = False
    now = datetime.now(timezone.utc)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
        required_tables = [
            ExecutionLeaseModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=required_tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                session.add(AgentRunModel(
                    run_id="run-read-1",
                    message_id="message:" + str("run-read-1"),
                    sequence=1,
                    session_id="session-read-1",
                    status="running",
                    root_thread_id="root-thread-1",
                    cancellation_epoch=2,
                    cancellation_external_effect_status="unknown",
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                ))
                session.add(AgentPlanModel(
                    plan_id="plan-read-1",
                    run_id="run-read-1",
                    session_id="session-read-1",
                    status="waiting_review",
                    user_goal="Prepare a report",
                    max_parallelism=2,
                    current_version=1,
                    cancellation_epoch=2,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                ))
                await session.flush()
                session.add(PlanVersionModel(
                    plan_id="plan-read-1",
                    version=1,
                    dag_json={"nodes": [{"task_id": "task-read-1"}], "edges": []},
                    change_reason="initial plan",
                    based_on_round_id=None,
                    created_at=now,
                ))
                session.add(AgentTaskModel(
                    task_id="task-read-1",
                    plan_id="plan-read-1",
                    run_id="run-read-1",
                    title="Research",
                    objective="Find the source material",
                    input_json="{}",
                    expected_output_json="{}",
                    allowed_tools_json="[]",
                    worker_type="general",
                    timeout_seconds=60,
                    status="completed",
                    execution_generation=1,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                ))
                await session.flush()
                session.add(PlanRoundModel(
                    round_id="round-read-1",
                    plan_id="plan-read-1",
                    plan_version=1,
                    round_number=1,
                    status="completed",
                    expected_task_count=1,
                    terminal_task_count=1,
                    created_at=now,
                    completed_at=now,
                ))
                await session.flush()
                session.add(RoundTaskModel(
                    round_id="round-read-1",
                    task_id="task-read-1",
                    status="completed",
                    active_execution_id="execution-read-1",
                    terminal_at=now,
                ))
                await session.flush()
                session.add(TaskExecutionModel(
                    execution_id="execution-read-1",
                    round_id="round-read-1",
                    task_id="task-read-1",
                    attempt=1,
                    worker_thread_id="worker-thread-read-1",
                    status="completed",
                    cancellation_epoch=2,
                    result_ref="result-read-1",
                    error_json={"code": "old", "message": "safe", "stack": "private"},
                    created_at=now,
                    updated_at=now,
                ))
                session.add(ExecutionLeaseModel(
                    resource_type="worker_execution",
                    resource_id="execution-read-1",
                    owner_id=None,
                    fencing_token=3,
                    state="released",
                    lease_until=None,
                    created_at=now,
                    updated_at=now,
                ))
                session.add(ApprovalRecordModel(
                    approval_id="approval-read-1",
                    session_id="session-read-1",
                    run_id="run-read-1",
                    plan_id="plan-read-1",
                    task_id="task-read-1",
                    execution_id="execution-read-1",
                    worker_thread_id="worker-thread-read-1",
                    interrupt_id="interrupt-read-1",
                    tool_call_id="call-read-1",
                    tool_name="write_file",
                    arguments_json='{"content":"private"}',
                    risk_level="high",
                    status="approved",
                    decision="approve",
                    created_at=now.isoformat(),
                    decided_at=now.isoformat(),
                ))

        repository = PostgresOrchestrationRepository(session_factory)
        snapshot = await repository.get_run_orchestration_snapshot("run-read-1")
        assert snapshot is not None
        assert snapshot["run"]["cancellation_epoch"] == 2
        assert snapshot["run"]["cancellation_external_effect_status"] == "unknown"
        plan = snapshot["plan"]
        assert plan["versions"][0]["change_reason"] == "initial plan"
        member = plan["rounds"][0]["tasks"][0]
        assert member["task_id"] == "task-read-1"
        assert member["executions"][0]["fencing_token"] == 3
        assert member["executions"][0]["error"] == {
            "code": "old",
            "message": "safe",
        }
        approval = snapshot["approvals"][0]
        assert approval["decision"] == "approve"
        assert "arguments" not in approval

        version = await repository.get_plan_version("plan-read-1", 1)
        assert version is not None
        assert version["dag_json"]["nodes"][0]["task_id"] == "task-read-1"
        assert await repository.get_plan_version("plan-read-1", 2) is None
        assert await repository.get_run_orchestration_snapshot("missing") is None
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
