"""PostgreSQL acceptance for approval ownership across Runs."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.approval import ApprovalDecision
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    ApprovalRecordModel,
    Base,
    OrchestrationOutboxModel,
)
from infrastructure.persistence.postgres.repositories.approval_repository import (
    PostgresApprovalRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_run_level_approval_is_not_resolvable() -> None:
    """拒绝没有 Worker execution/interrupt 身份的旧式 Run 审批。

    参数：无。
    返回：无。
    异常：数据库故障或验收断言失败时测试会失败。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"cross_run_approval_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    now = datetime.now(timezone.utc)
    run_id = f"run-{uuid4().hex}"
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        AgentRunModel.__table__,
                        ApprovalRecordModel.__table__,
                        OrchestrationOutboxModel.__table__,
                    ],
                )
            )

        approval_id = f"approval-{uuid4().hex}"
        async with session_factory() as session, session.begin():
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    session_id=f"session-{run_id}",
                    status="running",
                    root_thread_id=f"run:{run_id}",
                    cancellation_epoch=0,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            session.add(
                ApprovalRecordModel(
                    approval_id=approval_id,
                    session_id=f"session-{run_id}",
                    run_id=run_id,
                    plan_id=None,
                    task_id=f"task-{run_id}",
                    execution_id=None,
                    worker_thread_id=None,
                    interrupt_id=None,
                    tool_call_id=f"call-{approval_id}",
                    tool_name="write_file",
                    arguments_json='{"path":"private.txt"}',
                    risk_level="high",
                    status="pending",
                    decision=None,
                    created_at=now.isoformat(),
                )
            )

        repository = PostgresApprovalRepository(session_factory, clock=lambda: now)
        assert not await repository.resolve(
            approval_id,
            decision=ApprovalDecision.APPROVED,
        )

        async with session_factory() as session:
            approval = await session.get(ApprovalRecordModel, approval_id)
            outbox_count = await session.scalar(
                select(func.count()).select_from(OrchestrationOutboxModel)
            )
        assert approval is not None
        assert approval.status == "pending" and approval.decision is None
        assert outbox_count == 0

    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
