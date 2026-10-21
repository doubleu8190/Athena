"""PostgreSQL approval persistence implementation."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import DateTime, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from domain.approval import ApprovalDecision, ApprovalRequest, ApprovalStatus
from domain.approval.ports import ApprovalRepository

from ..models import (
    ApprovalRecordModel,
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
                        status=request.status.value,
                        decision=request.decision.value if request.decision else None,
                        created_at=(
                            request.created_at or datetime.now(timezone.utc)
                        ).isoformat(),
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
        async with self._session_factory() as session:
            async with session.begin():
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
        )

__all__ = ["PostgresApprovalRepository"]
