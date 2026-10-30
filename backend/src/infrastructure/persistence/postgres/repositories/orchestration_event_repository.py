"""原子应用 Worker 终态事件并推进 Round 屏障。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.leases import LeaseLostError
from domain.orchestration import TaskEventReceipt, WorkerTaskTerminalEvent, TaskEventRepository
from ..models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    OrchestrationInboxModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    RootResumeJobModel,
    RoundTaskModel,
    TaskExecutionModel,
    TaskExecutionResultModel,
    WorkerJobModel,
)
from .lease_center import PostgresLeaseCenter
from .task_execution_results import strict_result_json


_CONSUMER_NAME = "round-task-terminal-v1"
_INLINE_RESULT_LIMIT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class _TerminalEventRows:
    """按固定加锁顺序读取的 Worker 终态相关数据库行。"""

    run: AgentRunModel | None
    plan: AgentPlanModel | None
    round: PlanRoundModel | None
    task: AgentTaskModel | None
    round_task: RoundTaskModel | None
    execution: TaskExecutionModel | None
    job: WorkerJobModel | None


class TaskEventConflict(ValueError):
    """相同事件 ID 被不同事件内容复用。"""


class PostgresOrchestrationEventRepository(TaskEventRepository):
    """在单个 PostgreSQL 事务中验证执行栅栏并推进 Round。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
        *,
        max_attempts: int = 3,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        """保存数据库会话工厂和可替换的时间、ID 来源。"""
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._session_factory = session_factory
        self._max_attempts = max_attempts
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._clock = clock
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def apply_worker_terminal_event(
        self, event: WorkerTaskTerminalEvent
    ) -> TaskEventReceipt:
        """在一个事务中校验 Worker 终态并推进任务与 Round 状态。

        Args:
            event: Worker 执行结束后提交的带 fencing 身份事件。

        Returns:
            事件受理、拒绝或重复投递的稳定回执。

        Raises:
            TaskEventConflict: 同一消息 ID 被不同事件内容复用时。
            Exception: 数据库错误由事件消费者按重试策略处理。
        """
        payload = _event_document(event)
        payload_hash = _hash(payload)
        result_json = strict_result_json(event.result)
        result_bytes = result_json.encode("utf-8")
        result_sha256 = hashlib.sha256(result_bytes).hexdigest()

        async with self._session_factory() as session:
            async with session.begin():
                now = await self._event_time(session)
                inbox, duplicate_receipt = await self._receive_terminal_event(
                    session,
                    event=event,
                    payload=payload,
                    payload_hash=payload_hash,
                    result_sha256=result_sha256,
                    result_bytes=len(result_bytes),
                    now=now,
                )
                if duplicate_receipt is not None:
                    return duplicate_receipt

                rows = await self._lock_terminal_event_rows(session, event)
                rejection = _rejection_reason(
                    event,
                    rows.run,
                    rows.plan,
                    rows.round,
                    rows.task,
                    rows.execution,
                    rows.round_task,
                    rows.job,
                    now,
                )
                if rejection is None:
                    try:
                        await self._leases.assert_current_in_transaction(
                            session, event.lease
                        )
                    except LeaseLostError:
                        rejection = "worker_lease_lost"
                if rejection is not None:
                    receipt = self._rejected_receipt(event, rows.round, rejection)
                    _mark_processed(inbox, receipt, now)
                    return receipt

                assert rows.run is not None
                assert rows.plan is not None
                assert rows.round is not None
                assert rows.task is not None
                assert rows.round_task is not None
                assert rows.execution is not None
                assert rows.job is not None
                retry_scheduled, next_execution_id = await self._apply_terminal_result(
                    session,
                    event=event,
                    rows=rows,
                    now=now,
                    result_bytes=len(result_bytes),
                    result_sha256=result_sha256,
                )
                round_completed = await self._advance_terminal_round(
                    session,
                    event=event,
                    rows=rows,
                    retry_scheduled=retry_scheduled,
                    now=now,
                )
                # 重试会创建新的 execution_id；本次终态执行始终关闭自己的资源。
                if not await self._leases.finish_in_transaction(
                    session, event.lease, completed=True
                ):
                    raise LeaseLostError(event.lease.key)
                receipt = TaskEventReceipt(
                    message_id=event.message_id,
                    accepted=True,
                    round_completed=round_completed,
                    retry_scheduled=retry_scheduled,
                    next_execution_id=next_execution_id,
                    terminal_task_count=rows.round.terminal_task_count,
                    expected_task_count=rows.round.expected_task_count,
                )
                _mark_processed(inbox, receipt, now)
                return receipt

    async def _event_time(self, session: AsyncSession) -> datetime:
        """使用注入时钟或数据库时钟生成统一 UTC 事件时间。"""
        now = (
            self._clock()
            if self._clock is not None
            else await session.scalar(select(func.clock_timestamp()))
        )
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    async def _receive_terminal_event(
        self,
        session: AsyncSession,
        *,
        event: WorkerTaskTerminalEvent,
        payload: dict[str, Any],
        payload_hash: str,
        result_sha256: str,
        result_bytes: int,
        now: datetime,
    ) -> tuple[OrchestrationInboxModel, TaskEventReceipt | None]:
        """在当前事务内登记事件、校验消息 ID 并读取重放回执。"""
        inbox_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"result", "error"}
        } | {"result_sha256": result_sha256, "result_bytes": result_bytes}
        await session.execute(
            insert(OrchestrationInboxModel)
            .values(
                consumer_name=_CONSUMER_NAME,
                message_id=event.message_id,
                event_type="WorkerTaskTerminal",
                payload_json=inbox_payload,
                payload_hash=payload_hash,
                status="received",
                attempt=1,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["consumer_name", "message_id"])
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
            raise TaskEventConflict("message_id_reused_with_different_payload")
        if inbox.status == "processed":
            return inbox, _receipt(inbox.receipt_json, deduplicated=True)
        return inbox, None

    async def _lock_terminal_event_rows(
        self, session: AsyncSession, event: WorkerTaskTerminalEvent
    ) -> _TerminalEventRows:
        """按 Run、Plan、Round、Task、Execution 的既定顺序加锁。"""
        # 所有终态事件使用同一锁顺序，以免与取消事务形成死锁环。
        run = await _locked_one(
            session, AgentRunModel, AgentRunModel.run_id == event.run_id
        )
        plan = await _locked_one(
            session, AgentPlanModel, AgentPlanModel.plan_id == event.plan_id
        )
        round_row = await _locked_one(
            session, PlanRoundModel, PlanRoundModel.round_id == event.round_id
        )
        task = await _locked_one(
            session, AgentTaskModel, AgentTaskModel.task_id == event.task_id
        )
        round_task = await _locked_one(
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
        job = await _locked_one(
            session,
            WorkerJobModel,
            (WorkerJobModel.execution_id == event.execution_id)
            & WorkerJobModel.job_type.in_(("execute", "resume_worker"))
            & (WorkerJobModel.status == "running"),
        )
        return _TerminalEventRows(
            run, plan, round_row, task, round_task, execution, job
        )

    @staticmethod
    def _rejected_receipt(
        event: WorkerTaskTerminalEvent,
        round_row: PlanRoundModel | None,
        reason: str,
    ) -> TaskEventReceipt:
        """构造已记录但未应用的过期或无效终态事件回执。"""
        return TaskEventReceipt(
            message_id=event.message_id,
            accepted=False,
            reason=reason,
            terminal_task_count=(
                round_row.terminal_task_count if round_row is not None else None
            ),
            expected_task_count=(
                round_row.expected_task_count if round_row is not None else None
            ),
        )

    async def _apply_terminal_result(
        self,
        session: AsyncSession,
        *,
        event: WorkerTaskTerminalEvent,
        rows: _TerminalEventRows,
        now: datetime,
        result_bytes: int,
        result_sha256: str,
    ) -> tuple[bool, str | None]:
        """保存 Execution 结果并推进 Task，必要时创建下一次重试。"""
        assert rows.task is not None
        assert rows.execution is not None
        assert rows.round_task is not None
        assert rows.job is not None
        execution = rows.execution
        task = rows.task
        round_task = rows.round_task
        job = rows.job
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
        if result_bytes > _INLINE_RESULT_LIMIT_BYTES:
            # 大结果单独持久化，Execution 行只保留可校验的引用和摘要。
            result_ref = f"task-result:{execution.execution_id}"
            session.add(
                TaskExecutionResultModel(
                    result_ref=result_ref,
                    execution_id=execution.execution_id,
                    result_json=dict(event.result),
                    content_sha256=result_sha256,
                    content_bytes=result_bytes,
                    created_at=now,
                )
            )
            execution.result_ref = result_ref
            execution.result_json = {
                "status": status,
                "result_ref": result_ref,
                "result_bytes": result_bytes,
                "result_sha256": result_sha256,
                "error": dict(event.error) if event.error is not None else None,
                "retryable": event.retryable,
            }
        else:
            execution.result_ref = None
            execution.result_json = result_document
        execution.error_json = dict(event.error) if event.error is not None else None
        execution.updated_at = now
        job.status = "completed"
        job.updated_at = now

        retry_scheduled = (
            event.retryable
            and status == "failed"
            and execution.attempt < self._max_attempts
        )
        next_execution_id: str | None = None
        if retry_scheduled:
            next_execution_id = self._schedule_retry(
                session,
                event=event,
                task=task,
                execution=execution,
                round_task=round_task,
                now=now,
            )
            self._write_task_retrying_outbox(
                session,
                event=event,
                attempt=execution.attempt,
                next_execution_id=next_execution_id,
                now=now,
            )
            return True, next_execution_id

        task.status = status
        task.output_json = (
            {
                    "result_ref": execution.result_ref,
                    "result_bytes": result_bytes,
                    "result_sha256": result_sha256,
            }
            if execution.result_ref is not None
            else dict(event.result)
        )
        task.error_json = (
            dict(event.error) if event.error is not None else None
        )
        task.updated_at = now
        round_task.status = status
        round_task.terminal_at = now
        self._write_task_terminal_outbox(
            session,
            event=event,
            task=task,
            execution=execution,
            result_bytes=result_bytes,
            result_sha256=result_sha256,
            now=now,
        )
        return False, None

    async def _advance_terminal_round(
        self,
        session: AsyncSession,
        *,
        event: WorkerTaskTerminalEvent,
        rows: _TerminalEventRows,
        retry_scheduled: bool,
        now: datetime,
    ) -> bool:
        """更新 Round 终态屏障，并在全部任务收敛后安排 Root Resume。"""
        assert rows.run is not None
        assert rows.plan is not None
        assert rows.round is not None
        round_row = rows.round
        if not retry_scheduled:
            round_row.terminal_task_count += 1
        completed = not retry_scheduled and (
            round_row.terminal_task_count == round_row.expected_task_count
        )
        if not completed:
            return False

        round_row.status = "completed"
        round_row.completed_at = now
        rows.plan.status = "waiting_review"
        self._write_round_completed_outbox(session, event, round_row, now)
        if (
            rows.run.root_wait_round_id != round_row.round_id
            or rows.run.root_wait_plan_version != round_row.plan_version
        ):
            # 每个新 Round 使用新 generation，避免复用旧 Root checkpoint。
            rows.run.root_wait_generation = int(rows.run.root_wait_generation or 0) + 1
            rows.run.root_wait_round_id = round_row.round_id
            rows.run.root_wait_plan_version = round_row.plan_version
            rows.run.root_wait_armed_at = None
            rows.run.root_wait_interrupt_id = None
            rows.run.root_wait_checkpoint_id = None
        session.add(
            RootResumeJobModel(
                job_id=self._id_factory(),
                dedupe_key=(
                    f"round:{round_row.round_id}:root-resume:"
                    f"{rows.run.root_wait_generation}"
                ),
                run_id=rows.run.run_id,
                plan_id=rows.plan.plan_id,
                round_id=round_row.round_id,
                root_wait_generation=rows.run.root_wait_generation,
                status="pending",
                attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        return True

    def _write_task_retrying_outbox(
        self,
        session: AsyncSession,
        *,
        event: WorkerTaskTerminalEvent,
        attempt: int,
        next_execution_id: str,
        now: datetime,
    ) -> None:
        """Persist the retry transition with its next execution job."""
        session.add(
            OrchestrationOutboxModel(
                message_id=self._id_factory(),
                dedupe_key=f"execution:{event.execution_id}:retrying",
                event_type="TaskRetrying",
                correlation_id=event.run_id,
                plan_id=event.plan_id,
                plan_version=event.plan_version,
                round_id=event.round_id,
                task_id=event.task_id,
                execution_id=event.execution_id,
                payload_json={
                    "task_id": event.task_id,
                    "execution_id": event.execution_id,
                    "next_execution_id": next_execution_id,
                    "attempt": attempt,
                    "execution_status": event.status.value,
                    "error": dict(event.error) if event.error is not None else None,
                    "parent_activity_id": f"round:{event.round_id}",
                },
                schema_version=1,
                status="pending",
                attempt=0,
                available_at=now,
                occurred_at=now,
            )
        )

    def _write_task_terminal_outbox(
        self,
        session: AsyncSession,
        *,
        event: WorkerTaskTerminalEvent,
        task: AgentTaskModel,
        execution: TaskExecutionModel,
        result_bytes: int,
        result_sha256: str,
        now: datetime,
    ) -> None:
        """Commit the final Task event with its accepted terminal result."""
        status = event.status.value
        event_type = "TaskCompleted" if status == "completed" else "TaskFailed"
        result = (
            dict(event.result)
            if execution.result_ref is None
            else {
                "result_ref": execution.result_ref,
                "result_bytes": result_bytes,
                "result_sha256": result_sha256,
            }
        )
        session.add(
            OrchestrationOutboxModel(
                message_id=self._id_factory(),
                dedupe_key=f"execution:{event.execution_id}:terminal",
                event_type=event_type,
                correlation_id=event.run_id,
                plan_id=event.plan_id,
                plan_version=event.plan_version,
                round_id=event.round_id,
                task_id=event.task_id,
                execution_id=event.execution_id,
                payload_json={
                    "task_id": event.task_id,
                    "execution_id": event.execution_id,
                    "execution_generation": execution.attempt,
                    "attempt": execution.attempt,
                    "execution_status": status,
                    "title": task.title,
                    "objective": task.objective,
                    "worker_type": task.worker_type,
                    "result": result,
                    "error": dict(event.error) if event.error is not None else None,
                    "parent_activity_id": f"round:{event.round_id}",
                },
                schema_version=1,
                status="pending",
                attempt=0,
                available_at=now,
                occurred_at=now,
            )
        )

    def _schedule_retry(
        self,
        session: AsyncSession,
        *,
        event: WorkerTaskTerminalEvent,
        task: AgentTaskModel,
        execution: TaskExecutionModel,
        round_task: RoundTaskModel,
        now: datetime,
    ) -> str:
        """保存失败 attempt，并原子插入同一 Round 的下一次执行。"""
        attempt = execution.attempt + 1
        execution_id = self._id_factory()
        worker_thread_id = f"task:{task.task_id}:exec:{attempt}"
        session.add(
            TaskExecutionModel(
                execution_id=execution_id,
                round_id=event.round_id,
                task_id=event.task_id,
                attempt=attempt,
                worker_thread_id=worker_thread_id,
                status="queued",
                cancellation_epoch=event.cancellation_epoch,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            WorkerJobModel(
                job_id=self._id_factory(),
                dedupe_key=f"execute:{execution_id}",
                job_type="execute",
                execution_id=execution_id,
                payload_json={
                    "plan_id": event.plan_id,
                    "round_id": event.round_id,
                    "task_id": event.task_id,
                },
                status="pending",
                attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        task.status = "pending"
        task.output_json = None
        task.error_json = None
        task.execution_generation = attempt
        task.worker_thread_id = worker_thread_id
        task.active_execution_id = execution_id
        task.updated_at = now
        round_task.status = "pending"
        round_task.active_execution_id = execution_id
        round_task.terminal_at = None
        return execution_id

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


def _rejection_reason(
    event, run, plan, round_row, task, execution, round_task, job, now
):
    """验证身份、状态和执行栅栏，返回可持久化的拒绝原因。"""
    if any(
        item is None
        for item in (run, plan, round_row, task, execution, round_task, job)
    ):
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
    if job.status != "running":
        return "worker_job_not_running"
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
    if round_task.status in {"completed", "failed", "cancelled"}:
        return "task_already_terminal"
    return None


def _event_document(event: WorkerTaskTerminalEvent) -> dict[str, Any]:
    """生成事件信封的确定性 JSON 表示。"""
    return {
        "message_id": event.message_id,
        "occurred_at": event.occurred_at.isoformat(),
        "worker_id": event.worker_id,
        "run_id": event.run_id,
        "plan_id": event.plan_id,
        "plan_version": event.plan_version,
        "round_id": event.round_id,
        "task_id": event.task_id,
        "execution_id": event.execution_id,
        "cancellation_epoch": event.cancellation_epoch,
        "status": event.status.value,
        "result": dict(event.result),
        "error": dict(event.error) if event.error is not None else None,
        "retryable": event.retryable,
    }


def _strict_json(value: Any) -> str:
    """只编码标准 JSON，拒绝隐式字符串化的未知对象。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


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
        "retry_scheduled": receipt.retry_scheduled,
        "next_execution_id": receipt.next_execution_id,
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
        retry_scheduled=bool(value.get("retry_scheduled", False)),
        next_execution_id=(
            str(value["next_execution_id"])
            if value.get("next_execution_id") is not None
            else None
        ),
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
