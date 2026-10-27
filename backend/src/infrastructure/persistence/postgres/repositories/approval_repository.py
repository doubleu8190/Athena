"""PostgreSQL approval persistence implementation."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.approval import ApprovalDecision, ApprovalRequest, ApprovalStatus
from domain.approval.ports import ApprovalRepository

from ..models import (
    AgentPlanModel,
    AgentRunModel,
    ApprovalRecordModel,
    OrchestrationOutboxModel,
    WorkerJobModel,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)

def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return parsed

class PostgresApprovalRepository(ApprovalRepository):
    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or _now

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
                )
                now = self._now()
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
                        decided_at=now.isoformat(),
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

    async def expire_due_approvals(self, *, limit: int = 64) -> int:
        """将到期审批标为 EXPIRED，并原子写入通知和 Worker 恢复作业。"""
        if limit < 1:
            raise ValueError("limit must be positive")
        expired_count = 0
        async with self._session_factory() as session:
            async with session.begin():
                now = self._now()
                candidates = (
                    await session.execute(
                        select(
                            ApprovalRecordModel.approval_id,
                            ApprovalRecordModel.run_id,
                            ApprovalRecordModel.plan_id,
                        )
                        .where(
                            ApprovalRecordModel.status
                            == ApprovalStatus.PENDING.value,
                            ApprovalRecordModel.execution_id.is_not(None),
                            ApprovalRecordModel.worker_thread_id.is_not(None),
                            ApprovalRecordModel.interrupt_id.is_not(None),
                            ApprovalRecordModel.plan_id.is_not(None),
                            ApprovalRecordModel.task_id.is_not(None),
                            ApprovalRecordModel.args_hash.is_not(None),
                            ApprovalRecordModel.operation_key.is_not(None),
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
                for approval_id, run_id, plan_id in candidates:
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
                    approval.decided_at = now.isoformat()
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
            _json(row.arguments_json, {}),
            row.risk_level,
            row.task_id,
            row.plan_id,
            row.approval_batch_id,
            ApprovalStatus(row.status),
            ApprovalDecision(row.decision) if row.decision else None,
            datetime.fromisoformat(row.created_at),
            datetime.fromisoformat(row.decided_at) if row.decided_at else None,
            getattr(row, "execution_id", None),
            getattr(row, "worker_thread_id", None),
            getattr(row, "interrupt_id", None),
            getattr(row, "args_hash", None),
            getattr(row, "operation_key", None),
            getattr(row, "expires_at", None),
            getattr(row, "decided_by", None),
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
