"""PostgreSQL approval persistence implementation."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from sqlalchemy import DateTime, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from domain.approval import ApprovalDecision, ApprovalRequest, ApprovalStatus
from domain.approval.ports import ApprovalRepository

from ..models import (
    AgentPlanModel,
    AgentRunModel,
    ApprovalRecordModel,
    WorkerJobModel,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

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
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def create(self, request: ApprovalRequest) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                if await session.get(ApprovalRecordModel, request.approval_id):
                    return False
                session.add(
                    ApprovalRecordModel(
                        approval_id=request.approval_id,
                        session_id=request.session_id,
                        run_id=request.run_id,
                        approval_batch_id=request.approval_batch_id,
                        plan_id=request.plan_id,
                        task_id=request.task_id,
                        tool_call_id=request.tool_call_id,
                        tool_name=request.tool_name,
                        arguments_json=json.dumps(request.arguments),
                        risk_level=request.risk_level,
                        execution_id=request.execution_id,
                        worker_thread_id=request.worker_thread_id,
                        interrupt_id=request.interrupt_id,
                        args_hash=request.args_hash,
                        operation_key=request.operation_key,
                        status=request.status.value,
                        decision=request.decision.value if request.decision else None,
                        created_at=(
                            request.created_at or datetime.now(timezone.utc)
                        ).isoformat(),
                        expires_at=request.expires_at,
                        decided_by=request.decided_by,
                    )
                )
        return True

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
                    ApprovalRecordModel.status
                    == (
                        ApprovalStatus.PENDING.value
                        if pending
                        else ApprovalStatus.RESOLVED.value
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
        self, approval_id: str, *, run_id: str, task_id: str, decision: ApprovalDecision
    ) -> bool:
        """仅在关联 Run 与 Plan 仍活跃时记录决策并创建 Worker Resume Job。"""
        async with self._session_factory() as session:
            async with session.begin():
                # 先读取不可变归属，再按 Run、Plan、Approval 加锁，与取消事务保持同序以消除竞态。
                hint = await session.get(ApprovalRecordModel, approval_id)
                if hint is None or hint.run_id != run_id:
                    return False
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
                if (
                    run is None
                    or run.status not in {"created", "running"}
                    or (hint.plan_id is not None and plan is None)
                    or (
                        plan is not None
                        and (plan.run_id != run_id or plan.status != "running")
                    )
                    or approval is None
                    or approval.run_id != run_id
                    or approval.status != ApprovalStatus.PENDING.value
                    or approval.plan_id != hint.plan_id
                    or (
                        approval.task_id is not None
                        and approval.task_id != task_id
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
                        decided_at=_now(),
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
                    now = datetime.now(timezone.utc)
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

    async def resolve_batch(
        self,
        batch_id: str,
        decisions: dict[str, ApprovalDecision],
        *,
        run_id: str | None = None,
    ) -> bool:
        changed = False
        for approval_id, decision in decisions.items():
            row = await self.get(approval_id)
            if (
                row
                and (row.approval_batch_id == batch_id or (row.approval_batch_id is None and row.approval_id == batch_id))
                and (run_id is None or row.run_id == run_id)
            ):
                changed = (
                    await self.resolve(
                        approval_id,
                        run_id=row.run_id,
                        task_id=row.task_id or "",
                        decision=decision,
                    )
                    or changed
                )
        return changed

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

__all__ = ["PostgresApprovalRepository"]
