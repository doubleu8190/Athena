"""PostgreSQL Worker Job claim、lease 和 fencing 适配器。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.orchestration import (
    RunCancellationReceipt,
    Task,
    TaskResult,
    TaskStatus,
    WorkerJobLease,
    WorkerJobRepository,
)
from domain.runs import RunStartPayload
from domain.leases import LeaseGrant, LeaseKey, LeaseLostError, LeaseRecord
from application.contracts.runs import RunStartPayloadSchema
from ..models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    MemoryProcessingJobModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    RootResumeJobModel,
    SessionModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from .task_execution_results import load_task_execution_outputs
from .memory_job_repository import enqueue_memory_job_in_transaction
from .memory_suppression import ensure_writable_run_scope
from .lease_center import PostgresLeaseCenter


@dataclass(frozen=True, slots=True)
class _Candidate:
    job_id: str
    run_id: str
    session_id: str
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
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        """保存会话工厂、时钟和单次领取最多扫描的候选数。"""
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        self._session_factory = session_factory
        self._clock = clock
        self._candidate_limit = candidate_limit
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

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
                after_resource_id: str | None = None
                while True:
                    claimable = await self._leases.claimable_keys_in_transaction(
                        session,
                        "worker_execution",
                        limit=self._candidate_limit * 4,
                        after_resource_id=after_resource_id,
                    )
                    if not claimable:
                        return None
                    candidates = await self._candidate_rows(
                        session, now, {key.resource_id for key in claimable}
                    )
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
                    after_resource_id = claimable[-1].resource_id

    async def renew_lease(
        self, lease: WorkerJobLease, *, lease_seconds: float
    ) -> WorkerJobLease | None:
        """仅续期租约中心中仍有效的执行权。"""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self._session_factory() as session:
            async with session.begin():
                grant = await self._leases.renew_in_transaction(
                    session, lease.lease, lease_seconds=lease_seconds
                )
                return None if grant is None else replace(lease, lease=grant)

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
            审批记录、执行状态和可重放通知同时持久化时返回 True。

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
                    lease.job_type not in {"execute", "resume_worker"}
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
                try:
                    await self._leases.assert_current_in_transaction(
                        session, lease.lease
                    )
                except Exception:
                    return False
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
                        arguments_json=dict(approval.get("arguments", {})),
                        risk_level=str(approval.get("risk_level", "high")),
                        args_hash=str(approval["args_hash"]),
                        operation_key=str(approval["operation_key"]),
                        status="pending",
                        created_at=now,
                        expires_at=approval.get("expires_at"),
                    )
                )
                session.add(
                    OrchestrationOutboxModel(
                        message_id=uuid4().hex,
                        dedupe_key=(
                            f"approval:{approval['approval_id']}:requested"
                        ),
                        event_type="ApprovalRequested",
                        correlation_id=run.run_id,
                        plan_id=plan.plan_id,
                        plan_version=plan.current_version,
                        round_id=round_row.round_id,
                        payload_json={
                            "approval_id": str(approval["approval_id"]),
                            "task_id": task.task_id,
                            "execution_id": execution.execution_id,
                            "worker_thread_id": execution.worker_thread_id,
                            "interrupt_id": str(approval["interrupt_id"]),
                            "tool_call_id": str(approval["tool_call_id"]),
                            "tool_name": str(approval["tool_name"]),
                            "arguments": dict(approval.get("arguments", {})),
                            "risk_level": str(approval.get("risk_level", "high")),
                            "args_hash": str(approval["args_hash"]),
                            "operation_key": str(approval["operation_key"]),
                            "expires_at": approval.get("expires_at").isoformat()
                            if approval.get("expires_at") is not None
                            else None,
                            "parent_activity_id": f"task:{task.task_id}",
                        },
                        schema_version=1,
                        status="pending",
                        attempt=0,
                        available_at=now,
                        occurred_at=now,
                    )
                )
                execution.status = "waiting_approval"
                execution.updated_at = now
                job.status = "completed"
                job.updated_at = now
                task.status = "waiting_approval"
                task.error_json = {"approval_id": str(approval["approval_id"])}
                task.updated_at = now
                round_task.status = "waiting_approval"
                round_task.terminal_at = None
                if not await self._leases.finish_in_transaction(
                    session, lease.lease
                ):
                    raise LeaseLostError(lease.lease.key)
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
                    run.status != "stopping"
                    or plan.status != "cancelling"
                    or run.cancellation_epoch != plan.cancellation_epoch
                    or execution.cancellation_epoch != run.cancellation_epoch
                    or lease.cancellation_epoch == run.cancellation_epoch
                    or task.status != "cancelling"
                    or round_task.status != "cancelling"
                    or execution.status != "cancel_requested"
                    or job.status != "cancel_requested"
                ):
                    return False

                if not await self._leases.acknowledge_stopped_in_transaction(
                    session, lease.lease, completed=True
                ):
                    return False

                job.status = "cancelled"
                job.updated_at = now
                execution.status = "cancelled"
                execution.updated_at = now
                task.status = "cancelled"
                task.updated_at = now
                round_task.status = "cancelled"
                round_task.terminal_at = now
                round_row.terminal_task_count = min(
                    round_row.expected_task_count,
                    round_row.terminal_task_count + 1,
                )
                session.add(
                    self._task_cancelled_outbox(
                        run=run,
                        plan=plan,
                        round_id=round_row.round_id,
                        task_id=task.task_id,
                        execution_id=execution.execution_id,
                        execution_generation=execution.attempt,
                        title=task.title,
                        objective=task.objective,
                        now=now,
                    )
                )
                return True

    async def reconcile_cancellations(self) -> list[RunCancellationReceipt]:
        """在一个事务中回收过期取消租约并收敛已停止的 Run。

        Returns:
            本轮完成取消收敛的 Run 回执。

        Raises:
            Exception: 数据库错误由队列循环重试。
        """
        receipts: list[RunCancellationReceipt] = []
        async with self._session_factory() as session:
            async with session.begin():
                now = await self._now(session)
                run_ids = (
                    await session.execute(
                        select(AgentRunModel.run_id)
                        .where(AgentRunModel.status == "stopping")
                        .order_by(AgentRunModel.updated_at)
                        .limit(self._candidate_limit)
                    )
                ).scalars().all()
                for run_id in run_ids:
                    receipt = await self._reconcile_cancelling_run(
                        session, run_id=run_id, now=now
                    )
                    if receipt is not None:
                        receipts.append(receipt)
        return receipts

    async def _reconcile_cancelling_run(
        self, session: AsyncSession, *, run_id: str, now: datetime
    ) -> RunCancellationReceipt | None:
        """锁定 Run 与 Plan，回收过期租约后决定是否可完成取消。"""
        session_id = await session.scalar(
            select(AgentRunModel.session_id).where(AgentRunModel.run_id == run_id)
        )
        if session_id is None:
            return None
        session_row = await session.scalar(
            select(SessionModel)
            .where(SessionModel.id == session_id)
            .with_for_update()
        )
        run = await session.get(AgentRunModel, run_id, with_for_update=True)
        if (
            session_row is None
            or run is None
            or run.status != "stopping"
            or session_row.active_run_id != run_id
        ):
            return None
        plan = (
            await session.execute(
                select(AgentPlanModel)
                .where(AgentPlanModel.run_id == run_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        root_lease = await self._leases.get_in_transaction(
            session, LeaseKey("root_execution", run_id)
        )
        if (
            root_lease is not None
            and root_lease.state in {"active", "revoked"}
            and root_lease.lease_until is not None
            and root_lease.lease_until > now
        ):
            return None
        if plan is None or plan.status in {"completed", "failed", "cancelled"}:
            failed = run.error_json is not None
            run.status = "failed" if failed else "cancelled"
            run.updated_at = now
            if root_lease is not None:
                await self._leases.acknowledge_stopped_in_transaction(
                    session, _lease_grant_from_record(root_lease), completed=True
                )
            if session_row.active_run_id == run.run_id:
                session_row.active_run_id = None
            await self._enqueue_cancelled_run_memory_job(session, run=run, now=now)
            session.add(
                OrchestrationOutboxModel(
                    message_id=uuid4().hex,
                    dedupe_key=f"run:{run.run_id}:{run.status}:{run.cancellation_epoch}",
                    event_type="RunFailed" if failed else "RunCancelled",
                    correlation_id=run.run_id,
                    plan_id=plan.plan_id if plan is not None else None,
                    plan_version=plan.current_version if plan is not None else None,
                    round_id=plan.active_round_id if plan is not None else None,
                    payload_json={
                        "cancellation_epoch": run.cancellation_epoch,
                        "external_effect_status": run.cancellation_external_effect_status,
                        "error": run.error_json if failed else None,
                    },
                    schema_version=1,
                    status="pending",
                    attempt=0,
                    available_at=now,
                    occurred_at=now,
                )
            )
            return RunCancellationReceipt(
                run_id=run.run_id,
                session_id=run.session_id,
                plan_id=plan.plan_id if plan is not None else "",
                cancellation_epoch=run.cancellation_epoch,
                external_effect_status=run.cancellation_external_effect_status,
            )
        if plan.status != "cancelling":
            return None

        await self._cancel_expired_worker_executions(
            session, run=run, plan=plan, now=now
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
            return None
        return await self._finalize_cancellation(
            session, run=run, plan=plan, now=now
        )

    async def _cancel_expired_worker_executions(
        self,
        session: AsyncSession,
        *,
        run: AgentRunModel,
        plan: AgentPlanModel,
        now: datetime,
    ) -> None:
        """隔离租约已过期的取消执行并记录对应任务事件。"""
        # 只有租约到期后才能确认 Worker 已失去所有权并安全完成取消。
        cancelled_execution_ids = list((await session.scalars(
            select(TaskExecutionModel.execution_id)
            .join(WorkerJobModel, WorkerJobModel.execution_id == TaskExecutionModel.execution_id)
            .join(AgentTaskModel, AgentTaskModel.task_id == TaskExecutionModel.task_id)
            .where(
                AgentTaskModel.plan_id == plan.plan_id,
                WorkerJobModel.status == "cancel_requested",
            )
        )).all())
        lease_by_execution = await self._leases.get_many_in_transaction(
            session, "worker_execution", cancelled_execution_ids
        )
        lease_by_execution = {
            execution_id: record
            for execution_id, record in lease_by_execution.items()
            if record.state in {"pending", "released"}
            or (
                record.state in {"active", "revoked"}
                and record.lease_until is not None
                and record.lease_until <= now
            )
        }
        execution_ids = list(lease_by_execution)
        if not execution_ids:
            return
        expired_rows = (
            await session.execute(
                select(
                    WorkerJobModel.job_id,
                    TaskExecutionModel.execution_id,
                    AgentTaskModel.task_id,
                    RoundTaskModel.round_id,
                    AgentTaskModel.title,
                    AgentTaskModel.objective,
                    TaskExecutionModel.attempt,
                )
                .join(
                    TaskExecutionModel,
                    WorkerJobModel.execution_id == TaskExecutionModel.execution_id,
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
                    TaskExecutionModel.execution_id.in_(execution_ids),
                )
            )
        ).all()
        for (
            job_id,
            execution_id,
            task_id,
            round_id,
            title,
            objective,
            attempt,
        ) in expired_rows:
            lease_record = lease_by_execution.get(execution_id)
            lease_owner = lease_record.owner_id if lease_record else None
            lease_token = lease_record.fencing_token if lease_record else None
            lease_until = lease_record.lease_until if lease_record else now
            await session.execute(
                update(WorkerJobModel)
                .where(
                    WorkerJobModel.job_id == job_id,
                    WorkerJobModel.status == "cancel_requested",
                )
                .values(
                    status="cancelled",
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
                    updated_at=now,
                )
            )
            await session.execute(
                update(AgentTaskModel)
                .where(
                    AgentTaskModel.task_id == task_id,
                    AgentTaskModel.status == "cancelling",
                )
                .values(status="cancelled", updated_at=now)
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
            session.add(
                self._task_cancelled_outbox(
                    run=run,
                    plan=plan,
                    round_id=round_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    execution_generation=attempt,
                    title=title,
                    objective=objective,
                    now=now,
                )
            )
            if lease_owner is not None and lease_token is not None:
                await self._leases.acknowledge_stopped_in_transaction(
                    session,
                    LeaseGrant(
                        key=LeaseKey("worker_execution", execution_id),
                        owner_id=lease_owner,
                        fencing_token=lease_token,
                        lease_until=lease_until,
                    ),
                    completed=True,
                )
            else:
                await self._leases.complete_unclaimed_in_transaction(
                    session, LeaseKey("worker_execution", execution_id)
                )

    async def _finalize_cancellation(
        self,
        session: AsyncSession,
        *,
        run: AgentRunModel,
        plan: AgentPlanModel,
        now: datetime,
    ) -> RunCancellationReceipt:
        """原子终结 Plan、Run 和剩余任务，并写入终态 Outbox。"""
        cancelling_tasks = (
            await session.execute(
                select(AgentTaskModel)
                .where(
                    AgentTaskModel.plan_id == plan.plan_id,
                    AgentTaskModel.status == "cancelling",
                )
                .order_by(AgentTaskModel.task_id)
                .with_for_update()
            )
        ).scalars().all()
        for task in cancelling_tasks:
            session.add(
                self._task_cancelled_outbox(
                    run=run,
                    plan=plan,
                    round_id=plan.active_round_id,
                    task_id=task.task_id,
                    execution_id=task.active_execution_id,
                    execution_generation=task.execution_generation,
                    title=task.title,
                    objective=task.objective,
                    now=now,
                )
            )
        await session.execute(
            update(AgentTaskModel)
            .where(
                AgentTaskModel.plan_id == plan.plan_id,
                AgentTaskModel.status == "cancelling",
            )
            .values(status="cancelled", updated_at=now)
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
        plan.updated_at = now
        failed = run.error_json is not None
        run.status = "failed" if failed else "cancelled"
        run.updated_at = now
        session_row = await session.get(SessionModel, run.session_id)
        if session_row is not None and session_row.active_run_id == run.run_id:
            session_row.active_run_id = None
        await self._enqueue_cancelled_run_memory_job(session, run=run, now=now)
        await session.execute(
            update(RootResumeJobModel)
            .where(
                RootResumeJobModel.run_id == run.run_id,
                RootResumeJobModel.status.in_(("pending", "running")),
            )
            .values(
                status="cancelled",
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
                    event_type="RunFailed" if failed else "RunCancelled",
                    dedupe_key=f"run:{run.run_id}:{run.status}:{run.cancellation_epoch}",
                    now=now,
                ),
            ]
        )
        return RunCancellationReceipt(
            run_id=run.run_id,
            session_id=run.session_id,
            plan_id=plan.plan_id,
            cancellation_epoch=run.cancellation_epoch,
            external_effect_status=run.cancellation_external_effect_status,
        )

    async def _enqueue_cancelled_run_memory_job(
        self,
        session: AsyncSession,
        *,
        run: AgentRunModel,
        now: datetime,
    ) -> None:
        """在取消终态事务内提交回顾作业和冻结的项目绑定。"""
        try:
            request = RunStartPayloadSchema.model_validate(run.request_json).to_domain()
        except (TypeError, ValueError):
            return
        user_text = request.message.strip()
        if not user_text:
            return
        await enqueue_memory_job_in_transaction(session, turn_id=run.run_id,
            session_id=run.session_id, user_text=user_text, assistant_text="", now=now)

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
                **({"error": run.error_json} if event_type == "RunFailed" and run.error_json else {}),
            },
            schema_version=1,
            status="pending",
            attempt=0,
            available_at=now,
            occurred_at=now,
        )

    def _task_cancelled_outbox(
        self,
        *,
        run: AgentRunModel,
        plan: AgentPlanModel,
        round_id: str | None,
        task_id: str,
        execution_id: str | None,
        execution_generation: int,
        title: str,
        objective: str,
        now: datetime,
    ) -> OrchestrationOutboxModel:
        """Construct a deduplicated Task cancellation event for this epoch."""
        return OrchestrationOutboxModel(
            message_id=uuid4().hex,
            dedupe_key=(
                f"task:{task_id}:cancelled:{run.cancellation_epoch}"
            ),
            event_type="TaskCancelled",
            correlation_id=run.run_id,
            plan_id=plan.plan_id,
            plan_version=plan.current_version,
            round_id=round_id,
            task_id=task_id,
            execution_id=execution_id,
            payload_json={
                "task_id": task_id,
                "execution_id": execution_id,
                "execution_generation": execution_generation,
                "title": title,
                "objective": objective,
                "status": "cancelled",
                "parent_activity_id": (
                    f"round:{round_id}" if round_id else f"plan:{plan.plan_id}"
                ),
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

    async def _candidate_rows(
        self, session: AsyncSession, now: datetime, claimable_ids: set[str]
    ):
        """读取未锁定候选 ID；实际领取仍按 Run 到 Job 顺序加锁复核。"""
        ready_job = and_(
            WorkerJobModel.status == "pending",
            WorkerJobModel.available_at <= now,
        )
        if not claimable_ids:
            return []
        statement = (
            select(
                WorkerJobModel.job_id,
                AgentRunModel.run_id,
                AgentRunModel.session_id,
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
                TaskExecutionModel.execution_id.in_(claimable_ids),
                WorkerJobModel.job_type.in_(("execute", "resume_worker")),
                or_(ready_job, WorkerJobModel.status == "running"),
            )
            .order_by(WorkerJobModel.available_at, WorkerJobModel.created_at)
            .limit(len(claimable_ids))
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
        resume_value = candidate.resume_value if candidate.resume_value is not None else {}
        if is_resume:
            approval = (
                await session.get(ApprovalRecordModel, candidate.approval_id)
                if candidate.approval_id
                else None
            )
            if (
                approval is None
                or approval.status not in {"resolved", "expired"}
                or approval.decision not in {"approved", "denied", "expired"}
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

        grant = await self._leases.acquire_in_transaction(
            session,
            LeaseKey("worker_execution", execution.execution_id),
            owner_id=uuid4().hex,
            lease_seconds=lease_seconds,
        )
        if grant is None:
            return None
        job.status = "running"
        if not is_reclaim and candidate.job_type == "execute":
            job.attempt += 1
        job.updated_at = now
        execution.status = "running"
        execution.updated_at = now
        task_row.status = "running"
        task_row.execution_generation = execution.attempt
        task_row.worker_thread_id = execution.worker_thread_id
        task_row.updated_at = now
        round_task.status = "running"

        task = _task_from_row(task_row)
        upstream = await self._load_upstream_results(
            session,
            plan_id=plan.plan_id,
            plan_version=round_row.plan_version,
            task_id=task_row.task_id,
        )
        lease = WorkerJobLease(
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
            lease=grant,
            cancellation_epoch=execution.cancellation_epoch,
            task=task,
            upstream_results=upstream,
            job_type=candidate.job_type,
            approval_id=candidate.approval_id,
            resume_value=resume_value if is_resume else {},
        )
        if not is_reclaim:
            if candidate.job_type == "execute":
                dedupe_key = f"execution:{execution.execution_id}:started"
            else:
                dedupe_key = (
                    f"execution:{execution.execution_id}:resumed:"
                    f"{candidate.approval_id}"
                )
            session.add(
                OrchestrationOutboxModel(
                    message_id=uuid4().hex,
                    dedupe_key=dedupe_key,
                    event_type="TaskStarted",
                    correlation_id=run.run_id,
                    plan_id=plan.plan_id,
                    plan_version=round_row.plan_version,
                    round_id=round_row.round_id,
                    task_id=task_row.task_id,
                    execution_id=execution.execution_id,
                    payload_json={
                        "task_id": task_row.task_id,
                        "execution_id": execution.execution_id,
                        "execution_generation": execution.attempt,
                        "attempt": execution.attempt,
                        "title": task.title,
                        "objective": task.objective,
                        "worker_type": task.worker_type,
                        "resumed": is_resume,
                        "started_at": now.isoformat(),
                        "parent_activity_id": f"round:{round_row.round_id}",
                    },
                    schema_version=1,
                    status="pending",
                    attempt=0,
                    available_at=now,
                    occurred_at=now,
                )
            )
        return lease

    async def _lock_execution_chain(
        self,
        session: AsyncSession,
        lease: WorkerJobLease | _Candidate,
        *,
        skip_locked: bool = False,
    ):
        """先锁 Session，再按 Run、Plan、Round、Task、Execution、Job 顺序加锁。"""
        session_row = await _locked_one(
            session,
            SessionModel,
            SessionModel.id == lease.session_id,
            skip_locked=skip_locked,
        )
        if session_row is None or session_row.active_run_id != lease.run_id:
            return (None, None, None, None, None, None, None)
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
        and execution.task_id == lease.task_id
        and execution.round_id == lease.round_id
        and job.execution_id == lease.execution_id
        and job.job_type == lease.job_type
        and execution.cancellation_epoch == lease.cancellation_epoch
        and run.cancellation_epoch == lease.cancellation_epoch
        and plan.cancellation_epoch == lease.cancellation_epoch
    )


def _lease_grant_from_record(row: LeaseRecord) -> LeaseGrant:
    """从当前中心记录恢复退出确认需要的 owner 和 fencing 代次。"""
    if row.owner_id is None or row.lease_until is None or row.fencing_token is None:
        raise ValueError("execution_lease_has_no_active_owner")
    return LeaseGrant(
        key=row.key,
        owner_id=row.owner_id,
        fencing_token=row.fencing_token,
        lease_until=row.lease_until,
    )


def _task_from_row(row: AgentTaskModel) -> Task:
    """将旧 Task 投影转换为 Worker 领域输入。"""
    return Task(
        task_id=row.task_id,
        title=row.title,
        objective=row.objective,
        input=row.input_json,
        expected_output=row.expected_output_json,
        allowed_tools=_load_json_string_list(row.allowed_tools_json),
        worker_type=row.worker_type,
        timeout_seconds=getattr(row, "timeout_seconds", 3600.0) or 3600.0,
    )


def _result_from_row(
    row: AgentTaskModel,
    resolved_output: dict[str, Any] | None = None,
) -> TaskResult:
    """恢复已完成上游的结果、证据和不确定性。"""
    raw = (
        resolved_output
        if resolved_output is not None
        else (row.output_json if row.output_json is not None else {})
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
    error = row.error_json
    return TaskResult(
        task_id=row.task_id,
        status=status,
        output=output,
        evidence=tuple(raw.get("evidence", [])),
        uncertainties=tuple(str(item) for item in raw.get("uncertainties", [])),
        error=error,
        retryable=bool(raw.get("retryable", False)),
    )


def _load_json_string_list(value: object) -> tuple[str, ...]:
    """读取 JSONB 字符串列表；元素类型不符时返回空元组。"""
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        return ()
    return tuple(value)


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
