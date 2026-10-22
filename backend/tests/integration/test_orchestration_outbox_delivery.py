"""PostgreSQL acceptance for Outbox delivery recovery after dispatcher failure."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.orchestration import OrchestrationOutboxLease
from infrastructure.persistence.postgres.models import (
    AgentEventModel,
    AgentRunModel,
    Base,
    OrchestrationOutboxModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresEventRepository,
    PostgresOrchestrationOutboxRepository,
)
from workers.orchestration_outbox_dispatcher import OrchestrationOutboxDispatcher


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


class _FailOnceAfterPublishOutbox:
    """在事件持久化后模拟进程于 Outbox 确认前退出。"""

    def __init__(self, repository: PostgresOrchestrationOutboxRepository) -> None:
        """保存真实仓储，并设置一次性确认故障。

        参数：repository 是连接隔离 PostgreSQL schema 的真实 Outbox 仓储。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._repository = repository
        self._fail_acknowledgement = True

    async def claim_next_event(
        self, worker_id: str, *, lease_seconds: float
    ) -> OrchestrationOutboxLease | None:
        """使用真实仓储领取一条 Outbox 事件。

        参数：worker_id 是本次 Dispatcher 的唯一身份；lease_seconds 是租约时长。
        返回：成功领取的事件租约；没有可领取事件时返回 None。
        异常：数据库访问失败时传播仓储异常。
        """
        return await self._repository.claim_next_event(
            worker_id, lease_seconds=lease_seconds
        )

    async def mark_event_published(self, lease: OrchestrationOutboxLease) -> bool:
        """首次确认时模拟进程故障，后续调用转交真实仓储。

        参数：lease 是当前 Dispatcher 持有的 Outbox 租约。
        返回：真实仓储是否接受确认。
        异常：首次调用抛出 RuntimeError，表示事件已写入但 Outbox 尚未确认。
        """
        if self._fail_acknowledgement:
            self._fail_acknowledgement = False
            raise RuntimeError("simulated crash before outbox acknowledgement")
        return await self._repository.mark_event_published(lease)

    async def retry_event(
        self, lease: OrchestrationOutboxLease, *, delay_seconds: float
    ) -> bool:
        """将失败重试操作转交真实仓储。

        参数：lease 是当前 Dispatcher 持有的事件租约；delay_seconds 是重试延迟。
        返回：真实仓储是否接受重试安排。
        异常：数据库访问失败时传播仓储异常。
        """
        return await self._repository.retry_event(
            lease, delay_seconds=delay_seconds
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_replay_after_publish_before_ack_persists_one_event() -> None:
    """事件提交后 Dispatcher 崩溃，租约恢复后只保留一条历史事件。

    PostgreSQL 测试库需允许创建 schema；本测试所有写入都限制在随机 schema，
    并在结束时删除该 schema。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"outbox_delivery_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    now = datetime.now(timezone.utc)
    current_time = [now]

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        required_tables = [
            AgentRunModel.__table__,
            AgentEventModel.__table__,
            OrchestrationOutboxModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=required_tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session, session.begin():
            session.add(
                AgentRunModel(
                    run_id="run-outbox-1",
                    session_id="session-outbox-1",
                    status="cancelled",
                    root_thread_id="run:run-outbox-1",
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            session.add(
                OrchestrationOutboxModel(
                    message_id="run-cancelled-message-1",
                    dedupe_key="run:run-outbox-1:cancelled:1",
                    event_type="RunCancelled",
                    correlation_id="run-outbox-1",
                    plan_id="plan-outbox-1",
                    plan_version=1,
                    payload_json={
                        "cancellation_epoch": 1,
                        "external_effect_status": "unknown",
                    },
                    status="pending",
                    attempt=0,
                    available_at=now,
                    occurred_at=now,
                )
            )

        outbox_repository = PostgresOrchestrationOutboxRepository(
            session_factory,
            clock=lambda: current_time[0],
        )
        outbox_with_crash = _FailOnceAfterPublishOutbox(outbox_repository)
        dispatcher_before_crash = OrchestrationOutboxDispatcher(
            outbox_with_crash,
            PostgresEventRepository(session_factory),
            worker_id="dispatcher-before-crash",
            lease_seconds=5,
        )

        with pytest.raises(
            RuntimeError, match="simulated crash before outbox acknowledgement"
        ):
            await dispatcher_before_crash.dispatch_once()

        async with session_factory() as session:
            persisted_events = (
                await session.execute(select(AgentEventModel))
            ).scalars().all()
            outbox_row = await session.get(
                OrchestrationOutboxModel, "run-cancelled-message-1"
            )

        assert len(persisted_events) == 1
        assert persisted_events[0].transition_id == (
            "orchestration-outbox:run-cancelled-message-1"
        )
        assert outbox_row is not None
        assert outbox_row.status == "processing"

        # 让旧租约自然过期，模拟重启后的新 Dispatcher 重新领取。
        current_time[0] = now + timedelta(seconds=6)
        dispatcher_after_restart = OrchestrationOutboxDispatcher(
            outbox_repository,
            PostgresEventRepository(session_factory),
            worker_id="dispatcher-after-restart",
            lease_seconds=5,
        )

        assert await dispatcher_after_restart.dispatch_once() is True

        async with session_factory() as session:
            persisted_events = (
                await session.execute(
                    select(AgentEventModel).order_by(AgentEventModel.session_seq)
                )
            ).scalars().all()
            outbox_row = await session.get(
                OrchestrationOutboxModel, "run-cancelled-message-1"
            )

        assert len(persisted_events) == 1
        assert persisted_events[0].session_seq == 1
        assert persisted_events[0].run_id == "run-outbox-1"
        assert persisted_events[0].transition_id == (
            "orchestration-outbox:run-cancelled-message-1"
        )
        assert json.loads(persisted_events[0].payload_json)["status"] == "cancelled"
        assert outbox_row is not None
        assert outbox_row.status == "published"
        assert outbox_row.attempt == 2
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
