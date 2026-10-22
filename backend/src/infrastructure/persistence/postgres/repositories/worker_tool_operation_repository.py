"""PostgreSQL idempotency ledger for Worker tool side effects."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.tools import (
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolOperationClaim,
    ToolOperationClaimStatus,
    WorkerToolOperationLedger,
)

from ..models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    PlanRoundModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerToolOperationModel,
)


class PostgresWorkerToolOperationRepository(WorkerToolOperationLedger):
    """Claim one logical operation once and replay its persisted outcome."""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def claim(
        self, request: ToolExecutionRequest, *, args_hash: str
    ) -> ToolOperationClaim:
        """Reserve an operation only for the currently fenced Worker lease.

        A row left in ``executing`` means the prior process may have crossed
        the external side-effect boundary before it crashed. Such operations
        are deliberately not retried automatically.
        """
        if (
            not request.operation_key
            or not request.logical_operation_key
            or not request.plan_id
            or not request.task_id
            or not request.execution_id
            or request.fencing_token is None
            or request.cancellation_epoch is None
        ):
            return ToolOperationClaim(
                ToolOperationClaimStatus.REJECTED,
                reason="worker_operation_identity_missing",
            )

        async with self._session_factory() as session:
            async with session.begin():
                # The logical key survives retries that get a new execution id.
                # Its lock serializes first claims across those attempts.
                # row locks below serialize the side-effect gate with cancel.
                await session.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtext(request.logical_operation_key)
                        )
                    )
                )
                run = await session.get(
                    AgentRunModel, request.run_id, with_for_update=True
                )
                plan = await session.get(
                    AgentPlanModel, request.plan_id, with_for_update=True
                )
                if run is None or plan is None:
                    return ToolOperationClaim(
                        ToolOperationClaimStatus.REJECTED,
                        reason="worker_operation_plan_missing",
                    )
                round_row = (
                    await session.get(
                        PlanRoundModel,
                        plan.active_round_id,
                        with_for_update=True,
                    )
                    if plan.active_round_id
                    else None
                )
                task = await session.get(
                    AgentTaskModel, request.task_id, with_for_update=True
                )
                round_task = (
                    await session.get(
                        RoundTaskModel,
                        (plan.active_round_id, request.task_id),
                        with_for_update=True,
                    )
                    if plan.active_round_id
                    else None
                )
                execution = await session.get(
                    TaskExecutionModel,
                    request.execution_id,
                    with_for_update=True,
                )
                now = _utc(self._clock())
                if (
                    round_row is None
                    or task is None
                    or round_task is None
                    or execution is None
                    or run.status != "running"
                    or plan.status != "running"
                    or plan.run_id != run.run_id
                    or run.session_id != request.session_id
                    or plan.active_round_id != execution.round_id
                    or plan.current_version != round_row.plan_version
                    or round_row.status != "running"
                    or task.run_id != run.run_id
                    or task.plan_id != plan.plan_id
                    or task.active_execution_id != execution.execution_id
                    or round_task.active_execution_id != execution.execution_id
                    or task.status != "running"
                    or round_task.status != "running"
                    or execution.task_id != task.task_id
                    or execution.status != "running"
                    or execution.fencing_token != request.fencing_token
                    or execution.cancellation_epoch != request.cancellation_epoch
                    or run.cancellation_epoch != request.cancellation_epoch
                    or plan.cancellation_epoch != request.cancellation_epoch
                    or execution.lease_until is None
                    or _utc(execution.lease_until) <= now
                ):
                    return ToolOperationClaim(
                        ToolOperationClaimStatus.REJECTED,
                        reason="worker_operation_lease_not_active",
                    )

                existing_attempt = await session.get(
                    WorkerToolOperationModel,
                    request.operation_key,
                    with_for_update=True,
                )
                if (
                    existing_attempt is not None
                    and existing_attempt.logical_operation_key
                    != request.logical_operation_key
                ):
                    return ToolOperationClaim(
                        ToolOperationClaimStatus.CONFLICT,
                        reason="worker_operation_key_conflict",
                    )

                operations = (
                    await session.scalars(
                        select(WorkerToolOperationModel)
                        .where(
                            WorkerToolOperationModel.logical_operation_key
                            == request.logical_operation_key
                        )
                        .order_by(WorkerToolOperationModel.created_at)
                        .with_for_update()
                    )
                ).all()
                if operations:
                    if any(
                        not _matches_logical_operation(
                            operation, request, args_hash
                        )
                        for operation in operations
                    ):
                        return ToolOperationClaim(
                            ToolOperationClaimStatus.CONFLICT,
                            reason="worker_logical_operation_conflict",
                        )
                    if any(operation.status != "completed" for operation in operations):
                        return ToolOperationClaim(
                            ToolOperationClaimStatus.IN_PROGRESS,
                            reason="worker_operation_outcome_unknown",
                        )
                    results = {
                        _result_from_document(operation.result_json)
                        for operation in operations
                    }
                    if len(results) != 1:
                        return ToolOperationClaim(
                            ToolOperationClaimStatus.CONFLICT,
                            reason="worker_logical_operation_result_conflict",
                        )
                    if operations[0].status == "completed":
                        return ToolOperationClaim(
                            ToolOperationClaimStatus.COMPLETED,
                            result=_result_from_document(operations[0].result_json),
                        )

                session.add(
                    WorkerToolOperationModel(
                        operation_key=request.operation_key,
                        logical_operation_key=request.logical_operation_key,
                        run_id=request.run_id,
                        plan_id=request.plan_id,
                        task_id=request.task_id,
                        execution_id=request.execution_id,
                        tool_name=request.tool_name,
                        tool_call_id=request.tool_call_id,
                        args_hash=args_hash,
                        status="executing",
                        result_json=None,
                        created_at=now,
                        completed_at=None,
                    )
                )
                return ToolOperationClaim(ToolOperationClaimStatus.ACQUIRED)

    async def complete(
        self,
        request: ToolExecutionRequest,
        *,
        args_hash: str,
        result: ToolExecutionResult,
    ) -> bool:
        """Persist the first result; an executing row remains fail-closed."""
        if not request.operation_key:
            return False
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(
                    WorkerToolOperationModel,
                    request.operation_key,
                    with_for_update=True,
                )
                if row is None or not _matches(row, request, args_hash):
                    return False
                if row.status == "completed":
                    return row.result_json == _result_document(result)
                if row.status != "executing":
                    return False
                row.status = "completed"
                row.result_json = _result_document(result)
                row.completed_at = _utc(self._clock())
                return True


def _matches(
    row: WorkerToolOperationModel,
    request: ToolExecutionRequest,
    args_hash: str,
) -> bool:
    return (
        row.run_id == request.run_id
        and row.plan_id == request.plan_id
        and row.task_id == request.task_id
        and row.logical_operation_key == request.logical_operation_key
        and row.execution_id == request.execution_id
        and row.tool_name == request.tool_name
        and row.tool_call_id == request.tool_call_id
        and row.args_hash == args_hash
    )


def _matches_logical_operation(
    row: WorkerToolOperationModel,
    request: ToolExecutionRequest,
    args_hash: str,
) -> bool:
    """Match effect semantics while allowing a new execution attempt."""
    return (
        row.logical_operation_key == request.logical_operation_key
        and row.run_id == request.run_id
        and row.plan_id == request.plan_id
        and row.task_id == request.task_id
        and row.tool_name == request.tool_name
        and row.args_hash == args_hash
    )


def _result_document(result: ToolExecutionResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "output": result.output,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


def _result_from_document(value: Any) -> ToolExecutionResult:
    if not isinstance(value, dict):
        raise RuntimeError("worker_tool_operation_result_missing")
    try:
        return ToolExecutionResult(
            status=str(value["status"]),
            output=value.get("output"),
            error=value.get("error"),
            duration_ms=float(value.get("duration_ms", 0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("worker_tool_operation_result_invalid") from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["PostgresWorkerToolOperationRepository"]
