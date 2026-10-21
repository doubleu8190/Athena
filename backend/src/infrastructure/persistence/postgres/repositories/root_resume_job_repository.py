"""PostgreSQL leasing for durable Root Resume jobs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    AgentPlanModel,
    AgentRunModel,
    PlanRoundModel,
    RootResumeJobModel,
)


@dataclass(frozen=True, slots=True)
class RootResumeLease:
    """A fenced-by-owner lease for resuming one Root wait checkpoint."""

    job_id: str
    run_id: str
    session_id: str
    plan_id: str
    round_id: str
    root_wait_generation: int
    worker_id: str
    lease_until: datetime


class PostgresRootResumeJobRepository:
    """Claim, renew, retry and complete Root Resume jobs transactionally."""

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

    async def claim_next(
        self, worker_id: str, *, lease_seconds: float
    ) -> RootResumeLease | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker identity and positive lease are required")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                ready = and_(
                    RootResumeJobModel.status == "pending",
                    RootResumeJobModel.available_at <= now,
                )
                expired = and_(
                    RootResumeJobModel.status == "running",
                    RootResumeJobModel.lease_until <= now,
                )
                candidate = (
                    await session.execute(
                        select(RootResumeJobModel.job_id)
                        .where(
                            or_(ready, expired)
                        ).order_by(
                            RootResumeJobModel.available_at,
                            RootResumeJobModel.created_at,
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                if candidate is None:
                    return None

                job_hint = await session.get(RootResumeJobModel, candidate)
                if job_hint is None:
                    return None
                run = await session.get(
                    AgentRunModel, job_hint.run_id, with_for_update=True
                )
                plan = await session.get(
                    AgentPlanModel, job_hint.plan_id, with_for_update=True
                )
                round_row = await session.get(
                    PlanRoundModel, job_hint.round_id, with_for_update=True
                )
                job = (
                    await session.execute(
                        select(RootResumeJobModel)
                        .where(RootResumeJobModel.job_id == candidate)
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if any(row is None for row in (run, plan, round_row, job)):
                    return None
                assert run is not None and plan is not None
                assert round_row is not None and job is not None
                if (
                    run.status != "running"
                    or plan.status != "waiting_review"
                    or plan.active_round_id != job.round_id
                    or round_row.status != "completed"
                    or round_row.plan_version != plan.current_version
                    or run.root_wait_generation != job.root_wait_generation
                    or run.root_wait_round_id != job.round_id
                    or run.root_wait_plan_version != round_row.plan_version
                ):
                    job.status = "completed"
                    job.lease_owner = None
                    job.lease_until = None
                    job.updated_at = now
                    return None
                if job.status == "running" and job.lease_until and job.lease_until > now:
                    return None

                expires_at = now + timedelta(seconds=lease_seconds)
                job.status = "running"
                job.lease_owner = worker_id
                job.lease_until = expires_at
                job.attempt += 1
                job.updated_at = now
                return RootResumeLease(
                    job_id=job.job_id,
                    run_id=run.run_id,
                    session_id=run.session_id,
                    plan_id=plan.plan_id,
                    round_id=round_row.round_id,
                    root_wait_generation=job.root_wait_generation,
                    worker_id=worker_id,
                    lease_until=expires_at,
                )

    async def renew(
        self, lease: RootResumeLease, *, lease_seconds: float
    ) -> RootResumeLease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                expires_at = now + timedelta(seconds=lease_seconds)
                result = await session.execute(
                    update(RootResumeJobModel)
                    .where(
                        RootResumeJobModel.job_id == lease.job_id,
                        RootResumeJobModel.status == "running",
                        RootResumeJobModel.lease_owner == lease.worker_id,
                        RootResumeJobModel.root_wait_generation
                        == lease.root_wait_generation,
                        RootResumeJobModel.lease_until > now,
                    )
                    .values(lease_until=expires_at, updated_at=now)
                )
                return replace(lease, lease_until=expires_at) if result.rowcount else None

    async def retry(
        self, lease: RootResumeLease, *, delay_seconds: float
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                result = await session.execute(
                    update(RootResumeJobModel)
                    .where(
                        RootResumeJobModel.job_id == lease.job_id,
                        RootResumeJobModel.status == "running",
                        RootResumeJobModel.lease_owner == lease.worker_id,
                        RootResumeJobModel.root_wait_generation
                        == lease.root_wait_generation,
                    )
                    .values(
                        status="pending",
                        available_at=now + timedelta(seconds=max(0, delay_seconds)),
                        lease_owner=None,
                        lease_until=None,
                        updated_at=now,
                    )
                )
                return bool(result.rowcount)

    async def complete(self, lease: RootResumeLease) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                result = await session.execute(
                    update(RootResumeJobModel)
                    .where(
                        RootResumeJobModel.job_id == lease.job_id,
                        RootResumeJobModel.status == "running",
                        RootResumeJobModel.lease_owner == lease.worker_id,
                        RootResumeJobModel.root_wait_generation
                        == lease.root_wait_generation,
                    )
                    .values(
                        status="completed",
                        lease_owner=None,
                        lease_until=None,
                        updated_at=now,
                    )
                )
                return bool(result.rowcount)

    async def _now(self, session: AsyncSession) -> datetime:
        value = (
            self._clock()
            if self._clock is not None
            else await session.scalar(select(func.clock_timestamp()))
        )
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


__all__ = ["PostgresRootResumeJobRepository", "RootResumeLease"]
