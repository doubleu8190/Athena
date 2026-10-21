"""PostgreSQL Worker Job claim、lease 和 fencing 适配器。"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.orchestration import (
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
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
)


@dataclass(frozen=True, slots=True)
class _Candidate:
    job_id: str
    run_id: str
    plan_id: str
    round_id: str
    task_id: str
    execution_id: str


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
                WorkerJobModel.job_type == "execute",
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
        if is_reclaim or previous_owner is not None:
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
        return {
            parent: _result_from_row(by_id[parent])
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
        and job.job_type == "execute"
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


def _result_from_row(row: AgentTaskModel) -> TaskResult:
    """恢复已完成上游的结果、证据和不确定性。"""
    raw = _load_json(row.output_json, {})
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
