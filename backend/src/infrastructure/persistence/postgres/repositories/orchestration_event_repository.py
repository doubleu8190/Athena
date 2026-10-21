"""原子应用 Worker 终态事件并推进 Round 屏障。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.orchestration import TaskEventReceipt, WorkerTaskTerminalEvent
from ..models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    OrchestrationInboxModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    RoundTaskModel,
    TaskExecutionModel,
)


_CONSUMER_NAME = "round-task-terminal-v1"


class TaskEventConflict(ValueError):
    """相同事件 ID 被不同事件内容复用。"""


class PostgresOrchestrationEventRepository:
    """在单个 PostgreSQL 事务中验证执行栅栏并推进 Round。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存数据库会话工厂和可替换的时间、ID 来源。"""
        self._session_factory = session_factory
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def apply_worker_terminal_event(
        self, event: WorkerTaskTerminalEvent
    ) -> TaskEventReceipt:
        """应用一次 Worker 完成/失败事件，重复事件只返回原回执。"""
        payload = _event_document(event)
        payload_hash = _hash(payload)
        now = self._clock()

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    insert(OrchestrationInboxModel)
                    .values(
                        consumer_name=_CONSUMER_NAME,
                        message_id=event.message_id,
                        event_type="WorkerTaskTerminal",
                        payload_json=payload,
                        payload_hash=payload_hash,
                        status="received",
                        attempt=1,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["consumer_name", "message_id"]
                    )
                )
                inbox = (
                    await session.execute(
                        select(OrchestrationInboxModel)
                        .where(
                            OrchestrationInboxModel.consumer_name == _CONSUMER_NAME,
                            OrchestrationInboxModel.message_id == event.message_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one()
                if inbox.payload_hash != payload_hash:
                    raise TaskEventConflict(
                        "message_id_reused_with_different_payload"
                    )
                if inbox.status == "processed":
                    return _receipt(inbox.receipt_json, deduplicated=True)

                # 按 Run -> Plan -> Round -> Task -> Execution 的顺序加锁。
                run = await _locked_one(
                    session,
                    AgentRunModel,
                    AgentRunModel.run_id == event.run_id,
                )
                plan = await _locked_one(
                    session,
                    AgentPlanModel,
                    AgentPlanModel.plan_id == event.plan_id,
                )
                round_row = await _locked_one(
                    session,
                    PlanRoundModel,
                    PlanRoundModel.round_id == event.round_id,
                )
                task = await _locked_one(
                    session,
                    AgentTaskModel,
                    AgentTaskModel.task_id == event.task_id,
                )
                execution = await _locked_one(
                    session,
                    RoundTaskModel,
                    (RoundTaskModel.round_id == event.round_id)
                    & (RoundTaskModel.task_id == event.task_id),
                )
                execution = await _locked_one(
                    session,
                    TaskExecutionModel,
                    TaskExecutionModel.execution_id == event.execution_id,
                )

                rejection = _rejection_reason(
                    event, run, plan, round_row, task, execution, round_task
                )
                if rejection is not None:
                    receipt = TaskEventReceipt(
                        message_id=event.message_id,
                        accepted=False,
                        reason=rejection,
                        terminal_task_count=(
                            round_row.terminal_task_count
                            if round_row is not None
                            else None
                        ),
                        expected_task_count=(
                            round_row.expected_task_count
                            if round_row is not None
                            else None
                        ),
                    )
                    _mark_processed(inbox, receipt, now)
                    return receipt

                assert task is not None
                assert execution is not None
                assert round_task is not None
                assert round_row is not None
                assert plan is not None

                status = event.status.value
                result_document = {
                    "status": status,
                    "output": dict(event.result),
                    "error": dict(event.error) if event.error is not None else None,
                    "retryable": event.retryable,
                    "evidence": list(event.result.get("evidence", ())),
                    "uncertainties": list(event.result.get("uncertainties", ())),
                    "metrics": dict(event.result.get("metrics", {})),
                }
                execution.status = status
                execution.result_json = result_document
                execution.error_json = (
                    dict(event.error) if event.error is not None else None
                )
                execution.lease_owner = None
                execution.lease_until = None
                execution.updated_at = now

                task.status = status
                task.output_json = _strict_json(dict(event.result))
                task.error_json = (
                    _strict_json(dict(event.error))
                    if event.error is not None
                    else None
                )
                task.updated_at = now.isoformat()
                round_task.status = status
                round_task.terminal_at = now

                round_row.terminal_task_count += 1
                completed = (
                    round_row.terminal_task_count == round_row.expected_task_count
                )
                if completed:
                    round_row.status = "completed"
                    round_row.completed_at = now
                    plan.status = "waiting_review"
                    self._write_round_completed_outbox(
                        session, event, round_row, now
                    )

                receipt = TaskEventReceipt(
                    message_id=event.message_id,
                    accepted=True,
                    round_completed=completed,
                    terminal_task_count=round_row.terminal_task_count,
                    expected_task_count=round_row.expected_task_count,
                )
                _mark_processed(inbox, receipt, now)
                return receipt

    def _write_round_completed_outbox(
        self,
        session: AsyncSession,
        event: WorkerTaskTerminalEvent,
        round_row: PlanRoundModel,
        now: datetime,
    ) -> None:
        """把 Review 唤醒事件和最后一个终态结果放进同一事务。"""
        session.add(
            OrchestrationOutboxModel(
                message_id=self._id_factory(),
                dedupe_key=f"round:{round_row.round_id}:completed",
                event_type="RoundCompleted",
                correlation_id=event.run_id,
                plan_id=event.plan_id,
                plan_version=round_row.plan_version,
                round_id=round_row.round_id,
                payload_json={
                    "terminal_task_count": round_row.terminal_task_count,
                    "expected_task_count": round_row.expected_task_count,
                },
                schema_version=1,
                status="pending",
                attempt=0,
                available_at=now,
                occurred_at=now,
            )
        )


async def _locked_one(session: AsyncSession, model: type, predicate: Any):
    """按调用方既定锁顺序获取单条记录。"""
    return (
        await session.execute(
            select(model).where(predicate).with_for_update()
        )
    ).scalar_one_or_none()


def _rejection_reason(event, run, plan, round_row, task, execution, round_task):
    """验证身份、状态和执行栅栏，返回可持久化的拒绝原因。"""
    if any(item is None for item in (run, plan, round_row, task, execution, round_task)):
        return "orchestration_record_not_found"
    if (
        plan.run_id != event.run_id
        or task.plan_id != event.plan_id
        or run.session_id != plan.session_id
    ):
        return "ownership_mismatch"
    if round_row.plan_id != event.plan_id or round_row.plan_version != event.plan_version:
        return "round_version_mismatch"
    if execution.task_id != event.task_id or execution.round_id != event.round_id:
        return "execution_membership_mismatch"
    if round_task.active_execution_id != event.execution_id:
        return "stale_execution"
    if execution.status != "running" or task.status != "running":
        return "execution_not_running"
    if execution.fencing_token != event.fencing_token:
        return "stale_fencing_token"
    if any(
        epoch != event.cancellation_epoch
        for epoch in (
            run.cancellation_epoch,
            plan.cancellation_epoch,
            execution.cancellation_epoch,
        )
    ):
        return "stale_cancellation_epoch"
    if run.status != "running" or plan.status != "running":
        return "plan_or_run_not_running"
    if round_row.status != "running":
        return "round_not_running"
    if round_task.status in {"completed", "failed", "timed_out", "cancelled"}:
        return "task_already_terminal"
    return None


def _event_document(event: WorkerTaskTerminalEvent) -> dict[str, Any]:
    """生成事件信封的确定性 JSON 表示。"""
    return {
        "message_id": event.message_id,
        "occurred_at": event.occurred_at.isoformat(),
        "run_id": event.run_id,
        "plan_id": event.plan_id,
        "plan_version": event.plan_version,
        "round_id": event.round_id,
        "task_id": event.task_id,
        "execution_id": event.execution_id,
        "fencing_token": event.fencing_token,
        "cancellation_epoch": event.cancellation_epoch,
        "status": event.status.value,
        "result": dict(event.result),
        "error": dict(event.error) if event.error is not None else None,
        "retryable": event.retryable,
    }


def _strict_json(value: Any) -> str:
    """只编码标准 JSON，拒绝隐式字符串化的未知对象。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    """计算规范 JSON 的 SHA-256 指纹。"""
    return hashlib.sha256(_strict_json(value).encode("utf-8")).hexdigest()


def _mark_processed(inbox, receipt: TaskEventReceipt, now: datetime) -> None:
    """持久化事件的固定业务结果。"""
    inbox.status = "processed"
    inbox.processed_at = now
    inbox.receipt_json = {
        "message_id": receipt.message_id,
        "accepted": receipt.accepted,
        "reason": receipt.reason,
        "round_completed": receipt.round_completed,
        "terminal_task_count": receipt.terminal_task_count,
        "expected_task_count": receipt.expected_task_count,
    }


def _receipt(value: dict[str, Any] | None, *, deduplicated: bool) -> TaskEventReceipt:
    """从 Inbox 中恢复稳定回执。"""
    if value is None:
        raise RuntimeError("processed orchestration inbox row has no receipt")
    return TaskEventReceipt(
        message_id=str(value["message_id"]),
        accepted=bool(value["accepted"]),
        reason=value.get("reason"),
        deduplicated=deduplicated,
        round_completed=bool(value.get("round_completed", False)),
        terminal_task_count=(
            int(value["terminal_task_count"])
            if value.get("terminal_task_count") is not None
            else None
        ),
        expected_task_count=(
            int(value["expected_task_count"])
            if value.get("expected_task_count") is not None
            else None
        ),
    )


__all__ = ["PostgresOrchestrationEventRepository", "TaskEventConflict"]
