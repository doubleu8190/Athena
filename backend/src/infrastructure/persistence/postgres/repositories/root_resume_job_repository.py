"""Root 逐轮恢复作业的 PostgreSQL 持久化适配器。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.leases import LeaseGrant, LeaseKey, LeaseLostError
from domain.orchestration import RootPlanResumeLease
from ..models import AgentRunModel, OrchestratorPlanModel, OrchestratorRootResumeJobModel, SessionModel
from .lease_center import PostgresLeaseCenter


class PostgresRootResumeJobRepository:
    """在数据库事务中领取、重试和完成 Root 逐轮恢复作业。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        clock: Callable[[], datetime] | None = None,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        """保存数据库会话、时钟和共享租约中心。"""
        self._session_factory = session_factory
        self._clock = clock
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def claim_next(self, worker_id: str, *, lease_seconds: float) -> RootPlanResumeLease | None:
        """领取当前 Run 等待代次仍有效的已完成 Plan。"""
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker identity and positive lease are required")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                after: str | None = None
                while True:
                    keys = await self._leases.claimable_keys_in_transaction(session, "root_resume", limit=256, after_resource_id=after)
                    if not keys:
                        return None
                    ids = [key.resource_id for key in keys]
                    job_ids = (await session.scalars(select(OrchestratorRootResumeJobModel.job_id).where(
                        OrchestratorRootResumeJobModel.job_id.in_(ids),
                        or_(and_(OrchestratorRootResumeJobModel.status == "pending", OrchestratorRootResumeJobModel.available_at <= now), OrchestratorRootResumeJobModel.status == "running"),
                    ).order_by(OrchestratorRootResumeJobModel.available_at, OrchestratorRootResumeJobModel.created_at).limit(len(ids)))).all()
                    for job_id in job_ids:
                        hint = await session.get(OrchestratorRootResumeJobModel, job_id)
                        if hint is None:
                            continue
                        session_id = await session.scalar(select(AgentRunModel.session_id).where(AgentRunModel.run_id == hint.run_id))
                        session_row = None if session_id is None else await session.scalar(
                            select(SessionModel).where(SessionModel.id == session_id).with_for_update()
                        )
                        run = await session.scalar(select(AgentRunModel).where(AgentRunModel.run_id == hint.run_id).execution_options(populate_existing=True).with_for_update())
                        plan = await session.scalar(select(OrchestratorPlanModel).where(OrchestratorPlanModel.plan_id == hint.plan_id).execution_options(populate_existing=True).with_for_update())
                        job = await session.scalar(select(OrchestratorRootResumeJobModel).where(OrchestratorRootResumeJobModel.job_id == job_id).execution_options(populate_existing=True).with_for_update(skip_locked=True))
                        if job is None:
                            continue
                        if session_row is None or run is None or plan is None or session_row.active_run_id != run.run_id or run.status != "running":
                            job.status = "cancelled"
                            job.updated_at = now
                            await self._leases.complete_unclaimed_in_transaction(session, LeaseKey("root_resume", job.job_id))
                            continue
                        if run.root_wait_generation > job.root_wait_generation:
                            job.status = "completed"
                            job.updated_at = now
                            await self._leases.complete_unclaimed_in_transaction(session, LeaseKey("root_resume", job.job_id))
                            continue
                        # Plan 已终态而结果先于 checkpoint arm 到达时，保持作业可重试，等待 Run 身份更新。
                        if run.root_wait_generation < job.root_wait_generation:
                            job.status = "pending"
                            job.available_at = now + timedelta(seconds=0.25)
                            job.updated_at = now
                            continue
                        if (
                            plan.run_id != run.run_id
                            or plan.root_wait_generation != job.root_wait_generation
                            or run.orchestrator_plan_sequence != plan.sequence
                        ):
                            job.status = "completed"
                            job.updated_at = now
                            await self._leases.complete_unclaimed_in_transaction(session, LeaseKey("root_resume", job.job_id))
                            continue
                        if plan.status != "completed":
                            # arm 可能早于最后一个 Worker 结果；job 必须保留，不能在此收敛。
                            job.status = "pending"
                            job.available_at = now + timedelta(seconds=0.25)
                            job.updated_at = now
                            continue
                        grant = await self._leases.acquire_in_transaction(session, LeaseKey("root_resume", job.job_id), owner_id=uuid4().hex, lease_seconds=lease_seconds)
                        if grant is None:
                            continue
                        job.status = "running"
                        job.attempt += 1
                        job.updated_at = now
                        return RootPlanResumeLease(job_id=job.job_id, run_id=run.run_id, session_id=run.session_id, plan_id=plan.plan_id, sequence=plan.sequence, root_wait_generation=job.root_wait_generation, interrupt_id=run.root_wait_interrupt_id, checkpoint_id=run.root_wait_checkpoint_id, lease=grant)
                    after = keys[-1].resource_id

    async def renew(self, lease: RootPlanResumeLease, *, lease_seconds: float) -> RootPlanResumeLease | None:
        """续期恢复作业租约。"""
        self._validate(lease)
        renewed = await self._leases.renew(lease.lease, lease_seconds=lease_seconds)
        return None if renewed is None else replace(lease, lease=renewed)

    async def retry(self, lease: RootPlanResumeLease, *, delay_seconds: float) -> bool:
        """以 fencing 租约原子安排下一次恢复。"""
        self._validate(lease)
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.scalar(select(AgentRunModel).where(AgentRunModel.run_id == lease.run_id).with_for_update())
                plan = await session.scalar(select(OrchestratorPlanModel).where(OrchestratorPlanModel.plan_id == lease.plan_id).with_for_update())
                job = await session.scalar(select(OrchestratorRootResumeJobModel).where(OrchestratorRootResumeJobModel.job_id == lease.job_id).with_for_update())
                if job is None or job.status != "running":
                    return False
                await self._leases.assert_current_in_transaction(session, lease.lease)
                now = await self._now(session)
                job.status = "pending"
                job.available_at = now + timedelta(seconds=max(0, delay_seconds))
                job.updated_at = now
                if not await self._leases.finish_in_transaction(session, lease.lease):
                    raise LeaseLostError(lease.lease.key)
                return True

    async def complete(self, lease: RootPlanResumeLease) -> bool:
        """以 fencing 租约原子完成恢复作业。"""
        self._validate(lease)
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.scalar(select(AgentRunModel).where(AgentRunModel.run_id == lease.run_id).with_for_update())
                plan = await session.scalar(select(OrchestratorPlanModel).where(OrchestratorPlanModel.plan_id == lease.plan_id).with_for_update())
                job = await session.scalar(select(OrchestratorRootResumeJobModel).where(OrchestratorRootResumeJobModel.job_id == lease.job_id).with_for_update())
                if job is None or job.status != "running":
                    return False
                await self._leases.assert_current_in_transaction(session, lease.lease)
                job.status = "completed"
                job.updated_at = await self._now(session)
                if not await self._leases.finish_in_transaction(session, lease.lease, completed=True):
                    raise LeaseLostError(lease.lease.key)
                return True

    async def is_wait_current(self, lease: RootPlanResumeLease) -> bool:
        """确认作业等待身份仍对应运行中的当前 Plan。"""
        async with self._session_factory() as session:
            row = await session.execute(
                select(AgentRunModel.status, AgentRunModel.root_wait_generation, AgentRunModel.orchestrator_plan_sequence, OrchestratorPlanModel.status, OrchestratorPlanModel.sequence, SessionModel.active_run_id)
                .select_from(AgentRunModel)
                .join(
                    OrchestratorPlanModel,
                    and_(
                        OrchestratorPlanModel.plan_id == lease.plan_id,
                        OrchestratorPlanModel.run_id == AgentRunModel.run_id,
                    ),
                )
                .join(SessionModel, SessionModel.id == AgentRunModel.session_id)
                .where(AgentRunModel.run_id == lease.run_id, OrchestratorPlanModel.run_id == lease.run_id)
            )
            current = row.one_or_none()
            return bool(current is not None and current[0] == "running" and current[1] == lease.root_wait_generation and current[2] == lease.sequence and current[3] == "completed" and current[4] == lease.sequence and current[5] == lease.run_id)

    @staticmethod
    def _validate(lease: RootPlanResumeLease) -> None:
        """校验恢复租约的资源身份。"""
        if lease.lease.key != LeaseKey("root_resume", lease.job_id):
            raise ValueError("root_resume_lease_mismatch")

    async def _now(self, session: AsyncSession) -> datetime:
        """读取并规范数据库 UTC 时钟。"""
        value = self._clock() if self._clock is not None else await session.scalar(select(func.clock_timestamp()))
        if value is None:
            raise RuntimeError("database_clock_unavailable")
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


__all__ = ["PostgresRootResumeJobRepository"]
