"""PostgreSQL approval persistence implementation."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any
from uuid import uuid4

import rfc8785
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.approval import ApprovalDecision, ApprovalRequest, ApprovalStatus
from domain.approval.ports import ApprovalRepository
from domain.orchestration.worker_entities import WorkerTaskStatus

from ..models import (
    AgentPlanModel,
    AgentRunModel,
    ApprovalRecordModel,
    OrchestrationOutboxModel,
    WorkerJobModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    WorkerApprovalRequestModel,
    WorkerInstanceModel,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)

class PostgresApprovalRepository(ApprovalRepository):
    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        clock: Callable[[], datetime] | None = None,
        resume_dispatch_max_wait_seconds: float = 300.0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or _now
        if not 1 <= resume_dispatch_max_wait_seconds <= 86400:
            raise ValueError("worker_resume_dispatch_max_wait_out_of_range")
        self._resume_dispatch_max_wait_seconds = resume_dispatch_max_wait_seconds

    async def get(self, approval_id: str):
        async with self._session_factory() as session:
            row = await session.get(ApprovalRecordModel, approval_id)
        return None if row is None else self._domain(row)

    async def list_pending(self, session_id: str | None = None):
        return await self._list(session_id, pending=True)

    async def list_history(
        self, session_id: str | None = None, *, limit: int = 50, offset: int = 0
    ):
        return await self._list(session_id, pending=False, limit=limit, offset=offset)

    async def _list(self, session_id, *, pending, limit=100000, offset=0):
        async with self._session_factory() as session:
            query = (
                select(ApprovalRecordModel)
                .where(
                    ApprovalRecordModel.status == ApprovalStatus.PENDING.value
                    if pending
                    else ApprovalRecordModel.status.in_(
                        (
                            ApprovalStatus.RESOLVED.value,
                            ApprovalStatus.EXPIRED.value,
                            ApprovalStatus.CANCELLED.value,
                        )
                    )
                )
                .order_by(ApprovalRecordModel.created_at)
            )
            if session_id is not None:
                query = query.where(ApprovalRecordModel.session_id == session_id)
            rows = (
                (await session.execute(query.offset(offset).limit(limit)))
                .scalars()
                .all()
            )
        return [self._domain(row) for row in rows]

    async def resolve(
        self, approval_id: str, *, decision: ApprovalDecision
    ) -> bool:
        """从审批行读取归属，在同一事务写入决定、Outbox 和 Worker Resume Job。"""
        if decision not in {ApprovalDecision.APPROVED, ApprovalDecision.DENIED}:
            return False
        async with self._session_factory() as session:
            async with session.begin():
                # 先读取不可变归属，再按 Run、Plan、Approval 加锁，与取消事务保持同序以消除竞态。
                hint = await session.get(ApprovalRecordModel, approval_id)
                if hint is None:
                    return False
                if hint.approval_request_id is not None:
                    return await self._resolve_remote_worker_approval(
                        session, approval_id, decision, allow_expired=False
                    )
                run_id = hint.run_id
                run = await session.get(
                    AgentRunModel, run_id, with_for_update=True
                )
                plan = (
                    await session.get(
                        AgentPlanModel,
                        hint.plan_id,
                        with_for_update=True,
                    )
                    if hint.plan_id
                    else None
                )
                approval = await session.get(
                    ApprovalRecordModel,
                    approval_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                # 行锁串行化并发决定。对已成功写入的相同决定返回成功，
                # 使客户端在响应丢失后可以安全重试；相反决定仍按冲突处理。
                if (
                    approval is not None
                    and approval.run_id == run_id
                    and approval.plan_id == hint.plan_id
                    and approval.status == ApprovalStatus.RESOLVED.value
                ):
                    return approval.decision == decision.value
                now = await session.scalar(select(func.clock_timestamp()))
                if now is None:
                    raise RuntimeError("database_clock_unavailable")
                now = _as_utc(now)
                if (
                    run is None
                    or run.status != "running"
                    or (hint.plan_id is not None and plan is None)
                    or (
                        plan is not None
                        and (plan.run_id != run_id or plan.status != "running")
                    )
                    or approval is None
                    or approval.run_id != run_id
                    or approval.status != ApprovalStatus.PENDING.value
                    or approval.plan_id != hint.plan_id
                    or not approval.plan_id
                    or not approval.task_id
                    or not approval.execution_id
                    or not approval.worker_thread_id
                    or not approval.interrupt_id
                    or not approval.args_hash
                    or not approval.operation_key
                    or (
                        approval.expires_at is not None
                        and _as_utc(approval.expires_at) <= now
                    )
                ):
                    return False
                result = await session.execute(
                    update(ApprovalRecordModel)
                    .where(
                        ApprovalRecordModel.approval_id == approval_id,
                        ApprovalRecordModel.run_id == run_id,
                        ApprovalRecordModel.status == ApprovalStatus.PENDING.value,
                    )
                    .values(
                        status=ApprovalStatus.RESOLVED.value,
                        decision=decision.value,
                        decided_at=now,
                    )
                )
                if result.rowcount:
                    session.add(
                        OrchestrationOutboxModel(
                            message_id=uuid4().hex,
                            dedupe_key=f"approval:{approval_id}:resolved",
                            event_type="ApprovalResolved",
                            correlation_id=run_id,
                            plan_id=plan.plan_id if plan else approval.plan_id,
                            plan_version=plan.current_version if plan else None,
                            round_id=plan.active_round_id if plan else None,
                            payload_json={
                                "approval_id": approval_id,
                                "task_id": approval.task_id,
                                "execution_id": approval.execution_id,
                                "worker_thread_id": approval.worker_thread_id,
                                "interrupt_id": approval.interrupt_id,
                                "tool_call_id": approval.tool_call_id,
                                "tool_name": approval.tool_name,
                                "decision": decision.value,
                                "parent_activity_id": (
                                    f"task:{approval.task_id}"
                                    if approval.task_id
                                    else f"tool:{approval.tool_call_id}"
                                ),
                            },
                            schema_version=1,
                            status="pending",
                            attempt=0,
                            available_at=now,
                            occurred_at=now,
                        )
                    )
                if (
                    result.rowcount
                    and approval.execution_id
                    and approval.worker_thread_id
                    and approval.interrupt_id
                    and approval.args_hash
                    and approval.operation_key
                ):
                    session.add(
                        WorkerJobModel(
                            job_id=uuid4().hex,
                            dedupe_key=f"resume-worker:{approval_id}",
                            job_type="resume_worker",
                            execution_id=approval.execution_id,
                            approval_id=approval_id,
                            payload_json={
                                "approval_id": approval_id,
                                "decision": decision.value,
                                "interrupt_id": approval.interrupt_id,
                                "execution_id": approval.execution_id,
                                "args_hash": approval.args_hash,
                                "operation_key": approval.operation_key,
                            },
                            status="pending",
                            attempt=0,
                            available_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
        return bool(result.rowcount)

    async def _resolve_remote_worker_approval(
        self,
        session: AsyncSession,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        allow_expired: bool,
    ) -> bool:
        """原子记录远程审批决定，并在批次完成时创建唯一 ResumeCommand。

        远程审批不再依赖旧 WorkerJob/interrupt 字段。所有身份先由
        Execution 的真实父级推导，再按 Run→Plan→Task→Execution→Approval
        加锁；批量审批只有全部项目有最终决定时才会生成一条恢复命令。
        """
        if decision not in {
            ApprovalDecision.APPROVED,
            ApprovalDecision.DENIED,
            ApprovalDecision.EXPIRED,
        }:
            return False
        hint = await session.scalar(
            select(ApprovalRecordModel)
            .where(ApprovalRecordModel.approval_id == approval_id)
            .execution_options(populate_existing=True)
        )
        if hint is None or hint.execution_id is None or hint.approval_request_id is None:
            return False
        execution_hint = await session.scalar(
            select(OrchestratorExecutionModel)
            .where(OrchestratorExecutionModel.execution_id == hint.execution_id)
            .execution_options(populate_existing=True)
        )
        if execution_hint is None:
            return False
        task_hint = await session.scalar(
            select(OrchestratorTaskModel)
            .where(OrchestratorTaskModel.task_id == execution_hint.task_id)
            .execution_options(populate_existing=True)
        )
        plan_hint = None if task_hint is None else await session.scalar(
            select(OrchestratorPlanModel)
            .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
            .execution_options(populate_existing=True)
        )
        if task_hint is None or plan_hint is None:
            return False
        run = await session.scalar(
            select(AgentRunModel).where(AgentRunModel.run_id == plan_hint.run_id)
            .execution_options(populate_existing=True).with_for_update()
        )
        plan = await session.scalar(
            select(OrchestratorPlanModel).where(OrchestratorPlanModel.plan_id == plan_hint.plan_id)
            .execution_options(populate_existing=True).with_for_update()
        )
        task = await session.scalar(
            select(OrchestratorTaskModel).where(OrchestratorTaskModel.task_id == task_hint.task_id)
            .execution_options(populate_existing=True).with_for_update()
        )
        execution = await session.scalar(
            select(OrchestratorExecutionModel)
            .where(OrchestratorExecutionModel.execution_id == execution_hint.execution_id)
            .execution_options(populate_existing=True).with_for_update()
        )
        request = await session.scalar(
            select(WorkerApprovalRequestModel)
            .where(
                WorkerApprovalRequestModel.approval_request_id
                == hint.approval_request_id,
                WorkerApprovalRequestModel.instance_id == execution_hint.instance_id,
                WorkerApprovalRequestModel.execution_id == hint.execution_id,
                WorkerApprovalRequestModel.pause_id == hint.pause_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        approvals = (
            await session.scalars(
                select(ApprovalRecordModel)
                .where(
                    ApprovalRecordModel.approval_request_id
                    == hint.approval_request_id,
                    ApprovalRecordModel.execution_id == hint.execution_id,
                )
                .order_by(ApprovalRecordModel.item_id, ApprovalRecordModel.approval_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        approval = next((row for row in approvals if row.approval_id == approval_id), None)
        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise RuntimeError("database_clock_unavailable")
        now = _as_utc(now)
        if (
            run is None
            or plan is None
            or task is None
            or execution is None
            or request is None
            or approval is None
            or plan.run_id != run.run_id
            or task.plan_id != plan.plan_id
            or execution.task_id != task.task_id
            or request.run_id != run.run_id
            or request.plan_id != plan.plan_id
            or request.task_id != task.task_id
            or request.execution_id != execution.execution_id
            or approval.run_id != run.run_id
            or approval.plan_id != plan.plan_id
            or approval.task_id != task.task_id
            or approval.execution_id != execution.execution_id
            or approval.approval_request_id != request.approval_request_id
            or approval.pause_id != request.pause_id
            or approval.generation != request.generation
            or approval.cancellation_epoch != request.cancellation_epoch
        ):
            return False
        # 审批行及其不可变归属已加锁校验；后续 pause 不应改变已完成决定的重放结果。
        if approval.status != ApprovalStatus.PENDING.value:
            return approval.decision == decision.value
        # 只有仍待处理的审批必须属于 execution 当前 pause 和 fencing 代次。
        if (
            request.generation != execution.generation
            or request.cancellation_epoch != execution.cancellation_epoch
            or execution.current_approval_request_id != request.approval_request_id
            or execution.current_pause_id != request.pause_id
            or approval.generation != execution.generation
            or approval.cancellation_epoch != execution.cancellation_epoch
        ):
            return False
        if (
            run.status != "running"
            or plan.status != "running"
            or task.status != WorkerTaskStatus.WAITING_APPROVAL.value
            or task.active_execution_id != execution.execution_id
            or task.execution_generation != execution.generation
            or task.cancellation_epoch != execution.cancellation_epoch
            or execution.status != WorkerTaskStatus.WAITING_APPROVAL.value
            or execution.deadline_at is not None
        ):
            return False
        if approval.expires_at is not None and _as_utc(approval.expires_at) <= now:
            if not allow_expired:
                return False
            decision = ApprovalDecision.EXPIRED
        approval.status = (
            ApprovalStatus.EXPIRED.value
            if decision is ApprovalDecision.EXPIRED
            else ApprovalStatus.RESOLVED.value
        )
        approval.decision = decision.value
        approval.decided_at = now
        approval.decided_by = "worker_user" if decision is not ApprovalDecision.EXPIRED else "expiry_scanner"
        session.add(
            OrchestrationOutboxModel(
                message_id=uuid4().hex,
                dedupe_key=f"approval:{approval_id}:resolved",
                event_type="ApprovalResolved",
                correlation_id=run.run_id,
                plan_id=plan.plan_id,
                plan_version=plan.sequence,
                round_id=None,
                payload_json={
                    "approval_id": approval.approval_id,
                    "approval_request_id": approval.approval_request_id,
                    "pause_id": approval.pause_id,
                    "item_id": approval.item_id,
                    "task_id": task.task_id,
                    "execution_id": execution.execution_id,
                    "tool_name": approval.tool_name,
                    "decision": decision.value,
                },
                schema_version=1,
                status="pending",
                attempt=0,
                available_at=now,
                occurred_at=now,
            )
        )
        final = all(row.status != ApprovalStatus.PENDING.value for row in approvals)
        if not final:
            return True
        if request.resume_command_id:
            return True
        if run.status != "running" or plan.status != "running":
            return True
        if task.status != WorkerTaskStatus.WAITING_APPROVAL.value or execution.status != WorkerTaskStatus.WAITING_APPROVAL.value:
            return True
        if execution.instance_id is None or execution.deadline_at is not None:
            return True
        instance = await session.get(WorkerInstanceModel, execution.instance_id, with_for_update=True)
        if instance is None:
            return True
        decisions = [
            {
                "item_id": str(row.item_id),
                "decision": str(row.decision),
                "arguments_sha256": str(row.arguments_sha256 or row.args_hash),
                "decided_at": _as_utc(row.decided_at or now)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            for row in approvals
        ]
        decision_set_sha256 = hashlib.sha256(rfc8785.dumps(decisions)).hexdigest()
        command_id = str(uuid4())
        batch_id = request.receipt_json.get("approval_batch_id")
        command_body = {
            "protocol_version": "1.0",
            "command_id": command_id,
            "assignment_required": True,
            "execution_id": execution.execution_id,
            "generation": execution.generation,
            "cancellation_epoch": execution.cancellation_epoch,
            "pause_id": request.pause_id,
            "approval_request_id": request.approval_request_id,
            "approval_id": str(approvals[0].approval_id) if len(approvals) == 1 else None,
            "approval_batch_id": batch_id if len(approvals) > 1 else None,
            "decisions": decisions,
            "decision_set_sha256": decision_set_sha256,
        }
        command = OrchestratorWorkerCommandModel(
            command_id=command_id,
            execution_id=execution.execution_id,
            instance_id=execution.instance_id,
            generation=execution.generation,
            idempotency_key=f"resume:{request.approval_request_id}",
            command_type="resume",
            request_hash=hashlib.sha256(rfc8785.dumps(command_body)).hexdigest(),
            command_json=command_body,
            status="pending",
            attempt=0,
            send_attempt_count=0,
            delivery_uncertain_before_send=False,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            available_at=now,
            receipt_json=None,
            created_at=now,
            updated_at=now,
        )
        session.add(command)
        request.resume_command_id = command_id
        request.status = "resolved"
        request.resume_wait_deadline_at = now + timedelta(
            seconds=self._resume_dispatch_max_wait_seconds
        )
        return True

    async def expire_due_approvals(self, *, limit: int = 64) -> int:
        """将到期审批标为 EXPIRED，并原子写入通知和 Worker 恢复作业。"""
        if limit < 1:
            raise ValueError("limit must be positive")
        expired_count = 0
        async with self._session_factory() as session:
            async with session.begin():
                now = await session.scalar(select(func.clock_timestamp()))
                if now is None:
                    raise RuntimeError("database_clock_unavailable")
                now = _as_utc(now)
                candidates = (
                    await session.execute(
                        select(
                            ApprovalRecordModel.approval_id,
                            ApprovalRecordModel.run_id,
                            ApprovalRecordModel.plan_id,
                            ApprovalRecordModel.approval_request_id,
                        )
                        .where(
                            ApprovalRecordModel.status
                            == ApprovalStatus.PENDING.value,
                            ApprovalRecordModel.execution_id.is_not(None),
                            ApprovalRecordModel.plan_id.is_not(None),
                            ApprovalRecordModel.task_id.is_not(None),
                            ApprovalRecordModel.expires_at.is_not(None),
                            ApprovalRecordModel.expires_at <= now,
                        )
                        .order_by(
                            ApprovalRecordModel.expires_at,
                            ApprovalRecordModel.created_at,
                        )
                        .limit(limit)
                    )
                ).all()
                for approval_id, run_id, plan_id, approval_request_id in candidates:
                    if approval_request_id is not None:
                        if await self._resolve_remote_worker_approval(
                            session, approval_id, ApprovalDecision.EXPIRED, allow_expired=True
                        ):
                            expired_count += 1
                        continue
                    # 与取消和用户决策使用同一 Run → Plan → Approval 锁顺序，
                    # 让到期拒绝与批准/取消只会有一个事务获胜。
                    run = await session.get(
                        AgentRunModel, run_id, with_for_update=True
                    )
                    plan = (
                        await session.get(
                            AgentPlanModel, plan_id, with_for_update=True
                        )
                        if plan_id
                        else None
                    )
                    approval = await session.get(
                        ApprovalRecordModel,
                        approval_id,
                        with_for_update=True,
                    )
                    if (
                        approval is None
                        or approval.run_id != run_id
                        or approval.plan_id != plan_id
                        or approval.status != ApprovalStatus.PENDING.value
                        or approval.expires_at is None
                        or _as_utc(approval.expires_at) > now
                    ):
                        continue

                    approval.status = ApprovalStatus.EXPIRED.value
                    approval.decision = ApprovalDecision.EXPIRED.value
                    approval.decided_at = now
                    active_plan = (
                        run is not None
                        and run.status == "running"
                        and plan is not None
                        and plan.run_id == run_id
                        and plan.status == "running"
                    )
                    session.add(
                        OrchestrationOutboxModel(
                            message_id=uuid4().hex,
                            dedupe_key=f"approval:{approval_id}:resolved",
                            event_type="ApprovalResolved",
                            correlation_id=run_id,
                            plan_id=plan_id,
                            plan_version=(plan.current_version if plan else None),
                            round_id=(plan.active_round_id if plan else None),
                            payload_json={
                                "approval_id": approval_id,
                                "task_id": approval.task_id,
                                "execution_id": approval.execution_id,
                                "worker_thread_id": approval.worker_thread_id,
                                "interrupt_id": approval.interrupt_id,
                                "tool_call_id": approval.tool_call_id,
                                "tool_name": approval.tool_name,
                                "decision": ApprovalDecision.EXPIRED.value,
                                "expires_at": _as_utc(approval.expires_at).isoformat(),
                                "parent_activity_id": (
                                    f"task:{approval.task_id}"
                                    if approval.task_id
                                    else f"tool:{approval.tool_call_id}"
                                ),
                            },
                            schema_version=1,
                            status="pending",
                            attempt=0,
                            available_at=now,
                            occurred_at=now,
                        )
                    )
                    if (
                        active_plan
                        and approval.execution_id
                        and approval.worker_thread_id
                        and approval.interrupt_id
                        and approval.args_hash
                        and approval.operation_key
                    ):
                        session.add(
                            WorkerJobModel(
                                job_id=uuid4().hex,
                                dedupe_key=f"resume-worker:{approval_id}",
                                job_type="resume_worker",
                                execution_id=approval.execution_id,
                                approval_id=approval_id,
                                payload_json={
                                    "approval_id": approval_id,
                                    "decision": ApprovalDecision.EXPIRED.value,
                                    "interrupt_id": approval.interrupt_id,
                                    "execution_id": approval.execution_id,
                                    "args_hash": approval.args_hash,
                                    "operation_key": approval.operation_key,
                                },
                                status="pending",
                                attempt=0,
                                available_at=now,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    expired_count += 1
        return expired_count

    @staticmethod
    def _domain(row):
        return ApprovalRequest(
            row.approval_id,
            row.session_id,
            row.run_id,
            row.tool_call_id,
            row.tool_name,
            row.arguments_json,
            row.risk_level,
            row.task_id,
            row.plan_id,
            row.approval_batch_id,
            ApprovalStatus(row.status),
            ApprovalDecision(row.decision) if row.decision else None,
            row.created_at,
            row.decided_at,
            getattr(row, "execution_id", None),
            getattr(row, "worker_thread_id", None),
            getattr(row, "interrupt_id", None),
            getattr(row, "args_hash", None),
            getattr(row, "operation_key", None),
            getattr(row, "expires_at", None),
            getattr(row, "decided_by", None),
            getattr(row, "approval_request_id", None),
            getattr(row, "pause_id", None),
            getattr(row, "item_id", None),
            getattr(row, "generation", None),
            getattr(row, "cancellation_epoch", None),
            getattr(row, "arguments_sha256", None),
        )

    def _now(self) -> datetime:
        """返回当前 UTC 时间，统一注入时钟和数据库时间边界。"""
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """把审批过期时间标准化为可安全比较的 UTC 时间。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

__all__ = ["PostgresApprovalRepository"]
