"""Shared PostgreSQL persistence implementations for runtime capabilities."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from domain.runs import (
    CommandEnqueueResult,
    CommandStatus,
    CommandStatusRecord,
    CommandType,
    RunCommand,
    RunCommandPayload,
    RunStartPayload,
    RunCancellationResult,
    RunQueryPort,
    RunStatus,
    RunSummary,
    parse_run_command_payload,
)
from domain.runs.ports import CommandQueuePort, CommandStorePort, RunLifecyclePort

from ..models import (
    AgentCommandModel,
    AgentEventModel,
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    MemoryProcessingJobModel,
    MessageModel,
    OrchestrationOutboxModel,
    RoundTaskModel,
    RootResumeJobModel,
    SessionModel,
    TaskExecutionModel,
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


def _final_answer_text(result: dict[str, object]) -> str:
    """Extract the user-visible final answer from persisted Run output."""
    content = result.get("content")
    if isinstance(content, str):
        return content
    nested = result.get("result")
    if isinstance(nested, dict):
        content = nested.get("content")
        if isinstance(content, str):
            return content
    return ""


def _answer_stream_metadata(result: dict[str, object]) -> dict[str, Any]:
    """读取已校验正文结果中的流身份和最后片段序号。"""
    value = result.get("answer_stream")
    if not isinstance(value, dict):
        nested = result.get("result")
        value = nested.get("answer_stream") if isinstance(nested, dict) else None
    if not isinstance(value, dict):
        raise ValueError("answer_stream_metadata_missing")
    message_id = value.get("message_id")
    stream_id = value.get("stream_id")
    started_at = value.get("started_at")
    last_chunk_id = value.get("last_chunk_id")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(stream_id, str)
        or not stream_id
        or not isinstance(started_at, str)
        or not isinstance(last_chunk_id, int)
        or isinstance(last_chunk_id, bool)
        or last_chunk_id < 1
    ):
        raise ValueError("answer_stream_metadata_invalid")
    datetime.fromisoformat(started_at)
    return value


def _message_id(payload: RunCommandPayload) -> str | None:
    """Return the user message identity only for a new run."""
    if isinstance(payload, RunStartPayload):
        return payload.message_id
    return None


def _attachment_ids(payload: RunCommandPayload) -> tuple[str, ...]:
    """Expose attachments only for user-submitted start commands."""
    if isinstance(payload, RunStartPayload):
        return payload.attachment_ids
    return ()


def _run_start_payload_or_none(request_json: str) -> RunStartPayload | None:
    """Treat an empty run request as absent; validate every populated request."""
    if not request_json or request_json.strip() == "{}":
        return None
    return RunStartPayload.model_validate_json(request_json)


class PostgresRunRepository(
    CommandQueuePort,
    CommandStorePort,
    RunLifecyclePort,
    RunQueryPort,
):
    """Command queue, run projection and lifecycle in the target schema."""

    writes_cancellation_events_to_outbox = True
    writes_terminal_artifacts_to_outbox = True

    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def enqueue_command(self, command: RunCommand) -> CommandEnqueueResult:
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.get(AgentCommandModel, command.command_id)
                if existing is not None:
                    return self._result(existing, True)
                now = command.issued_at.isoformat()
                session.add(
                    AgentCommandModel(
                        command_id=command.command_id,
                        session_id=command.session_id,
                        run_id=command.run_id,
                        command_type=str(command.command_type),
                        schema_version=command.schema_version,
                        idempotency_key=command.command_id,
                        payload_json=json.dumps(
                            command.payload.model_dump(mode="json", exclude_none=True),
                            ensure_ascii=False,
                        ),
                        status=CommandStatus.QUEUED.value,
                        queued_at=now,
                    )
                )
                if (
                    command.run_id
                    and await session.get(AgentRunModel, command.run_id) is None
                ):
                    session.add(
                        AgentRunModel(
                            run_id=command.run_id,
                            session_id=command.session_id,
                            status=RunStatus.CREATED.value,
                            request_json=json.dumps(
                                command.payload.model_dump(
                                    mode="json", exclude_none=True
                                )
                            ),
                            root_thread_id=f"thread-{command.run_id}",
                            created_at=now,
                            updated_at=now,
                        )
                    )
            return CommandEnqueueResult(
                command.command_id,
                command.run_id,
                _message_id(command.payload),
                _attachment_ids(command.payload),
                CommandStatus.QUEUED,
                False,
            )

    async def get_command(self, command_id: str) -> CommandStatusRecord | None:
        async with self._session_factory() as session:
            row = await session.get(AgentCommandModel, command_id)
        return None if row is None else self._record(row)

    async def claim_next_command(self) -> RunCommand | None:
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AgentCommandModel)
                        .where(AgentCommandModel.status == CommandStatus.QUEUED.value)
                        .order_by(AgentCommandModel.queued_at)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                row.status = CommandStatus.RUNNING.value
                row.started_at = _now()
                return RunCommand(
                    row.command_id,
                    CommandType(row.command_type),
                    row.session_id,
                    row.run_id,
                    parse_run_command_payload(
                        CommandType(row.command_type), row.payload_json
                    ),
                    datetime.fromisoformat(row.queued_at),
                    row.schema_version,
                )

    async def complete_command(
        self,
        command_id: str,
        *,
        status: CommandStatus,
        result: object = None,
        error: object = None,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(AgentCommandModel, command_id)
                if row is not None:
                    row.status = str(status)
                    row.result_json = (
                        json.dumps(result, default=str) if result is not None else None
                    )
                    row.error_json = (
                        json.dumps(error, default=str) if error is not None else None
                    )
                    row.completed_at = _now()

    async def complete_active_command_for_run(
        self,
        run_id: str,
        *,
        status: CommandStatus,
        result: object = None,
        error: object = None,
    ) -> bool:
        """Converge the original command after an internal approval resume."""
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AgentCommandModel)
                        .where(
                            AgentCommandModel.run_id == run_id,
                            AgentCommandModel.status == CommandStatus.RUNNING.value,
                        )
                        .order_by(AgentCommandModel.started_at)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return False
                row.status = str(status)
                row.result_json = (
                    json.dumps(result, default=str) if result is not None else None
                )
                row.error_json = (
                    json.dumps(error, default=str) if error is not None else None
                )
                row.completed_at = _now()
                return True

    async def list_for_session(self, session_id: str) -> list[RunSummary]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentRunModel)
                        .where(AgentRunModel.session_id == session_id)
                        .order_by(AgentRunModel.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return await self._summaries(session, list(rows))

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None,
    ) -> tuple[list[RunSummary], int]:
        """分页读取未删除会话的运行，按提交时间从新到旧排列。

        Args:
            limit: 本页最多返回的条数。
            offset: 跳过的记录数。
            session_id: 可选会话筛选条件。

        Returns:
            当前页摘要及符合筛选条件的总条数。

        Raises:
            无预期业务异常；数据库异常由调用方处理。
        """
        conditions = [SessionModel.deleted_time.is_(None)]
        if session_id is not None:
            conditions.append(AgentRunModel.session_id == session_id)
        async with self._session_factory() as session:
            total = int((await session.execute(
                select(func.count()).select_from(AgentRunModel)
                .join(SessionModel, SessionModel.id == AgentRunModel.session_id)
                .where(*conditions)
            )).scalar_one())
            rows = (await session.execute(
                select(AgentRunModel)
                .join(SessionModel, SessionModel.id == AgentRunModel.session_id)
                .where(*conditions)
                .order_by(AgentRunModel.created_at.desc(), AgentRunModel.run_id.desc())
                .limit(limit).offset(offset)
            )).scalars().all()
            return await self._summaries(session, list(rows)), total

    async def _summaries(
        self, session: AsyncSession, rows: list[AgentRunModel],
    ) -> list[RunSummary]:
        """批量补齐实际执行起点，避免将排队时间计入执行耗时。

        Args:
            session: 当前只读数据库会话。
            rows: 当前页 Run 数据库记录。

        Returns:
            包含首次开始事件时间的运行摘要；无开始事件时保留空值。

        Raises:
            无预期业务异常；数据库异常由调用方处理。
        """
        if not rows:
            return []
        # Resume 不能重置执行起点；旧记录也直接从持久化事件恢复。
        starts = (await session.execute(
            select(AgentEventModel.run_id, func.min(cast(AgentEventModel.occurred_at, DateTime(timezone=True))))
            .where(
                AgentEventModel.run_id.in_([row.run_id for row in rows]),
                AgentEventModel.event_type.in_(("run.started", "run.resumed")),
            )
            .group_by(AgentEventModel.run_id)
        )).all()
        run_id_to_started_at = {run_id: started_at for run_id, started_at in starts}
        return [self._run(row, run_id_to_started_at.get(row.run_id)) for row in rows]

    async def get(self, run_id: str) -> RunSummary | None:
        async with self._session_factory() as session:
            row = await session.get(AgentRunModel, run_id)
        return None if row is None else self._run(row)

    async def get_active_for_session(self, session_id: str) -> RunSummary | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AgentRunModel)
                    .where(
                        AgentRunModel.session_id == session_id,
                        AgentRunModel.status.in_(
                            [
                                RunStatus.CREATED.value,
                                RunStatus.RUNNING.value,
                                RunStatus.CANCELLING.value,
                                RunStatus.PAUSED.value,
                            ]
                        ),
                    )
                    .order_by(AgentRunModel.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
        return None if row is None else self._run(row)

    async def update_status(self, run_id: str, status: RunStatus | str) -> bool:
        """仅从可运行状态推进 Run，禁止迟到的启动覆盖取消。"""
        active_states = (
            (RunStatus.CREATED.value, RunStatus.RUNNING.value)
            if str(status) == RunStatus.RUNNING.value
            else (
                RunStatus.CREATED.value,
                RunStatus.RUNNING.value,
                RunStatus.PAUSED.value,
            )
        )
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(AgentRunModel)
                    .where(
                        AgentRunModel.run_id == run_id,
                        AgentRunModel.status.in_(active_states),
                    )
                    .values(status=str(status), updated_at=_now())
                )
        return bool(result.rowcount)

    async def arm_root_wait(
        self,
        run_id: str,
        *,
        plan_id: str,
        round_id: str,
        plan_version: int,
        interrupt_id: str,
        checkpoint_id: str,
    ) -> bool:
        """Record a checkpointed Root wait only for its current Round generation."""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(AgentRunModel, run_id, with_for_update=True)
                plan = await session.get(AgentPlanModel, plan_id, with_for_update=True)
                if (
                    row is None
                    or plan is None
                    or row.status != RunStatus.RUNNING.value
                    or plan.run_id != run_id
                    or plan.active_round_id != round_id
                    or plan.current_version != plan_version
                    or not interrupt_id
                    or not checkpoint_id
                ):
                    return False

                if (
                    row.root_wait_round_id != round_id
                    or row.root_wait_plan_version != plan_version
                ):
                    row.root_wait_generation = int(row.root_wait_generation or 0) + 1
                    row.root_wait_round_id = round_id
                    row.root_wait_plan_version = plan_version

                row.root_wait_armed_at = _now()
                row.root_wait_interrupt_id = interrupt_id
                row.root_wait_checkpoint_id = checkpoint_id
                row.updated_at = _now()
                return True

    async def complete(self, run_id: str, result: dict[str, object]) -> bool:
        return await self._finish(run_id, RunStatus.COMPLETED, response=result)

    async def fail(self, run_id: str, error: dict[str, object]) -> bool:
        return await self._finish(run_id, RunStatus.FAILED, error=error)

    async def cancel(
        self, run_id: str, *, reason: str = "USER_REQUEST"
    ) -> RunCancellationResult | None:
        """在一个事务内设置取消栅栏并收敛关联计划状态。

        Args:
            run_id: 用户请求停止的 Run ID。
            reason: 取消原因，写入任务和计划事件。

        Returns:
            首次受理时返回取消状态；重复请求、缺失或终态 Run 返回 None。

        Raises:
            Exception: 数据库错误由调用方按可重试命令处理。
        """
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(AgentRunModel, run_id, with_for_update=True)
                if run is None:
                    return None
                if run.status in {
                    RunStatus.CANCELLING.value,
                    RunStatus.CANCELLED.value,
                    RunStatus.COMPLETED.value,
                    RunStatus.FAILED.value,
                }:
                    return RunCancellationResult(
                        RunStatus(run.status),
                        False,
                        str(run.cancellation_external_effect_status or "none"),
                    )

                plan = (
                    await session.execute(
                        select(AgentPlanModel)
                        .where(AgentPlanModel.run_id == run_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if plan is None or plan.status in {"completed", "failed", "cancelled"}:
                    return await self._cancel_run_without_active_plan(
                        session, run_id, run, plan, now
                    )

                active_execution_count = (
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
                        )
                    )
                ).scalar_one()
                return await self._begin_plan_cancellation(
                    session,
                    run_id,
                    run,
                    plan,
                    reason=reason,
                    now=now,
                    active_execution_count=active_execution_count,
                )

    async def _cancel_run_without_active_plan(
        self,
        session: AsyncSession,
        run_id: str,
        run: AgentRunModel,
        plan: AgentPlanModel | None,
        now: datetime,
    ) -> RunCancellationResult:
        """直接终结没有活动 Plan 的 Run，并在同一事务写入清理事件。"""
        now_iso = now.isoformat()
        external_effect_status = (
            "unknown" if run.status == RunStatus.RUNNING.value else "none"
        )
        run.status = RunStatus.CANCELLED.value
        run.cancellation_epoch = int(run.cancellation_epoch or 0) + 1
        run.cancellation_external_effect_status = external_effect_status
        run.updated_at = now_iso
        await self._cancel_root_resume_jobs(session, run_id, now)
        await self._cancel_pending_approvals(session, run_id=run_id, plan=None, now=now)
        await session.execute(
            update(AgentCommandModel)
            .where(
                AgentCommandModel.run_id == run_id,
                AgentCommandModel.status == CommandStatus.QUEUED.value,
                AgentCommandModel.command_type.in_(
                    (CommandType.RUN_START.value, CommandType.RUN_RESUME.value)
                ),
            )
            .values(
                status=CommandStatus.CANCELLED.value,
                completed_at=now_iso,
                result_json=json.dumps({"status": RunStatus.CANCELLED.value}),
            )
        )
        session.add(
            OrchestrationOutboxModel(
                message_id=uuid4().hex,
                dedupe_key=f"run:{run_id}:cancelled:{run.cancellation_epoch}",
                event_type="RunCancelled",
                correlation_id=run_id,
                plan_id=plan.plan_id if plan is not None else None,
                plan_version=plan.current_version if plan is not None else None,
                round_id=plan.active_round_id if plan is not None else None,
                payload_json={
                    "cancellation_epoch": run.cancellation_epoch,
                    "external_effect_status": external_effect_status,
                },
                schema_version=1,
                status="pending",
                attempt=0,
                available_at=now,
                occurred_at=now,
            )
        )
        return RunCancellationResult(RunStatus.CANCELLED, True, external_effect_status)

    async def _begin_plan_cancellation(
        self,
        session: AsyncSession,
        run_id: str,
        run: AgentRunModel,
        plan: AgentPlanModel,
        *,
        reason: str,
        now: datetime,
        active_execution_count: int,
    ) -> RunCancellationResult:
        """在 Run 锁内推进 Plan 取消并撤销尚未开始的工作。"""
        now_iso = now.isoformat()
        external_effect_status = (
            "unknown"
            if run.status == RunStatus.RUNNING.value or active_execution_count
            else "none"
        )
        cancellation_epoch = (
            max(int(run.cancellation_epoch or 0), int(plan.cancellation_epoch or 0)) + 1
        )
        run.status = RunStatus.CANCELLING.value
        run.cancellation_epoch = cancellation_epoch
        run.cancellation_external_effect_status = external_effect_status
        run.updated_at = now_iso
        plan.status = "cancelling"
        plan.cancellation_epoch = cancellation_epoch
        plan.updated_at = now_iso

        await self._cancel_pending_plan_tasks(
            session,
            run_id=run_id,
            plan=plan,
            cancellation_epoch=cancellation_epoch,
            reason=reason,
            now=now,
        )
        await self._cancel_active_plan_executions(
            session,
            plan=plan,
            cancellation_epoch=cancellation_epoch,
            now=now,
        )
        await self._cancel_pending_approvals(session, run_id=run_id, plan=plan, now=now)
        await self._cancel_root_resume_jobs(session, run_id, now)
        for event_type, scope in (
            ("PlanCancelling", f"plan:{plan.plan_id}"),
            ("RunCancelling", f"run:{run_id}"),
        ):
            session.add(
                OrchestrationOutboxModel(
                    message_id=uuid4().hex,
                    dedupe_key=f"{scope}:cancelling:{cancellation_epoch}",
                    event_type=event_type,
                    correlation_id=run_id,
                    plan_id=plan.plan_id,
                    plan_version=plan.current_version,
                    round_id=plan.active_round_id,
                    payload_json={
                        "cancellation_epoch": cancellation_epoch,
                        "reason": reason,
                        "external_effect_status": external_effect_status,
                    },
                    schema_version=1,
                    status="pending",
                    attempt=0,
                    available_at=now,
                    occurred_at=now,
                )
            )
        return RunCancellationResult(RunStatus.CANCELLING, True, external_effect_status)

    async def _cancel_pending_plan_tasks(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        plan: AgentPlanModel,
        cancellation_epoch: int,
        reason: str,
        now: datetime,
    ) -> None:
        """取消未启动任务并为每个任务写入同事务事件。"""
        task_ids = select(AgentTaskModel.task_id).where(
            AgentTaskModel.plan_id == plan.plan_id
        )
        pending_task_ids = select(AgentTaskModel.task_id).where(
            AgentTaskModel.plan_id == plan.plan_id,
            AgentTaskModel.status.in_(("pending", "waiting_approval")),
        )
        pending_tasks = (
            (
                await session.execute(
                    select(AgentTaskModel)
                    .where(
                        AgentTaskModel.plan_id == plan.plan_id,
                        AgentTaskModel.status.in_(("pending", "waiting_approval")),
                    )
                    .order_by(AgentTaskModel.task_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        await session.execute(
            update(RoundTaskModel)
            .where(
                RoundTaskModel.task_id.in_(pending_task_ids),
                RoundTaskModel.status.in_(("pending", "waiting_approval")),
            )
            .values(status="cancelled", terminal_at=now)
        )
        await session.execute(
            update(RoundTaskModel)
            .where(
                RoundTaskModel.task_id.in_(task_ids),
                RoundTaskModel.status == "running",
            )
            .values(status="cancelling")
        )
        await session.execute(
            update(AgentTaskModel)
            .where(
                AgentTaskModel.plan_id == plan.plan_id,
                AgentTaskModel.status.in_(("pending", "waiting_approval")),
            )
            .values(status="cancelled", updated_at=now.isoformat())
        )
        for task in pending_tasks:
            session.add(
                OrchestrationOutboxModel(
                    message_id=uuid4().hex,
                    dedupe_key=f"task:{task.task_id}:cancelled:{cancellation_epoch}",
                    event_type="TaskCancelled",
                    correlation_id=run_id,
                    plan_id=plan.plan_id,
                    plan_version=plan.current_version,
                    round_id=plan.active_round_id,
                    task_id=task.task_id,
                    execution_id=task.active_execution_id,
                    payload_json={
                        "task_id": task.task_id,
                        "execution_id": task.active_execution_id,
                        "execution_generation": task.execution_generation,
                        "title": task.title,
                        "objective": task.objective,
                        "status": "cancelled",
                        "reason": reason,
                        "parent_activity_id": (
                            f"round:{plan.active_round_id}"
                            if plan.active_round_id
                            else f"plan:{plan.plan_id}"
                        ),
                    },
                    schema_version=1,
                    status="pending",
                    attempt=0,
                    available_at=now,
                    occurred_at=now,
                )
            )
        await session.execute(
            update(AgentTaskModel)
            .where(
                AgentTaskModel.plan_id == plan.plan_id,
                AgentTaskModel.status == "running",
            )
            .values(status="cancelling", updated_at=now.isoformat())
        )

    async def _cancel_active_plan_executions(
        self,
        session: AsyncSession,
        *,
        plan: AgentPlanModel,
        cancellation_epoch: int,
        now: datetime,
    ) -> None:
        """推进执行与 Job 的取消栅栏，阻止旧 Worker 写入结果。"""
        task_ids = select(AgentTaskModel.task_id).where(
            AgentTaskModel.plan_id == plan.plan_id
        )
        execution_ids = select(TaskExecutionModel.execution_id).where(
            TaskExecutionModel.task_id.in_(task_ids)
        )
        await session.execute(
            update(TaskExecutionModel)
            .where(
                TaskExecutionModel.task_id.in_(task_ids),
                TaskExecutionModel.status.in_(
                    ("queued", "waiting_approval", "interrupted")
                ),
            )
            .values(
                status="cancelled",
                cancellation_epoch=cancellation_epoch,
                lease_owner=None,
                lease_until=None,
                updated_at=now,
            )
        )
        await session.execute(
            update(TaskExecutionModel)
            .where(
                TaskExecutionModel.task_id.in_(task_ids),
                TaskExecutionModel.status == "running",
            )
            .values(
                status="cancel_requested",
                cancellation_epoch=cancellation_epoch,
                updated_at=now,
            )
        )
        await session.execute(
            update(WorkerJobModel)
            .where(
                WorkerJobModel.execution_id.in_(execution_ids),
                WorkerJobModel.status == "pending",
            )
            .values(
                status="cancelled",
                lease_owner=None,
                lease_until=None,
                updated_at=now,
            )
        )
        await session.execute(
            update(WorkerJobModel)
            .where(
                WorkerJobModel.execution_id.in_(execution_ids),
                WorkerJobModel.status == "running",
            )
            .values(status="cancel_requested", updated_at=now)
        )

    async def _cancel_pending_approvals(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        plan: AgentPlanModel | None,
        now: datetime,
    ) -> None:
        """Cancel pending approvals and persist their user events atomically."""
        approvals = (
            (
                await session.execute(
                    select(ApprovalRecordModel)
                    .where(
                        ApprovalRecordModel.run_id == run_id,
                        ApprovalRecordModel.status == "pending",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for approval in approvals:
            approval.status = "cancelled"
            approval_plan_id = approval.plan_id or (plan.plan_id if plan else None)
            session.add(
                OrchestrationOutboxModel(
                    message_id=uuid4().hex,
                    dedupe_key=f"approval:{approval.approval_id}:cancelled",
                    event_type="ApprovalCancelled",
                    correlation_id=run_id,
                    plan_id=approval_plan_id,
                    plan_version=plan.current_version if plan else None,
                    round_id=plan.active_round_id if plan else None,
                    payload_json={
                        "approval_id": approval.approval_id,
                        "task_id": approval.task_id,
                        "execution_id": approval.execution_id,
                        "worker_thread_id": approval.worker_thread_id,
                        "interrupt_id": approval.interrupt_id,
                        "tool_call_id": approval.tool_call_id,
                        "tool_name": approval.tool_name,
                        "decision": "cancelled",
                        "parent_activity_id": (
                            f"task:{approval.task_id}" if approval.task_id else None
                        ),
                    },
                    schema_version=1,
                    status="pending",
                    attempt=0,
                    available_at=now,
                    occurred_at=now,
                )
            )

    async def _cancel_root_resume_jobs(
        self, session: AsyncSession, run_id: str, now: datetime
    ) -> None:
        """撤销尚未执行的 Root 恢复作业，避免取消后重新进入 Root Graph。"""
        await session.execute(
            update(RootResumeJobModel)
            .where(
                RootResumeJobModel.run_id == run_id,
                RootResumeJobModel.status.in_(("pending", "running")),
            )
            .values(
                status="cancelled",
                lease_owner=None,
                lease_until=None,
                updated_at=now,
            )
        )

    async def _finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        response: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        values: dict[str, Any] = {"status": status.value, "updated_at": now_iso}
        if response is not None:
            values["response_json"] = json.dumps(response, ensure_ascii=False)
        if error is not None:
            values["error_json"] = json.dumps(error, ensure_ascii=False)
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(AgentRunModel)
                    .where(
                        AgentRunModel.run_id == run_id,
                        AgentRunModel.status.in_(
                            (
                                RunStatus.CREATED.value,
                                RunStatus.RUNNING.value,
                                RunStatus.PAUSED.value,
                            )
                        ),
                    )
                    .values(**values)
                )
                if result.rowcount:
                    await session.execute(
                        update(RootResumeJobModel)
                        .where(
                            RootResumeJobModel.run_id == run_id,
                            RootResumeJobModel.status.in_(("pending", "running")),
                        )
                        .values(
                            status=(
                                "cancelled"
                                if status == RunStatus.CANCELLED
                                else "completed"
                            ),
                            lease_owner=None,
                            lease_until=None,
                            updated_at=now_iso,
                        )
                    )
                    if status == RunStatus.COMPLETED and response is not None:
                        run = await session.get(AgentRunModel, run_id)
                        if run is not None:
                            request = _run_start_payload_or_none(run.request_json)
                            user_text = request.message.strip() if request else ""
                            content = _final_answer_text(response)
                            user_message_id = request.message_id if request else ""
                            if user_text:
                                await session.execute(
                                    pg_insert(MemoryProcessingJobModel)
                                    .values(
                                        job_id=f"memjob_{uuid4().hex}",
                                        turn_id=run_id,
                                        session_id=run.session_id,
                                        user_text=user_text,
                                        assistant_text=content,
                                        status="queued",
                                        attempt=0,
                                        available_at=now_iso,
                                        created_at=now_iso,
                                        updated_at=now_iso,
                                    )
                                    .on_conflict_do_nothing(index_elements=["turn_id"])
                                )
                            if user_message_id and content:
                                assistant_message_id = f"assistant:{run_id}"
                                stream = _answer_stream_metadata(response)
                                if stream["message_id"] != assistant_message_id:
                                    raise ValueError("answer_stream_message_id_mismatch")
                                started_at = str(stream["started_at"])
                                await session.execute(
                                    pg_insert(MessageModel)
                                    .values(
                                        id=assistant_message_id,
                                        session_id=run.session_id,
                                        role="assistant",
                                        content=content,
                                        tool_calls_json="[]",
                                        run_id=run_id,
                                        timestamp=started_at,
                                    )
                                    .on_conflict_do_nothing(index_elements=["id"])
                                )
                                self._add_completion_outbox(
                                    session,
                                    run=run,
                                    message_id=f"run-final:{run_id}:answer-completed",
                                    event_type="RunAnswerCompleted",
                                    dedupe_key=f"run:{run_id}:answer:completed",
                                    payload_json={
                                        "message_id": assistant_message_id,
                                        "reply_to_message_id": user_message_id,
                                        "timestamp": started_at,
                                        "stream_id": stream["stream_id"],
                                        "stream_type": "answer",
                                        "content": content,
                                        "kind": response.get("kind", "final"),
                                        "last_chunk_id": stream["last_chunk_id"],
                                        "is_complete": True,
                                        "status": "completed",
                                    },
                                    now=now,
                                )
                            self._add_completion_outbox(
                                session,
                                run=run,
                                message_id=f"run-final:{run_id}:completed",
                                event_type="RunCompleted",
                                dedupe_key=f"run:{run_id}:completed",
                                payload_json={"summary": "运行已完成"},
                                now=now,
                            )
                        await session.execute(
                            update(AgentCommandModel)
                            .where(
                                AgentCommandModel.run_id == run_id,
                                AgentCommandModel.status.in_(
                                    (
                                        CommandStatus.QUEUED.value,
                                        CommandStatus.RUNNING.value,
                                    )
                                ),
                                AgentCommandModel.command_type.in_(
                                    (
                                        CommandType.MESSAGE_SUBMIT.value,
                                        CommandType.RUN_START.value,
                                        CommandType.RUN_RESUME.value,
                                    )
                                ),
                            )
                            .values(
                                status=CommandStatus.COMPLETED.value,
                                result_json=json.dumps(response, ensure_ascii=False),
                                error_json=None,
                                completed_at=now_iso,
                            )
                        )
                    elif status == RunStatus.FAILED:
                        run = await session.get(AgentRunModel, run_id)
                        if run is not None:
                            self._add_completion_outbox(
                                session,
                                run=run,
                                message_id=f"run-final:{run_id}:failed",
                                event_type="RunFailed",
                                dedupe_key=f"run:{run_id}:failed",
                                payload_json={"error": error or {"code": "run_failed"}},
                                now=now,
                            )
                        await session.execute(
                            update(AgentCommandModel)
                            .where(
                                AgentCommandModel.run_id == run_id,
                                AgentCommandModel.status.in_(
                                    (
                                        CommandStatus.QUEUED.value,
                                        CommandStatus.RUNNING.value,
                                    )
                                ),
                            )
                            .values(
                                status=CommandStatus.FAILED.value,
                                error_json=json.dumps(error or {"code": "run_failed"}, ensure_ascii=False),
                                completed_at=now_iso,
                            )
                        )
        return bool(result.rowcount)

    @staticmethod
    def _add_completion_outbox(
        session: AsyncSession,
        *,
        run: AgentRunModel,
        message_id: str,
        event_type: str,
        dedupe_key: str,
        payload_json: dict[str, Any],
        now: datetime,
    ) -> None:
        session.add(
            OrchestrationOutboxModel(
                message_id=message_id,
                dedupe_key=dedupe_key,
                event_type=event_type,
                correlation_id=run.run_id,
                plan_id=None,
                plan_version=None,
                round_id=None,
                task_id=None,
                execution_id=None,
                payload_json=payload_json,
                schema_version=1,
                status="pending",
                attempt=0,
                available_at=now,
                occurred_at=now,
            )
        )

    @staticmethod
    def _result(row: AgentCommandModel, deduplicated: bool) -> CommandEnqueueResult:
        payload = parse_run_command_payload(
            CommandType(row.command_type), row.payload_json
        )
        return CommandEnqueueResult(
            row.command_id,
            row.run_id,
            _message_id(payload),
            _attachment_ids(payload),
            row.status,
            deduplicated,
        )

    @staticmethod
    def _record(row: AgentCommandModel) -> CommandStatusRecord:
        return CommandStatusRecord(
            row.command_id,
            row.session_id,
            row.run_id,
            row.command_type,
            row.status,
            _json(row.result_json, None),
            _json(row.error_json, None),
        )

    @staticmethod
    def _run(row: AgentRunModel, started_at: datetime | None = None) -> RunSummary:
        """将数据库行转为摘要，并提取用户任务标题。

        Args:
            row: Run 数据库行。
            started_at: 首次执行的服务端事件时间，未记录时为空。

        Returns:
            只读运行摘要；任务标题限制为 120 字符。

        Raises:
            ValueError: 数据库时间字符串无法解析。
        """
        request = _json(row.request_json, {})
        message = request.get("message", "") if isinstance(request, dict) else ""
        return RunSummary(
            row.run_id,
            row.session_id,
            row.status,
            row.root_thread_id,
            _json(row.error_json, None),
            datetime.fromisoformat(row.created_at),
            datetime.fromisoformat(row.updated_at),
            started_at=started_at,
            title=message[:120] if isinstance(message, str) else "",
        )


__all__ = [
    "PostgresRunRepository",
]
