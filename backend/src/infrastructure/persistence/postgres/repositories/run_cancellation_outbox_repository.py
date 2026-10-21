"""Run 取消通知 Outbox 的 PostgreSQL 租约与投递回执。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.orchestration import (
    RunCancellationOutboxLease,
    RunCancellationOutboxRepository,
)

from ..models import AgentPlanModel, OrchestrationOutboxModel


_CANCELLATION_EVENT_TYPES = ("PlanCancelling", "PlanCancelled", "RunCancelled")


class PostgresRunCancellationOutboxRepository(RunCancellationOutboxRepository):
    """通过短事务领取取消事件，并用 lease owner 防止旧 Dispatcher 确认。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def claim_next_cancellation_event(
        self, worker_id: str, *, lease_seconds: float
    ) -> RunCancellationOutboxLease | None:
        """独占领取一个待投递事件或租约已过期的事件。"""
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                eligible = or_(
                    and_(
                        OrchestrationOutboxModel.status == "pending",
                        OrchestrationOutboxModel.available_at <= now,
                    ),
                    and_(
                        OrchestrationOutboxModel.status == "processing",
                        or_(
                            OrchestrationOutboxModel.lease_until.is_(None),
                            OrchestrationOutboxModel.lease_until <= now,
                        ),
                    ),
                )
                row = (
                    await session.execute(
                        select(
                            OrchestrationOutboxModel,
                            AgentPlanModel.session_id,
                            AgentPlanModel.run_id,
                        )
                        .join(
                            AgentPlanModel,
                            AgentPlanModel.plan_id
                            == OrchestrationOutboxModel.plan_id,
                        )
                        .where(
                            OrchestrationOutboxModel.event_type.in_(
                                _CANCELLATION_EVENT_TYPES
                            ),
                            eligible,
                        )
                        .order_by(
                            OrchestrationOutboxModel.available_at,
                            OrchestrationOutboxModel.occurred_at,
                        )
                        .with_for_update(
                            skip_locked=True, of=OrchestrationOutboxModel
                        )
                        .limit(1)
                    )
                ).first()
                if row is None:
                    return None

                outbox, session_id, run_id = row
                lease_until = now + timedelta(seconds=lease_seconds)
                outbox.status = "processing"
                outbox.attempt = int(outbox.attempt or 0) + 1
                outbox.lease_owner = worker_id
                outbox.lease_until = lease_until
                return RunCancellationOutboxLease(
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
                    lease_until=lease_until,
                )

    async def mark_cancellation_event_published(
        self, lease: RunCancellationOutboxLease
    ) -> bool:
        """仅允许未过期的当前所有者确认投递。"""
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                result = await session.execute(
                    update(OrchestrationOutboxModel)
                    .where(
                        OrchestrationOutboxModel.message_id == lease.message_id,
                        OrchestrationOutboxModel.status == "processing",
                        OrchestrationOutboxModel.lease_owner == lease.worker_id,
                        OrchestrationOutboxModel.lease_until > now,
                    )
                    .values(
                        status="published",
                        published_at=now,
                        lease_owner=None,
                        lease_until=None,
                    )
                )
        return bool(result.rowcount)

    async def retry_cancellation_event(
        self, lease: RunCancellationOutboxLease, *, delay_seconds: float
    ) -> bool:
        """失败后释放租约并延迟重试，不丢弃取消事实。"""
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                result = await session.execute(
                    update(OrchestrationOutboxModel)
                    .where(
                        OrchestrationOutboxModel.message_id == lease.message_id,
                        OrchestrationOutboxModel.status == "processing",
                        OrchestrationOutboxModel.lease_owner == lease.worker_id,
                    )
                    .values(
                        status="pending",
                        available_at=now + timedelta(seconds=delay_seconds),
                        lease_owner=None,
                        lease_until=None,
                    )
                )
        return bool(result.rowcount)

    async def _now(self, session: AsyncSession) -> datetime:
        value: Any = (
            self._clock()
            if self._clock is not None
            else await session.scalar(select(func.clock_timestamp()))
        )
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


__all__ = ["PostgresRunCancellationOutboxRepository"]
