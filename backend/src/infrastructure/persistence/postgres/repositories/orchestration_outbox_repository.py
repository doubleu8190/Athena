"""Orchestration Outbox 的 PostgreSQL 租约和投递回执。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.leases import LeaseKey, LeaseLostError
from domain.orchestration import (
    OrchestrationOutboxLease,
    OrchestrationOutboxRepository,
)
from ..models import AgentRunModel, OrchestrationOutboxModel
from .lease_center import PostgresLeaseCenter


_USER_EVENT_TYPES = (
    "PlanCreated", "PlanAccepted", "PlanRevised", "PlanCompleted", "PlanFailed",
    "ApprovalRequested", "ApprovalResolved", "ApprovalCancelled", "TaskStarted",
    "TaskRetrying", "TaskCompleted", "TaskFailed", "TaskCancelled", "RoundStarted",
    "RoundCompleted", "PlanCancelling", "PlanCancelled", "RunStopping",
    "RunCancelled", "RunAnswerCompleted", "RunCompleted", "RunFailed",
)


class PostgresOrchestrationOutboxRepository(OrchestrationOutboxRepository):
    """领取持久事件，并在事务内确认发布或安排重试。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
        *,
        clock: Callable[[], datetime] | None = None,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        """保存数据库会话工厂、可选时钟和共享租约中心。"""
        self._session_factory = session_factory
        self._clock = clock
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def claim_next_event(
        self, worker_id: str, *, lease_seconds: float
    ) -> OrchestrationOutboxLease | None:
        """领取待投递事件；过期执行权由租约中心原子接管。"""
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker identity and positive lease are required")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                after_resource_id: str | None = None
                while True:
                    claimable = await self._leases.claimable_keys_in_transaction(
                        session,
                        "orchestration_outbox",
                        limit=128,
                        after_resource_id=after_resource_id,
                    )
                    if not claimable:
                        return None
                    claimable_ids = [key.resource_id for key in claimable]
                    rows = (
                        await session.execute(
                            select(
                                OrchestrationOutboxModel,
                                AgentRunModel.session_id,
                                AgentRunModel.run_id,
                            )
                            .join(
                                AgentRunModel,
                                AgentRunModel.run_id
                                == OrchestrationOutboxModel.correlation_id,
                            )
                            .where(
                                OrchestrationOutboxModel.message_id.in_(claimable_ids),
                                OrchestrationOutboxModel.event_type.in_(_USER_EVENT_TYPES),
                                OrchestrationOutboxModel.status == "pending",
                                OrchestrationOutboxModel.available_at <= now,
                            )
                            .order_by(
                                OrchestrationOutboxModel.available_at,
                                OrchestrationOutboxModel.occurred_at,
                            )
                            .with_for_update(
                                skip_locked=True, of=OrchestrationOutboxModel
                            )
                            .limit(len(claimable_ids))
                        )
                    ).all()
                    for outbox, session_id, run_id in rows:
                        grant = await self._leases.acquire_in_transaction(
                            session,
                            LeaseKey("orchestration_outbox", outbox.message_id),
                            owner_id=uuid4().hex,
                            lease_seconds=lease_seconds,
                        )
                        if grant is None:
                            continue
                        outbox.attempt = int(outbox.attempt or 0) + 1
                        return OrchestrationOutboxLease(
                            message_id=outbox.message_id,
                            worker_id=worker_id,
                            event_type=outbox.event_type,
                            run_id=run_id,
                            plan_id=outbox.plan_id,
                            session_id=session_id,
                            plan_version=outbox.plan_version,
                            round_id=outbox.round_id,
                            occurred_at=outbox.occurred_at,
                            payload=dict(outbox.payload_json or {}),
                            attempt=outbox.attempt,
                            lease=grant,
                        )
                    after_resource_id = claimable[-1].resource_id

    async def mark_event_published(
        self, lease: OrchestrationOutboxLease
    ) -> bool:
        """校验当前授权后，在同一事务确认发布并释放租约。"""
        if lease.lease.key != LeaseKey("orchestration_outbox", lease.message_id):
            raise ValueError("orchestration_outbox_lease_mismatch")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                outbox = await session.get(
                    OrchestrationOutboxModel,
                    lease.message_id,
                    with_for_update=True,
                )
                if outbox is None or outbox.status != "pending":
                    return False
                await self._leases.assert_current_in_transaction(session, lease.lease)
                outbox.status = "published"
                outbox.published_at = now
                if not await self._leases.finish_in_transaction(
                    session, lease.lease, completed=True
                ):
                    raise LeaseLostError(lease.lease.key)
                return True

    async def retry_event(
        self, lease: OrchestrationOutboxLease, *, delay_seconds: float
    ) -> bool:
        """保留事件并安排退避时间，同时释放当前投递执行权。"""
        if lease.lease.key != LeaseKey("orchestration_outbox", lease.message_id):
            raise ValueError("orchestration_outbox_lease_mismatch")
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                outbox = await session.get(
                    OrchestrationOutboxModel,
                    lease.message_id,
                    with_for_update=True,
                )
                if outbox is None or outbox.status != "pending":
                    return False
                await self._leases.assert_current_in_transaction(session, lease.lease)
                outbox.available_at = now + timedelta(seconds=delay_seconds)
                if not await self._leases.finish_in_transaction(
                    session, lease.lease
                ):
                    raise LeaseLostError(lease.lease.key)
                return True

    async def _now(self, session: AsyncSession) -> datetime:
        """读取并规范化数据库 UTC 时钟。"""
        value: Any = (
            self._clock()
            if self._clock is not None
            else await session.scalar(select(func.clock_timestamp()))
        )
        if value is None:
            raise RuntimeError("database_clock_unavailable")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


__all__ = ["PostgresOrchestrationOutboxRepository"]
