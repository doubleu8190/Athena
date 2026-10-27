"""PostgreSQL acceptance for operation replay across Worker attempts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.worker_tools import WorkerToolExecutor
from domain.leases import LeaseGrant, LeaseKey
from domain.tools import (
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolOperationClaimStatus,
)
from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    Base,
    ExecutionLeaseModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerToolOperationModel,
)
from infrastructure.persistence.postgres.repositories.worker_tool_operation_repository import (
    PostgresWorkerToolOperationRepository,
)
from infrastructure.persistence.postgres.repositories.lease_center import (
    PostgresLeaseCenter,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
class _UncertaintyToolService:
    """确保未知副作用被拒绝时不会触达外部工具。"""

    def __init__(self) -> None:
        """初始化调用记录。

        参数：无。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self.executed: list[ToolExecutionRequest] = []

    async def get(self, name: str) -> SimpleNamespace:
        """返回低风险、启用的本地工具配置。

        参数：name 是 Worker 请求查询的工具名称。
        返回：供 WorkerToolExecutor 校验的原生工具配置。
        异常：不主动抛出业务异常。
        """
        return SimpleNamespace(
            name=name,
            enabled=True,
            risk_level="low",
            require_approval=False,
            execution_mode="native",
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        """记录外部调用；本验收期望此方法始终不会被调用。

        参数：request 是经过 Worker 授权校验的工具请求。
        返回：固定成功结果，便于在意外调用时继续报告断言失败。
        异常：不主动抛出业务异常。
        """
        self.executed.append(request)
        return ToolExecutionResult(status="success", output="unexpected")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_replays_completed_effect_across_attempts() -> None:
    """A completed logical operation is replayed on a later attempt."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_operation_test_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_url = base_url.update_query_dict(
        {"options": f"-c search_path={schema}"}
    )
    test_engine = create_async_engine(test_url)
    created_schema = False
    now = datetime.now(timezone.utc)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created_schema = True

        core_tables = [

            ExecutionLeaseModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[*core_tables, WorkerToolOperationModel.__table__],
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session, session.begin():
            session.add(
                AgentRunModel(
                    run_id="run-1",
                    message_id="message:" + str("run-1"),
                    sequence=1,
                    session_id="session-1",
                    status="running",
                    root_thread_id="run:run-1",
                    cancellation_epoch=0,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            await session.flush()
            session.add(
                AgentPlanModel(
                    plan_id="plan-1",
                    run_id="run-1",
                    session_id="session-1",
                    status="running",
                    user_goal="write a report",
                    max_parallelism=1,
                    current_version=1,
                    active_round_id="round-1",
                    cancellation_epoch=0,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            await session.flush()
            session.add_all(
                [
                    AgentTaskModel(
                        task_id="task-1",
                        plan_id="plan-1",
                        run_id="run-1",
                        title="Write report",
                        objective="write a report",
                        allowed_tools_json='["write_file"]',
                        timeout_seconds=3600,
                        status="running",
                        execution_generation=2,
                        active_execution_id="execution-2",
                        created_at=now.isoformat(),
                        updated_at=now.isoformat(),
                    ),
                    PlanVersionModel(
                        plan_id="plan-1",
                        version=1,
                        dag_json={},
                        created_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                PlanRoundModel(
                    round_id="round-1",
                    plan_id="plan-1",
                    plan_version=1,
                    round_number=1,
                    status="running",
                    expected_task_count=1,
                    terminal_task_count=0,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                RoundTaskModel(
                    round_id="round-1",
                    task_id="task-1",
                    status="running",
                    active_execution_id="execution-2",
                )
            )
            await session.flush()
            session.add_all(
                [
                    TaskExecutionModel(
                        execution_id="execution-1",
                        round_id="round-1",
                        task_id="task-1",
                        attempt=1,
                        worker_thread_id="task:task-1:exec:1",
                        status="completed",
                        cancellation_epoch=0,
                        created_at=now - timedelta(minutes=1),
                        updated_at=now - timedelta(minutes=1),
                    ),
                    TaskExecutionModel(
                        execution_id="execution-2",
                        round_id="round-1",
                        task_id="task-1",
                        attempt=2,
                        worker_thread_id="task:task-1:exec:2",
                        status="running",
                        cancellation_epoch=0,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            session.add(
                ExecutionLeaseModel(
                    resource_type="worker_execution",
                    resource_id="execution-2",
                    owner_id="integration-worker",
                    fencing_token=2,
                    state="active",
                    lease_until=now + timedelta(minutes=1),
                    created_at=now,
                    updated_at=now,
                )
            )

        async with session_factory() as session, session.begin():
            session.add(
                WorkerToolOperationModel(
                    operation_key="athena:run-1:task-1:1:0:0",
                    logical_operation_key="athena:run-1:task-1:logical:0:0",
                    run_id="run-1",
                    plan_id="plan-1",
                    task_id="task-1",
                    execution_id="execution-1",
                    tool_name="write_file",
                    tool_call_id="worker:task-1:1:0:0",
                    args_hash="args-hash",
                    status="completed",
                    result_json={
                        "status": "success",
                        "output": "created",
                        "error": None,
                        "duration_ms": 12,
                    },
                    created_at=now - timedelta(minutes=1),
                    completed_at=now - timedelta(minutes=1),
                )
            )

        repository = PostgresWorkerToolOperationRepository(session_factory)
        retry_request = ToolExecutionRequest(
            tool_name="write_file",
            arguments={"path": "report.txt", "content": "ready"},
            session_id="session-1",
            run_id="run-1",
            tool_call_id="worker:task-1:2:0:0",
            agent_role="worker",
            plan_id="plan-1",
            task_id="task-1",
            operation_key="athena:run-1:task-1:2:0:0",
            logical_operation_key="athena:run-1:task-1:logical:0:0",
            execution_id="execution-2",
            lease=LeaseGrant(
                key=LeaseKey("worker_execution", "execution-2"),
                owner_id="integration-worker",
                fencing_token=2,
                lease_until=now + timedelta(minutes=1),
            ),
            cancellation_epoch=0,
        )

        claim = await repository.claim(retry_request, args_hash="args-hash")

        assert claim.status is ToolOperationClaimStatus.COMPLETED
        assert claim.result is not None
        assert claim.result.status == "success"
        assert claim.result.output == "created"

        concurrent_request = replace(
            retry_request,
            tool_call_id="worker:task-1:2:0:1",
            operation_key="athena:run-1:task-1:2:0:1",
            logical_operation_key="athena:run-1:task-1:logical:0:1",
        )
        call_arguments = {"path": "report.txt", "content": "ready"}
        args_hash = hashlib.sha256(
            json.dumps(
                call_arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        concurrent_claims = await asyncio.gather(
            repository.claim(concurrent_request, args_hash=args_hash),
            repository.claim(concurrent_request, args_hash=args_hash),
        )
        assert sorted(item.status.value for item in concurrent_claims) == [
            ToolOperationClaimStatus.ACQUIRED.value,
            ToolOperationClaimStatus.IN_PROGRESS.value,
        ]
        async with session_factory() as session:
            operation_count = await session.scalar(
                select(func.count())
                .select_from(WorkerToolOperationModel)
                .where(
                    WorkerToolOperationModel.logical_operation_key
                    == concurrent_request.logical_operation_key
                )
            )
        assert operation_count == 1

        # 模拟该调用已进入外部执行但 Worker 在写回结果前崩溃，再由新代际接管。
        async with session_factory() as session, session.begin():
            task = await session.get(AgentTaskModel, "task-1")
            round_task = await session.get(RoundTaskModel, ("round-1", "task-1"))
            previous_execution = await session.get(TaskExecutionModel, "execution-2")
            previous_lease = await session.get(
                ExecutionLeaseModel, ("worker_execution", "execution-2")
            )
            assert task is not None
            assert round_task is not None
            assert previous_execution is not None
            assert previous_lease is not None
            previous_execution.status = "failed"
            previous_lease.lease_until = now - timedelta(seconds=1)
            task.execution_generation = 3
            task.active_execution_id = "execution-3"
            round_task.active_execution_id = "execution-3"
            session.add(
                TaskExecutionModel(
                    execution_id="execution-3",
                    round_id="round-1",
                    task_id="task-1",
                    attempt=3,
                    worker_thread_id="task:task-1:exec:3",
                    status="running",
                    cancellation_epoch=0,
                    created_at=now + timedelta(seconds=1),
                    updated_at=now + timedelta(seconds=1),
                )
            )

        recovered_lease = await PostgresLeaseCenter(session_factory).acquire(
            LeaseKey("worker_execution", "execution-3"),
            owner_id="recovered-worker",
            lease_seconds=60,
        )
        assert recovered_lease is not None
        tools = _UncertaintyToolService()
        executor = WorkerToolExecutor(tools, operation_ledger=repository)
        recovered_result = await executor(
            tool_call={"name": "write_file", "arguments": call_arguments},
            execution={
                "plan_id": "plan-1",
                "run_id": "run-1",
                "session_id": "session-1",
                "task": {"task_id": "task-1", "allowed_tools": ["write_file"]},
                "execution_id": "execution-3",
                "lease": {
                    "key": {
                        "resource_type": recovered_lease.key.resource_type,
                        "resource_id": recovered_lease.key.resource_id,
                    },
                    "owner_id": recovered_lease.owner_id,
                    "fencing_token": recovered_lease.fencing_token,
                    "lease_until": recovered_lease.lease_until.isoformat(),
                },
                "cancellation_epoch": 0,
                "execution_generation": 3,
            },
            tool_round=0,
            call_index=1,
        )

        assert recovered_result["status"] == "failed"
        assert recovered_result["error"]["code"] == (
            "worker_tool_operation_outcome_unknown"
        )
        assert tools.executed == []
    finally:
        await test_engine.dispose()
        if created_schema:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
