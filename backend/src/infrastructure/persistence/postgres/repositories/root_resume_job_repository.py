"""PostgreSQL 租约中心驱动的 Root Resume 队列。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.leases import LeaseGrant, LeaseKey, LeaseLostError
from ..models import (
    AgentPlanModel,
    AgentRunModel,
    PlanRoundModel,
    RootResumeJobModel,
)
from .lease_center import PostgresLeaseCenter


@dataclass(frozen=True, slots=True)
class RootResumeLease:
    """绑定恢复作业身份、检查点快照与租约中心授权。"""

    job_id: str
    run_id: str
    session_id: str
    plan_id: str
    round_id: str
    plan_version: int
    root_wait_generation: int
    interrupt_id: str | None
    checkpoint_id: str | None
    worker_id: str
    lease: LeaseGrant


class PostgresRootResumeJobRepository:
    """在一个事务中领取、重试或结束 Root Resume 作业。"""

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

    async def claim_next(
        self, worker_id: str, *, lease_seconds: float
    ) -> RootResumeLease | None:
        """按业务锁顺序领取有效检查点，并在同一事务申请租约。"""
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker identity and positive lease are required")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                after_resource_id: str | None = None
                while True:
                    claimable = await self._leases.claimable_keys_in_transaction(
                        session,
                        "root_resume",
                        limit=256,
                        after_resource_id=after_resource_id,
                    )
                    if not claimable:
                        return None
                    claimable_ids = [key.resource_id for key in claimable]
                    candidates = (
                        await session.execute(
                            select(RootResumeJobModel.job_id)
                            .where(
                                RootResumeJobModel.job_id.in_(claimable_ids),
                                or_(
                                    and_(
                                        RootResumeJobModel.status == "pending",
                                        RootResumeJobModel.available_at <= now,
                                    ),
                                    RootResumeJobModel.status == "running",
                                ),
                            )
                            .order_by(
                                RootResumeJobModel.available_at,
                                RootResumeJobModel.created_at,
                            )
                            .limit(len(claimable_ids))
                        )
                    ).scalars().all()
                    for job_id in candidates:
                        hint = await session.get(RootResumeJobModel, job_id)
                        if hint is None:
                            continue
                        run = await session.get(
                            AgentRunModel, hint.run_id, with_for_update=True
                        )
                        plan = await session.get(
                            AgentPlanModel, hint.plan_id, with_for_update=True
                        )
                        round_row = await session.get(
                            PlanRoundModel, hint.round_id, with_for_update=True
                        )
                        job = (
                            await session.execute(
                                select(RootResumeJobModel)
                                .where(RootResumeJobModel.job_id == job_id)
                                .with_for_update(skip_locked=True)
                            )
                        ).scalar_one_or_none()
                        if job is None:
                            await self._leases.complete_unclaimed_in_transaction(
                                session, LeaseKey("root_resume", job_id)
                            )
                            continue
                        if any(row is None for row in (run, plan, round_row)):
                            job.status = "completed"
                            job.updated_at = now
                            await self._leases.complete_unclaimed_in_transaction(
                                session, LeaseKey("root_resume", job.job_id)
                            )
                            continue
                        assert run is not None and plan is not None and round_row is not None
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
                            job.updated_at = now
                            await self._leases.complete_unclaimed_in_transaction(
                                session, LeaseKey("root_resume", job.job_id)
                            )
                            continue
                        grant = await self._leases.acquire_in_transaction(
                            session,
                            LeaseKey("root_resume", job.job_id),
                            owner_id=uuid4().hex,
                            lease_seconds=lease_seconds,
                        )
                        if grant is None:
                            continue
                        job.status = "running"
                        job.attempt += 1
                        job.updated_at = now
                        return RootResumeLease(
                            job_id=job.job_id,
                            run_id=run.run_id,
                            session_id=run.session_id,
                            plan_id=plan.plan_id,
                            round_id=round_row.round_id,
                            plan_version=round_row.plan_version,
                            root_wait_generation=job.root_wait_generation,
                            interrupt_id=run.root_wait_interrupt_id,
                            checkpoint_id=run.root_wait_checkpoint_id,
                            worker_id=worker_id,
                            lease=grant,
                        )
                    after_resource_id = claimable[-1].resource_id

    async def renew(
        self, lease: RootResumeLease, *, lease_seconds: float
    ) -> RootResumeLease | None:
        """只续租中心中的恢复作业授权。"""
        if lease.lease.key != LeaseKey("root_resume", lease.job_id):
            raise ValueError("root_resume_lease_mismatch")
        renewed = await self._leases.renew(
            lease.lease, lease_seconds=lease_seconds
        )
        return None if renewed is None else replace(lease, lease=renewed)

    async def retry(
        self, lease: RootResumeLease, *, delay_seconds: float
    ) -> bool:
        """原子安排下一次尝试并释放当前恢复作业租约。"""
        if lease.lease.key != LeaseKey("root_resume", lease.job_id):
            raise ValueError("root_resume_lease_mismatch")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                job = await session.get(
                    RootResumeJobModel, lease.job_id, with_for_update=True
                )
                if job is None or job.status != "running":
                    return False
                await self._leases.assert_current_in_transaction(session, lease.lease)
                job.status = "pending"
                job.available_at = now + timedelta(
                    seconds=max(0, delay_seconds)
                )
                job.updated_at = now
                if not await self._leases.finish_in_transaction(session, lease.lease):
                    raise LeaseLostError(lease.lease.key)
                return True

    async def complete(self, lease: RootResumeLease) -> bool:
        """原子完成作业并释放对应租约。"""
        if lease.lease.key != LeaseKey("root_resume", lease.job_id):
            raise ValueError("root_resume_lease_mismatch")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                job = await session.get(
                    RootResumeJobModel, lease.job_id, with_for_update=True
                )
                if job is None or job.status != "running":
                    return False
                await self._leases.assert_current_in_transaction(session, lease.lease)
                job.status = "completed"
                job.updated_at = now
                if not await self._leases.finish_in_transaction(
                    session, lease.lease, completed=True
                ):
                    raise LeaseLostError(lease.lease.key)
                return True

    async def _now(self, session: AsyncSession) -> datetime:
        """读取数据库时钟并规范为 UTC。"""
        value = (
            self._clock()
            if self._clock is not None
            else await session.scalar(select(func.clock_timestamp()))
        )
        if value is None:
            raise RuntimeError("database_clock_unavailable")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


__all__ = ["PostgresRootResumeJobRepository", "RootResumeLease"]
