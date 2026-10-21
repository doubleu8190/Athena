"""PostgreSQL Worker Job claim、lease 和 fencing 适配器。"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.orchestration import (
    RunCancellationReceipt,
    Task,
    TaskResult,
    TaskStatus,
    WorkerJobLease,
    WorkerJobRepository,
)
from ..models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    RootResumeJobModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from .task_execution_results import load_task_execution_outputs


@dataclass(frozen=True, slots=True)
class _Candidate:
    job_id: str
    run_id: str
    plan_id: str
    round_id: str
    task_id: str
    execution_id: str
    job_type: str
    approval_id: str | None
    resume_value: dict[str, Any]


class PostgresWorkerJobRepository(WorkerJobRepository):
    """按稳定锁顺序领取执行，所有权变化时递增 fencing token。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
        *,
        clock: Callable[[], datetime] | None = None,
        candidate_limit: int = 64,
    ) -> None:
        """保存会话工厂、时钟和单次领取最多扫描的候选数。"""
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        self._session_factory = session_factory
        self._clock = clock
        self._candidate_limit = candidate_limit

    async def claim_next(
        self, worker_id: str, *, lease_seconds: float
    ) -> WorkerJobLease | None:
        """短事务领取一个 execute Job，并返回完整 Worker 输入快照。"""
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                candidates = await self._candidate_rows(session, now)
                for candidate in candidates:
                    lease = await self._claim_candidate(
                        session,
                        candidate,
                        worker_id=worker_id,
                        now=now,
                        lease_seconds=lease_seconds,
                    )
                    if lease is not None:
                        return lease
        return None

    async def renew_lease(
        self, lease: WorkerJobLease, *, lease_seconds: float
    ) -> WorkerJobLease | None:
        """仅当前 owner 持有未过期栅栏时延长 Job 与 Execution 租约。"""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                run, plan, round_row, task, round_task, execution, job = (
                    await self._lock_execution_chain(session, lease)
                )
                if any(
                    item is None
                    for item in (
                        run,
                        plan,
                        round_row,
                        task,
                        round_task,
                        execution,
                        job,
                    )
                ):
                    return None

                assert run is not None
                assert plan is not None
                assert round_row is not None
                assert task is not None
                assert round_task is not None
                assert execution is not None
                assert job is not None
                if not _owns_lease(
                    lease, run, plan, round_row, task, round_task, execution, job
                ):
                    return None
                if not _is_future(job.lease_until, now) or not _is_future(
                    execution.lease_until, now
                ):
                    return None

                expires_at = now + timedelta(seconds=lease_seconds)
                job.lease_until = expires_at
                job.updated_at = now
                execution.lease_until = expires_at
                execution.updated_at = now
                return replace(lease, lease_until=expires_at)

    async def wait_for_approval(
        self,
        lease: WorkerJobLease,
        approval: Mapping[str, object],
    ) -> bool:
        """原子登记 Worker 审批，并释放该执行的活跃租约。

        Args:
            lease: 当前 Worker Job 的有效租约。
            approval: 已校验的审批身份、工具和冻结参数。

        Returns:
            审批记录与执行状态同时持久化时返回 True。

        Raises:
            无：身份不匹配或租约失效会返回 False。
        """
        async with self._session_factory() as session:
            async with session.begin():
                prior = (
                    await session.execute(
                        select(ApprovalRecordModel).where(
                            ApprovalRecordModel.worker_thread_id
                            == approval.get("worker_thread_id"),
                            ApprovalRecordModel.interrupt_id
                            == approval.get("interrupt_id"),
                        )
                    )
                ).scalar_one_or_none()
                if prior is not None:
                    return prior.approval_id == approval.get("approval_id")

                run, plan, round_row, task, round_task, execution, job = (
                    await self._lock_execution_chain(session, lease)
                )
                if any(
                    item is None
                    for item in (
                        run,
                        plan,
                        round_row,
                        task,
                        round_task,
                        execution,
                        job,
                    )
                ):
                    return False
                assert run is not None
                assert plan is not None
                assert round_row is not None
                assert task is not None
                assert round_task is not None
                assert execution is not None
                assert job is not None
                if (
                    lease.job_type != "execute"
                    or not _owns_lease(
                        lease, run, plan, round_row, task, round_task, execution, job
                    )
                    or approval.get("run_id") != run.run_id
                    or approval.get("plan_id") != plan.plan_id
                    or approval.get("task_id") != task.task_id
                    or approval.get("execution_id") != execution.execution_id
                    or approval.get("worker_thread_id")
                    != execution.worker_thread_id
                    or not approval.get("approval_id")
                    or not approval.get("interrupt_id")
                    or not approval.get("tool_call_id")
                    or not approval.get("tool_name")
                    or not approval.get("args_hash")
                    or not approval.get("operation_key")
                ):
                    return False

                now = await self._now(session)
                session.add(
                    ApprovalRecordModel(
                        approval_id=str(approval["approval_id"]),
                        session_id=run.session_id,
                        run_id=run.run_id,
                        plan_id=plan.plan_id,
                        task_id=task.task_id,
                        execution_id=execution.execution_id,
                        worker_thread_id=execution.worker_thread_id,
                        interrupt_id=str(approval["interrupt_id"]),
                        tool_call_id=str(approval["tool_call_id"]),
                        tool_name=str(approval["tool_name"]),
                        arguments_json=json.dumps(
                            dict(approval.get("arguments", {})),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        risk_level=str(approval.get("risk_level", "high")),
                        args_hash=str(approval["args_hash"]),
                        operation_key=str(approval["operation_key"]),
                        status="pending",
                        created_at=now.isoformat(),
                    )
                )
                execution.status = "waiting_approval"
                execution.lease_owner = None
                execution.lease_until = None
                execution.fencing_token += 1
                execution.updated_at = now
                job.status = "completed"
                job.lease_owner = None
                job.lease_until = None
                job.updated_at = now
                task.status = "waiting_approval"
                task.error_json = json.dumps(
                    {"approval_id": str(approval["approval_id"])},
                    ensure_ascii=False,
                )
                task.updated_at = now.isoformat()
                round_task.status = "waiting_approval"
                round_task.terminal_at = None
                return True

    async def acknowledge_cancelled(self, lease: WorkerJobLease) -> bool:
        """只接受当前取消代际的 Worker 退出确认并释放其租约。"""
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                run, plan, round_row, task, round_task, execution, job = (
                    await self._lock_execution_chain(session, lease)
                )
                if any(
                    item is None
                    for item in (
                        run,
                        plan,
                        round_row,
                        task,
                        round_task,
                        execution,
                        job,
                    )
                ):
                    return False
                assert run is not None and plan is not None
                assert round_row is not None and task is not None
                assert round_task is not None and execution is not None
                assert job is not None
                if (
                    run.status != "cancelling"
                    or plan.status != "cancelling"
                    or run.cancellation_epoch != plan.cancellation_epoch
                    or execution.cancellation_epoch != run.cancellation_epoch
                    or lease.cancellation_epoch == run.cancellation_epoch
                    or task.status != "cancelling"
                    or round_task.status != "cancelling"
                    or execution.status != "cancel_requested"
                    or job.status != "cancel_requested"
                    or job.lease_owner != lease.worker_id
                    or execution.lease_owner != lease.worker_id
                    or execution.fencing_token != lease.fencing_token
                ):
                    return False

                job.status = "cancelled"
                job.lease_owner = None
                job.lease_until = None
                job.updated_at = now
                execution.status = "cancelled"
                execution.lease_owner = None
                execution.lease_until = None
                execution.updated_at = now
                task.status = "cancelled"
                task.updated_at = now.isoformat()
                round_task.status = "cancelled"
                round_task.terminal_at = now
                round_row.terminal_task_count = min(
                    round_row.expected_task_count,
                    round_row.terminal_task_count + 1,
                )
                return True

    async def reconcile_cancellations(self) -> list[RunCancellationReceipt]:
        """回收租约过期的取消执行，并在整组 Worker 收敛后终结 Run。"""
        receipts: list[RunCancellationReceipt] = []
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                run_ids = (
                    await session.execute(
                        select(AgentRunModel.run_id)
                        .where(AgentRunModel.status == "cancelling")
                        .order_by(AgentRunModel.updated_at)
                        .limit(self._candidate_limit)
                    )
                ).scalars().all()
                for run_id in run_ids:
                    run = await session.get(
                        AgentRunModel, run_id, with_for_update=True
                    )
                    if run is None or run.status != "cancelling":
                        continue
                    plan = (
                        await session.execute(
                            select(AgentPlanModel)
                            .where(AgentPlanModel.run_id == run_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if plan is None or plan.status != "cancelling":
                        continue

                    # 只把取消租约已过期的执行标记为可靠隔离；仍在租期内的
                    # Worker 会通过续租失败收到协作取消并主动确认退出。
                    expired_rows = (
                        await session.execute(
                            select(
                                WorkerJobModel.job_id,
                                TaskExecutionModel.execution_id,
                                AgentTaskModel.task_id,
                                RoundTaskModel.round_id,
                            )
                            .join(
                                TaskExecutionModel,
                                WorkerJobModel.execution_id
                                == TaskExecutionModel.execution_id,
                            )
                            .join(
                                AgentTaskModel,
                                TaskExecutionModel.task_id == AgentTaskModel.task_id,
                            )
                            .join(
                                RoundTaskModel,
                                (RoundTaskModel.round_id == TaskExecutionModel.round_id)
                                & (RoundTaskModel.task_id == TaskExecutionModel.task_id),
                            )
                            .where(
                                AgentTaskModel.plan_id == plan.plan_id,
                                WorkerJobModel.status == "cancel_requested",
                                or_(
                                    WorkerJobModel.lease_until.is_(None),
                                    WorkerJobModel.lease_until <= now,
                                ),
                            )
                        )
                    ).all()
                    for job_id, execution_id, task_id, round_id in expired_rows:
                        await session.execute(
                            update(WorkerJobModel)
                            .where(
                                WorkerJobModel.job_id == job_id,
                                WorkerJobModel.status == "cancel_requested",
                            )
                            .values(
                                status="cancelled",
                                lease_owner=None,
                                lease_until=None,
                                updated_at=now,
                            )
                        )
                        await session.execute(
                            update(TaskExecutionModel)
                            .where(
                                TaskExecutionModel.execution_id == execution_id,
                                TaskExecutionModel.status == "cancel_requested",
                            )
                            .values(
                                status="cancelled",
                                lease_owner=None,
                                lease_until=None,
                                updated_at=now,
                            )
                        )
                        await session.execute(
                            update(AgentTaskModel)
                            .where(
                                AgentTaskModel.task_id == task_id,
                                AgentTaskModel.status == "cancelling",
                            )
                            .values(status="cancelled", updated_at=now.isoformat())
                        )
                        await session.execute(
                            update(RoundTaskModel)
                            .where(
                                RoundTaskModel.round_id == round_id,
                                RoundTaskModel.task_id == task_id,
                                RoundTaskModel.status == "cancelling",
                            )
                            .values(status="cancelled", terminal_at=now)
                        )

                    active_count = (
                        await session.execute(
                            select(func.count())
                            .select_from(TaskExecutionModel)
                            .join(
                                AgentTaskModel,
                                TaskExecutionModel.task_id == AgentTaskModel.task_id,
                            )
                            .where(
                                AgentTaskModel.plan_id == plan.plan_id,
                                TaskExecutionModel.status == "cancel_requested",
                            )
                        )
                    ).scalar_one()
                    if active_count:
                        continue

                    await session.execute(
                        update(AgentTaskModel)
                        .where(
                            AgentTaskModel.plan_id == plan.plan_id,
                            AgentTaskModel.status == "cancelling",
                        )
                        .values(status="cancelled", updated_at=now.isoformat())
                    )
                    await session.execute(
                        update(RoundTaskModel)
                        .where(
                            RoundTaskModel.task_id.in_(
                                select(AgentTaskModel.task_id).where(
                                    AgentTaskModel.plan_id == plan.plan_id
                                )
                            ),
                            RoundTaskModel.status == "cancelling",
                        )
                        .values(status="cancelled", terminal_at=now)
                    )
                    await session.execute(
                        update(PlanRoundModel)
                        .where(
                            PlanRoundModel.plan_id == plan.plan_id,
                            PlanRoundModel.status == "running",
                        )
                        .values(
                            status="cancelled",
                            terminal_task_count=PlanRoundModel.expected_task_count,
                            completed_at=now,
                        )
                    )
                    plan.status = "cancelled"
                    plan.updated_at = now.isoformat()
                    run.status = "cancelled"
                    run.updated_at = now.isoformat()
                    await session.execute(
                        update(RootResumeJobModel)
                        .where(
                            RootResumeJobModel.run_id == run.run_id,
                            RootResumeJobModel.status.in_(("pending", "running")),
                        )
                        .values(
                            status="cancelled",
                            lease_owner=None,
                            lease_until=None,
                            updated_at=now,
                        )
                    )
                    session.add_all(
                        [
                            self._cancellation_outbox(
                                run=run,
                                plan=plan,
                                event_type="PlanCancelled",
                                dedupe_key=f"plan:{plan.plan_id}:cancelled:{plan.cancellation_epoch}",
                                now=now,
                            ),
                            self._cancellation_outbox(
                                run=run,
                                plan=plan,
                                event_type="RunCancelled",
                                dedupe_key=f"run:{run.run_id}:cancelled:{run.cancellation_epoch}",
                                now=now,
                            ),
                        ]
                    )
                    receipts.append(
                        RunCancellationReceipt(
                            run_id=run.run_id,
                            session_id=run.session_id,
                            plan_id=plan.plan_id,
                            cancellation_epoch=run.cancellation_epoch,
                            external_effect_status=(
                                run.cancellation_external_effect_status
                            ),
                        )
                    )
        return receipts

    def _cancellation_outbox(
        self,
        *,
        run: AgentRunModel,
        plan: AgentPlanModel,
        event_type: str,
        dedupe_key: str,
        now: datetime,
    ) -> OrchestrationOutboxModel:
        """构造 Plan/Run 取消终态的事务性 Outbox 记录。"""
        return OrchestrationOutboxModel(
            message_id=uuid4().hex,
            dedupe_key=dedupe_key,
            event_type=event_type,
            correlation_id=run.run_id,
            plan_id=plan.plan_id,
            plan_version=plan.current_version,
            round_id=plan.active_round_id,
            payload_json={
                "cancellation_epoch": run.cancellation_epoch,
                "external_effect_status": run.cancellation_external_effect_status,
            },
            schema_version=1,
            status="pending",
            attempt=0,
            available_at=now,
            occurred_at=now,
        )

    async def _now(self, session: AsyncSession) -> datetime:
        """优先使用 PostgreSQL 时钟，避免多 Worker 主机时钟偏差。"""
        value = (
            self._clock()
            if self._clock is not None
            else await session.scalar(select(func.clock_timestamp()))
        )
        return _utc(value)

    async def _candidate_rows(self, session: AsyncSession, now: datetime):
        """读取未锁定候选 ID；实际领取仍按 Run 到 Job 顺序加锁复核。"""
        ready_job = and_(
            WorkerJobModel.status == "pending",
            WorkerJobModel.available_at <= now,
        )
        expired_job = and_(
            WorkerJobModel.status == "running",
            WorkerJobModel.lease_until.is_not(None),
            WorkerJobModel.lease_until <= now,
        )
        statement = (
            select(
                WorkerJobModel.job_id,
                AgentRunModel.run_id,
                AgentPlanModel.plan_id,
                PlanRoundModel.round_id,
                AgentTaskModel.task_id,
                TaskExecutionModel.execution_id,
                WorkerJobModel.job_type,
                WorkerJobModel.approval_id,
                WorkerJobModel.payload_json,
            )
            .join(
                TaskExecutionModel,
                WorkerJobModel.execution_id == TaskExecutionModel.execution_id,
            )
            .join(AgentTaskModel, TaskExecutionModel.task_id == AgentTaskModel.task_id)
            .join(AgentPlanModel, AgentTaskModel.plan_id == AgentPlanModel.plan_id)
            .join(AgentRunModel, AgentPlanModel.run_id == AgentRunModel.run_id)
            .join(PlanRoundModel, TaskExecutionModel.round_id == PlanRoundModel.round_id)
            .where(
                WorkerJobModel.job_type.in_(("execute", "resume_worker")),
                or_(ready_job, expired_job),
            )
            .order_by(WorkerJobModel.available_at, WorkerJobModel.created_at)
            .limit(self._candidate_limit)
        )
        rows = (await session.execute(statement)).all()
        return [_Candidate(*row) for row in rows]

    async def _claim_candidate(
        self,
        session: AsyncSession,
        candidate,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: float,
    ) -> WorkerJobLease | None:
        run, plan, round_row, task_row, round_task, execution, job = (
            await self._lock_execution_chain(session, candidate, skip_locked=True)
        )
        if any(
            item is None
            for item in (run, plan, round_row, task_row, round_task, execution, job)
        ):
            return None
        assert run is not None
        assert plan is not None
        assert round_row is not None
        assert task_row is not None
        assert round_task is not None
        assert execution is not None
        assert job is not None

        if (
            run.status != "running"
            or plan.status != "running"
            or plan.run_id != run.run_id
            or plan.active_round_id != round_row.round_id
            or plan.current_version != round_row.plan_version
            or round_row.status != "running"
            or task_row.run_id != run.run_id
            or task_row.plan_id != plan.plan_id
            or task_row.active_execution_id != execution.execution_id
            or round_task.active_execution_id != execution.execution_id
            or execution.cancellation_epoch != run.cancellation_epoch
            or execution.cancellation_epoch != plan.cancellation_epoch
        ):
            return None

        is_resume = candidate.job_type == "resume_worker"
        if candidate.job_type not in {"execute", "resume_worker"}:
            return None
        resume_value = _load_json(candidate.resume_value, {})
        if is_resume:
            approval = (
                await session.get(ApprovalRecordModel, candidate.approval_id)
                if candidate.approval_id
                else None
            )
            if (
                approval is None
                or approval.status != "resolved"
                or approval.decision not in {"approved", "denied"}
                or approval.execution_id != execution.execution_id
                or approval.worker_thread_id != execution.worker_thread_id
                or approval.interrupt_id != resume_value.get("interrupt_id")
                or approval.args_hash != resume_value.get("args_hash")
                or approval.operation_key != resume_value.get("operation_key")
                or approval.decision != resume_value.get("decision")
            ):
                return None

        is_reclaim = job.status == "running"
        if is_reclaim:
            if (
                execution.status != "running"
                or task_row.status != "running"
                or round_task.status != "running"
                or not _is_expired(job.lease_until, now)
                or not _is_expired(execution.lease_until, now)
            ):
                return None
        elif is_resume:
            if (
                job.status != "pending"
                or execution.status != "waiting_approval"
                or task_row.status != "waiting_approval"
                or round_task.status != "waiting_approval"
                or job.available_at > now
            ):
                return None
        elif (
            job.status != "pending"
            or execution.status != "queued"
            or task_row.status != "pending"
            or round_task.status != "pending"
            or job.available_at > now
        ):
            return None

        running_elsewhere = (
            await session.execute(
                select(func.count())
                .select_from(TaskExecutionModel)
                .join(
                    AgentTaskModel,
                    TaskExecutionModel.task_id == AgentTaskModel.task_id,
                )
                .where(
                    AgentTaskModel.plan_id == plan.plan_id,
                    TaskExecutionModel.status == "running",
                    TaskExecutionModel.execution_id != execution.execution_id,
                )
            )
        ).scalar_one()
        if running_elsewhere >= plan.max_parallelism:
            return None

        previous_owner = execution.lease_owner
        if is_resume or is_reclaim or previous_owner is not None:
            execution.fencing_token += 1
        expires_at = now + timedelta(seconds=lease_seconds)
        job.status = "running"
        job.lease_owner = worker_id
        job.lease_until = expires_at
        job.attempt += 1
        job.updated_at = now
        execution.status = "running"
        execution.lease_owner = worker_id
        execution.lease_until = expires_at
        execution.updated_at = now
        task_row.status = "running"
        task_row.execution_generation = execution.attempt
        task_row.worker_thread_id = execution.worker_thread_id
        task_row.updated_at = now.isoformat()
        round_task.status = "running"

        task = _task_from_row(task_row)
        upstream = await self._load_upstream_results(
            session,
            plan_id=plan.plan_id,
            plan_version=round_row.plan_version,
            task_id=task_row.task_id,
        )
        return WorkerJobLease(
            job_id=job.job_id,
            worker_id=worker_id,
            run_id=run.run_id,
            plan_id=plan.plan_id,
            round_id=round_row.round_id,
            plan_version=round_row.plan_version,
            task_id=task_row.task_id,
            execution_id=execution.execution_id,
            session_id=run.session_id,
            attempt=execution.attempt,
            worker_thread_id=execution.worker_thread_id,
            fencing_token=execution.fencing_token,
            cancellation_epoch=execution.cancellation_epoch,
            lease_until=expires_at,
            task=task,
            upstream_results=upstream,
            job_type=candidate.job_type,
            approval_id=candidate.approval_id,
            resume_value=resume_value if is_resume else {},
        )

    async def _lock_execution_chain(
        self,
        session: AsyncSession,
        lease: WorkerJobLease | _Candidate,
        *,
        skip_locked: bool = False,
    ):
        """按 Run、Plan、Round、Task、RoundTask、Execution、Job 顺序加锁。"""
        run = await _locked_one(
            session,
            AgentRunModel,
            AgentRunModel.run_id == lease.run_id,
            skip_locked=skip_locked,
        )
        plan = await _locked_one(
            session,
            AgentPlanModel,
            AgentPlanModel.plan_id == lease.plan_id,
            skip_locked=skip_locked,
        )
        round_row = await _locked_one(
            session,
            PlanRoundModel,
            PlanRoundModel.round_id == lease.round_id,
            skip_locked=skip_locked,
        )
        task = await _locked_one(
            session,
            AgentTaskModel,
            AgentTaskModel.task_id == lease.task_id,
            skip_locked=skip_locked,
        )
        round_task = await _locked_one(
            session,
            RoundTaskModel,
            and_(
                RoundTaskModel.round_id == lease.round_id,
                RoundTaskModel.task_id == lease.task_id,
            ),
            skip_locked=skip_locked,
        )
        execution = await _locked_one(
            session,
            TaskExecutionModel,
            TaskExecutionModel.execution_id == lease.execution_id,
            skip_locked=skip_locked,
        )
        job = await _locked_one(
            session,
            WorkerJobModel,
            WorkerJobModel.job_id == lease.job_id,
            skip_locked=skip_locked,
        )
        return run, plan, round_row, task, round_task, execution, job

    async def _load_upstream_results(
        self,
        session: AsyncSession,
        *,
        plan_id: str,
        plan_version: int,
        task_id: str,
    ) -> dict[str, TaskResult]:
        """按冻结 DAG 版本装配直接上游已持久化结果。"""
        version = await session.get(PlanVersionModel, (plan_id, plan_version))
        if version is None:
            return {}
        edges = version.dag_json.get("edges", [])
        parents = sorted(
            str(edge.get("from"))
            for edge in edges
            if str(edge.get("to")) == task_id and edge.get("from")
        )
        if not parents:
            return {}
        rows = (
            await session.execute(
                select(AgentTaskModel).where(
                    AgentTaskModel.plan_id == plan_id,
                    AgentTaskModel.task_id.in_(parents),
                )
            )
        ).scalars().all()
        by_id = {row.task_id: row for row in rows}
        execution_ids = [
            by_id[parent].active_execution_id
            for parent in parents
            if parent in by_id and by_id[parent].active_execution_id
        ]
        executions = (
            (
                await session.execute(
                    select(TaskExecutionModel).where(
                        TaskExecutionModel.execution_id.in_(execution_ids)
                    )
                )
            ).scalars().all()
            if execution_ids
            else []
        )
        outputs_by_execution = await load_task_execution_outputs(
            session, executions
        )
        return {
            parent: _result_from_row(
                by_id[parent],
                outputs_by_execution.get(by_id[parent].active_execution_id),
            )
            for parent in parents
            if parent in by_id
        }


async def _locked_one(
    session: AsyncSession,
    model: type,
    predicate: Any,
    *,
    skip_locked: bool = False,
):
    """读取单行并持有当前事务的行锁。"""
    return (
        await session.execute(
            select(model).where(predicate).with_for_update(skip_locked=skip_locked)
        )
    ).scalar_one_or_none()


def _owns_lease(lease, run, plan, round_row, task, round_task, execution, job) -> bool:
    """确认租约仍属于同一 Worker、执行和版本。"""
    return (
        run.status == "running"
        and plan.status == "running"
        and plan.active_round_id == round_row.round_id
        and plan.current_version == round_row.plan_version
        and round_row.plan_version == lease.plan_version
        and round_row.status == "running"
        and round_task.status == "running"
        and round_task.active_execution_id == execution.execution_id
        and task.status == "running"
        and task.plan_id == plan.plan_id
        and task.run_id == run.run_id
        and task.active_execution_id == execution.execution_id
        and execution.status == "running"
        and job.status == "running"
        and job.lease_owner == lease.worker_id
        and execution.lease_owner == lease.worker_id
        and execution.fencing_token == lease.fencing_token
        and execution.task_id == lease.task_id
        and execution.round_id == lease.round_id
        and job.execution_id == lease.execution_id
        and job.job_type == lease.job_type
        and execution.cancellation_epoch == lease.cancellation_epoch
        and run.cancellation_epoch == lease.cancellation_epoch
        and plan.cancellation_epoch == lease.cancellation_epoch
    )


def _task_from_row(row: AgentTaskModel) -> Task:
    """将旧 Task 投影转换为 Worker 领域输入。"""
    return Task(
        task_id=row.task_id,
        title=row.title,
        objective=row.objective,
        input=_load_json(row.input_json, {}),
        expected_output=_load_json(row.expected_output_json, {}),
        allowed_tools=tuple(_load_json(row.allowed_tools_json, [])),
        worker_type=row.worker_type,
    )


def _result_from_row(
    row: AgentTaskModel,
    resolved_output: dict[str, Any] | None = None,
) -> TaskResult:
    """恢复已完成上游的结果、证据和不确定性。"""
    raw = (
        resolved_output
        if resolved_output is not None
        else _load_json(row.output_json, {})
    )
    if (
        resolved_output is None
        and raw.get("result_ref")
        and {"result_bytes", "result_sha256"}.issubset(raw)
    ):
        raise RuntimeError("task_execution_result_reference_unavailable")
    output = raw.get("output", raw)
    if not isinstance(output, dict):
        output = {"value": output}
    try:
        status = TaskStatus(row.status)
    except ValueError:
        status = TaskStatus.FAILED
    error = _load_json(row.error_json, None)
    return TaskResult(
        task_id=row.task_id,
        status=status,
        output=output,
        evidence=tuple(raw.get("evidence", [])),
        uncertainties=tuple(str(item) for item in raw.get("uncertainties", [])),
        error=error,
        retryable=bool(raw.get("retryable", False)),
    )


def _load_json(value: str | dict[str, Any] | None, default: Any) -> Any:
    """兼容当前字符串 JSON 投影与新 JSONB 文档。"""
    if value is None or value == "":
        return default
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return default
    return loaded if isinstance(loaded, dict) else default


def _utc(value: datetime) -> datetime:
    """规范化租约计算所用的 UTC 时间。"""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _is_future(value: datetime | None, now: datetime) -> bool:
    """检查数据库租约尚未过期。"""
    return value is not None and _utc(value) > now


def _is_expired(value: datetime | None, now: datetime) -> bool:
    """检查数据库租约已过期。"""
    return value is not None and _utc(value) <= now


__all__ = ["PostgresWorkerJobRepository"]
