"""Shared PostgreSQL persistence implementations for runtime capabilities."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.runs import (
    CommandType,
    RunCommand,
    RunEnqueueResult,
    RunStartPayload,
    RunCancellationResult,
    RunQueryPort,
    RunStatus,
    RunSummary,
)
from domain.runs.ports import RunLifecyclePort, RunQueuePort, RunSubmissionPort
from domain.sessions import Session
from shared.contracts.runs import RunStartPayloadSchema

from ..models import (
    AttachmentModel,
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
    SessionMemorySettingsModel,
    SessionModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from .memory_suppression import (
    ensure_writable_run_scope,
    project_scope_is_writable,
)
from ..mappers.converters import session_domain_to_model
from ..idempotency import execute_idempotent


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


def _run_enqueue_to_json(value: RunEnqueueResult) -> dict[str, Any]:
    return {
        "session_id": value.session_id,
        "run_id": value.run_id,
        "message_id": value.message_id,
        "sequence": value.sequence,
        "status": str(value.status),
        "deduplicated": value.deduplicated,
    }


def _run_enqueue_from_json(value: dict[str, Any]) -> RunEnqueueResult:
    return RunEnqueueResult(
        session_id=str(value["session_id"]),
        run_id=str(value["run_id"]),
        message_id=str(value["message_id"]),
        sequence=int(value["sequence"]),
        status=str(value["status"]),
        deduplicated=True,
    )


def _stop_to_json(value: RunCancellationResult) -> dict[str, Any]:
    return {
        "run_id": value.run_id,
        "status": value.status.value,
        "changed": value.changed,
        "external_effect_status": value.external_effect_status,
        "no_op": value.no_op,
    }


def _stop_from_json(value: dict[str, Any]) -> RunCancellationResult:
    return RunCancellationResult(
        status=RunStatus(str(value["status"])),
        changed=bool(value["changed"]),
        external_effect_status=str(value["external_effect_status"]),
        run_id=str(value["run_id"]) if value.get("run_id") is not None else None,
        no_op=bool(value.get("no_op", False)),
    )


def _run_start_payload_or_none(request_json: str) -> RunStartPayload | None:
    """Treat an empty run request as absent; validate every populated request."""
    if not request_json or request_json.strip() == "{}":
        return None
    return RunStartPayloadSchema.model_validate_json(request_json).to_domain()


class PostgresRunRepository(RunQueuePort, RunSubmissionPort, RunLifecyclePort, RunQueryPort):
    """FIFO Run queue, run projection and lifecycle in the target schema."""

    writes_cancellation_events_to_outbox = True
    writes_terminal_artifacts_to_outbox = True

    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def enqueue_run(
        self,
        *,
        request_id: str,
        parameters: dict[str, object],
        session_id: str,
        project_id: str | None,
        run_id: str,
        payload: RunStartPayload,
        new_session: Session | None = None,
    ) -> tuple[RunEnqueueResult, bool]:
        """Atomically persist an idempotent user message and queued Run."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        async with self._session_factory() as db:
            async with db.begin():
                async def create() -> RunEnqueueResult:
                    if new_session is not None:
                        if new_session.id != session_id or new_session.project_id is None:
                            raise ValueError("new_session_invalid")
                        if not await project_scope_is_writable(db, new_session.project_id):
                            raise ValueError("project_not_found")
                        db.add(session_domain_to_model(new_session))
                        await db.flush()

                    # 项目删除也先取得作用域锁；已有会话提交必须采用相同顺序。
                    scope_project_id = await db.scalar(
                        select(SessionModel.project_id).where(
                            SessionModel.id == session_id,
                            SessionModel.deleted_time.is_(None),
                        )
                    )
                    if scope_project_id is not None and not await project_scope_is_writable(db, scope_project_id):
                        raise ValueError("project_not_found")
                    session_row = await db.scalar(
                        select(SessionModel)
                        .where(
                            SessionModel.id == session_id,
                            SessionModel.deleted_time.is_(None),
                        )
                        .with_for_update()
                    )
                    if session_row is None:
                        raise ValueError("session_not_found")
                    if project_id is not None and session_row.project_id != project_id:
                        raise ValueError("session_project_mismatch")
                    writable, _ = await ensure_writable_run_scope(
                        db, session_id=session_id, turn_id=run_id, now=now_iso
                    )
                    if not writable:
                        raise ValueError("project_not_found")

                    attachment_rows: list[AttachmentModel] = []
                    if payload.attachment_ids:
                        attachment_rows = list(
                            (
                                await db.execute(
                                    select(AttachmentModel)
                                    .where(
                                        AttachmentModel.id.in_(payload.attachment_ids),
                                        AttachmentModel.session_id == session_id,
                                        AttachmentModel.status == "ready",
                                        AttachmentModel.deleted_time.is_(None),
                                    )
                                    .with_for_update()
                                )
                            ).scalars().all()
                        )
                        if {row.id for row in attachment_rows} != set(payload.attachment_ids):
                            raise ValueError("attachment_not_ready_or_not_found")

                    sequence = int(session_row.last_run_sequence or 0) + 1
                    session_row.last_run_sequence = sequence
                    session_row.updated_at = now_iso
                    session_row.last_activity_at = now_iso
                    db.add(
                        MessageModel(
                            id=payload.message_id,
                            session_id=session_id,
                            role="user",
                            content=payload.message,
                            tool_calls_json="[]",
                            run_id=run_id,
                            timestamp=now_iso,
                        )
                    )
                    for attachment in attachment_rows:
                        attachment.message_id = payload.message_id

                    persisted_payload = replace(payload, sequence=sequence)
                    request_json = json.dumps(
                        RunStartPayloadSchema.from_domain(persisted_payload).model_dump(
                            mode="json", exclude_none=True
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    db.add(
                        AgentRunModel(
                            run_id=run_id,
                            session_id=session_id,
                            message_id=payload.message_id,
                            sequence=sequence,
                            status=RunStatus.QUEUED.value,
                            request_json=request_json,
                            root_thread_id=f"thread-{run_id}",
                            created_at=now_iso,
                            updated_at=now_iso,
                        )
                    )
                    await self._write_queued_events(
                        db,
                        session_id=session_id,
                        run_id=run_id,
                        message_id=payload.message_id,
                        sequence=sequence,
                        occurred_at=now_iso,
                        content=payload.message,
                        request_id=request_id,
                        attachment_ids=payload.attachment_ids,
                    )
                    return RunEnqueueResult(
                        session_id=session_id,
                        run_id=run_id,
                        message_id=payload.message_id,
                        sequence=sequence,
                        status=RunStatus.QUEUED,
                        deduplicated=False,
                    )

                result, replayed = await execute_idempotent(
                    db,
                    scope="runs.submit",
                    request_id=request_id,
                    parameters=parameters,
                    action=create,
                    encode=_run_enqueue_to_json,
                    decode=_run_enqueue_from_json,
                )
                # The stable first response is persisted with the Run and source Message.
                return result, replayed

    async def _write_queued_events(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        run_id: str,
        message_id: str,
        sequence: int,
        occurred_at: str,
        content: str,
        request_id: str,
        attachment_ids: Sequence[str],
    ) -> None:
        """Write submission lifecycle events within the business transaction."""
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(session_id, 0)
                )
            )
        )
        max_seq = int(
            await db.scalar(
                select(func.max(AgentEventModel.session_seq)).where(
                    AgentEventModel.session_id == session_id
                )
            )
            or 0
        )
        for offset, (event_type, payload) in enumerate(
            (
                (
                    "message.persisted",
                    {
                        "role": "user",
                        "timestamp": occurred_at,
                        "content": content,
                        "request_id": request_id,
                        "attachment_ids": list(attachment_ids),
                    },
                ),
                ("run.queued", {"sequence": sequence, "status": "queued"}),
            ),
            start=1,
        ):
            db.add(
                AgentEventModel(
                    session_id=session_id,
                    session_seq=max_seq + offset,
                    run_id=run_id,
                    message_id=message_id,
                    event_type=event_type,
                    durability="durable",
                    payload_json=json.dumps(payload, ensure_ascii=False),
                    occurred_at=occurred_at,
                )
            )

    async def claim_next_run(self) -> RunCommand | None:
        """Claim the oldest queued Run for one locked, idle Session."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        async with self._session_factory() as db:
            async with db.begin():
                candidate_session = (
                    await db.execute(
                        select(SessionModel)
                        .where(
                            SessionModel.deleted_time.is_(None),
                            SessionModel.active_run_id.is_(None),
                            select(AgentRunModel.run_id)
                            .where(
                                AgentRunModel.session_id == SessionModel.id,
                                AgentRunModel.status == RunStatus.QUEUED.value,
                            )
                            .exists(),
                        )
                        .order_by(SessionModel.last_activity_at, SessionModel.id)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if candidate_session is None:
                    return None
                run = (
                    await db.execute(
                        select(AgentRunModel)
                        .where(
                            AgentRunModel.session_id == candidate_session.id,
                            AgentRunModel.status == RunStatus.QUEUED.value,
                        )
                        .order_by(AgentRunModel.sequence)
                        .with_for_update()
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if run is None:
                    return None
                payload = _run_start_payload_or_none(run.request_json)
                if payload is None:
                    run.status = RunStatus.FAILED.value
                    run.error_json = json.dumps({"code": "run_request_missing"})
                    run.updated_at = now_iso
                    self._add_completion_outbox(
                        db,
                        run=run,
                        message_id=f"run-final:{run.run_id}:failed",
                        event_type="RunFailed",
                        dedupe_key=f"run:{run.run_id}:failed",
                        payload_json={"error": {"code": "run_request_missing"}},
                        now=now,
                    )
                    return None
                run.status = RunStatus.RUNNING.value
                run.updated_at = now_iso
                run.execution_generation = int(run.execution_generation or 0) + 1
                lease_owner = f"run-consumer:{uuid4().hex}"
                run.execution_lease_owner = lease_owner
                run.execution_lease_until = (now + timedelta(seconds=60)).isoformat()
                candidate_session.active_run_id = run.run_id
                return RunCommand(
                    command_id=f"run:{run.run_id}:generation:{run.execution_generation}",
                    command_type=CommandType.RUN_START,
                    session_id=run.session_id,
                    run_id=run.run_id,
                    payload=payload,
                    issued_at=datetime.fromisoformat(run.created_at),
                    schema_version=1,
                    execution_generation=run.execution_generation,
                    lease_owner=lease_owner,
                )

    async def list_for_session(self, session_id: str) -> list[RunSummary]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentRunModel)
                        .where(AgentRunModel.session_id == session_id)
                        .order_by(AgentRunModel.sequence)
                    )
                )
                .scalars()
                .all()
            )
            return await self._summaries(session, list(rows))

    async def list_recent(
        self,
        *,
        limit: int,
        offset: int,
        session_id: str | None = None,
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
            total = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(AgentRunModel)
                        .join(SessionModel, SessionModel.id == AgentRunModel.session_id)
                        .where(*conditions)
                    )
                ).scalar_one()
            )
            rows = (
                (
                    await session.execute(
                        select(AgentRunModel)
                        .join(SessionModel, SessionModel.id == AgentRunModel.session_id)
                        .where(*conditions)
                        .order_by(
                            AgentRunModel.created_at.desc(), AgentRunModel.run_id.desc()
                        )
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
            return await self._summaries(session, list(rows)), total

    async def _summaries(
        self,
        session: AsyncSession,
        rows: list[AgentRunModel],
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
        starts = (
            await session.execute(
                select(
                    AgentEventModel.run_id,
                    func.min(
                        cast(AgentEventModel.occurred_at, DateTime(timezone=True))
                    ),
                )
                .where(
                    AgentEventModel.run_id.in_([row.run_id for row in rows]),
                    AgentEventModel.event_type.in_(("run.started", "run.resumed")),
                )
                .group_by(AgentEventModel.run_id)
            )
        ).all()
        run_id_to_started_at = {run_id: started_at for run_id, started_at in starts}
        return [self._run(row, run_id_to_started_at.get(row.run_id)) for row in rows]

    async def get(self, run_id: str) -> RunSummary | None:
        async with self._session_factory() as session:
            row = await session.get(AgentRunModel, run_id)
        return None if row is None else self._run(row)

    async def get_active_for_session(self, session_id: str) -> RunSummary | None:
        async with self._session_factory() as session:
            active_run_id = await session.scalar(
                select(SessionModel.active_run_id).where(
                    SessionModel.id == session_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if active_run_id is None:
                return None
            row = (
                await session.execute(
                    select(AgentRunModel)
                    .where(AgentRunModel.run_id == active_run_id)
                )
            ).scalar_one_or_none()
        return None if row is None else self._run(row)

    async def update_status(self, run_id: str, status: RunStatus | str) -> bool:
        """仅从可运行状态推进 Run，禁止迟到的启动覆盖取消。"""
        if str(status) == RunStatus.RUNNING.value:
            async with self._session_factory() as session:
                return await session.scalar(
                    select(AgentRunModel.run_id)
                    .join(SessionModel, SessionModel.id == AgentRunModel.session_id)
                    .where(
                        AgentRunModel.run_id == run_id,
                        AgentRunModel.status == RunStatus.RUNNING.value,
                        SessionModel.active_run_id == run_id,
                    )
                ) is not None
        if str(status) not in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
            return False
        async with self._session_factory() as session:
            async with session.begin():
                session_id = await session.scalar(
                    select(AgentRunModel.session_id).where(
                        AgentRunModel.run_id == run_id
                    )
                )
                if session_id is None:
                    return False
                session_row = await session.scalar(
                    select(SessionModel)
                    .where(SessionModel.id == session_id)
                    .with_for_update()
                )
                run = await session.get(AgentRunModel, run_id, with_for_update=True)
                if (
                    session_row is None
                    or session_row.active_run_id != run_id
                    or run.status != RunStatus.RUNNING.value
                ):
                    return False
                run.status = str(status)
                run.updated_at = _now()
                session_row.active_run_id = None
                return True

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
                session_id = await session.scalar(
                    select(AgentRunModel.session_id).where(
                        AgentRunModel.run_id == run_id
                    )
                )
                if session_id is None:
                    return False
                session_row = await session.scalar(
                    select(SessionModel)
                    .where(SessionModel.id == session_id)
                    .with_for_update()
                )
                row = await session.get(AgentRunModel, run_id, with_for_update=True)
                plan = await session.get(AgentPlanModel, plan_id, with_for_update=True)
                if (
                    session_row is None
                    or session_row.active_run_id != run_id
                    or row is None
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

    async def complete(
        self, run_id: str, result: dict[str, object], *,
        owner: str | None = None, generation: int | None = None,
    ) -> bool:
        return await self._finish(
            run_id, RunStatus.COMPLETED, response=result,
            owner=owner, generation=generation,
        )

    async def fail(
        self, run_id: str, error: dict[str, object], *,
        owner: str | None = None, generation: int | None = None,
    ) -> bool:
        """记录失败并撤销工作，保留会话执行权直到所有调用停止。"""
        now = datetime.now(timezone.utc)
        async with self._session_factory() as db:
            async with db.begin():
                session_id = await db.scalar(
                    select(AgentRunModel.session_id).where(AgentRunModel.run_id == run_id)
                )
                if session_id is None:
                    return False
                session_row = await db.scalar(
                    select(SessionModel).where(SessionModel.id == session_id).with_for_update()
                )
                run = await db.get(AgentRunModel, run_id, with_for_update=True)
                if (
                    session_row is None or run is None
                    or session_row.active_run_id != run_id
                    or run.status != RunStatus.RUNNING.value
                    or not self._owns_execution_lease(run, owner, generation, datetime.now(timezone.utc))
                ):
                    return False
                await self._begin_failure_locked(db, run, error, now)
        # 无执行中的调用或 Worker 时可立即完成；否则由调用端或 Worker 收尾。
        await self.finalize_stop(run_id)
        return True

    async def reconcile_expired_execution_leases(self) -> int:
        """回收异常退出的 Root 调用；正常 checkpoint 等待没有调用租约。"""
        now = datetime.now(timezone.utc)
        recovered: list[str] = []
        async with self._session_factory() as db:
            async with db.begin():
                sessions = (await db.scalars(
                    select(SessionModel)
                    .join(AgentRunModel, AgentRunModel.run_id == SessionModel.active_run_id)
                    .where(
                        AgentRunModel.status == RunStatus.RUNNING.value,
                        AgentRunModel.execution_lease_owner.is_not(None),
                        AgentRunModel.execution_lease_until.is_not(None),
                        AgentRunModel.execution_lease_until <= now.isoformat(),
                    )
                    .order_by(SessionModel.id)
                    .with_for_update(of=SessionModel, skip_locked=True)
                    .limit(32)
                )).all()
                for session_row in sessions:
                    run = await db.get(AgentRunModel, session_row.active_run_id, with_for_update=True)
                    if (
                        run is None or run.status != RunStatus.RUNNING.value
                        or run.execution_lease_owner is None
                        or run.execution_lease_until is None
                        or datetime.fromisoformat(run.execution_lease_until) > now
                    ):
                        continue
                    await self._begin_failure_locked(
                        db, run, {"code": "root_execution_lease_expired"}, now
                    )
                    recovered.append(run.run_id)
        for run_id in recovered:
            await self.finalize_stop(run_id)
        return len(recovered)

    async def _begin_failure_locked(
        self, db: AsyncSession, run: AgentRunModel,
        error: dict[str, object], now: datetime,
    ) -> None:
        """在 Session、Run 锁内保存失败原因并启动同一套取消栅栏。"""
        run.error_json = json.dumps(error, ensure_ascii=False)
        plan = await db.scalar(
            select(AgentPlanModel).where(AgentPlanModel.run_id == run.run_id).with_for_update()
        )
        if plan is None or plan.status in {"completed", "failed", "cancelled"}:
            await self._begin_stop_without_active_plan(db, run.run_id, run, plan, now)
        else:
            await self._begin_plan_cancellation(
                db, run.run_id, run, plan, reason="ROOT_EXECUTION_FAILED",
                now=now, active_execution_count=0,
            )

    @staticmethod
    def _owns_execution_lease(
        run: AgentRunModel, owner: str | None, generation: int | None, now: datetime,
    ) -> bool:
        """校验调用端的租约和代次，拒绝失去执行权的迟到结果。"""
        if owner is None and generation is None:
            return True
        return (
            owner is not None and generation is not None
            and run.execution_lease_owner == owner
            and run.execution_generation == generation
            and run.execution_lease_until is not None
            and datetime.fromisoformat(run.execution_lease_until) > now
        )

    async def acquire_execution_lease(
        self, run_id: str, *, owner: str, lease_seconds: float
    ) -> int | None:
        if not owner.strip() or lease_seconds <= 0:
            raise ValueError("execution_lease_owner_and_duration_required")
        now = datetime.now(timezone.utc)
        async with self._session_factory() as db:
            async with db.begin():
                session_id = await db.scalar(
                    select(AgentRunModel.session_id).where(AgentRunModel.run_id == run_id)
                )
                if session_id is None:
                    return None
                session_row = await db.scalar(
                    select(SessionModel).where(SessionModel.id == session_id).with_for_update()
                )
                run = await db.get(AgentRunModel, run_id, with_for_update=True)
                if (
                    session_row is None
                    or run is None
                    or session_row.active_run_id != run_id
                    or run.status != RunStatus.RUNNING.value
                ):
                    return None
                # 过期租约代表调用异常退出，必须先失败收尾，不能自动重放。
                if run.execution_lease_owner is not None or run.execution_lease_until is not None:
                    return None
                run.execution_generation = int(run.execution_generation or 0) + 1
                run.execution_lease_owner = owner
                run.execution_lease_until = (
                    now + timedelta(seconds=lease_seconds)
                ).isoformat()
                run.updated_at = now.isoformat()
                return run.execution_generation

    async def renew_execution_lease(
        self,
        run_id: str,
        *,
        owner: str,
        generation: int,
        lease_seconds: float,
    ) -> bool:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as db:
            async with db.begin():
                session_id = await db.scalar(
                    select(AgentRunModel.session_id).where(
                        AgentRunModel.run_id == run_id
                    )
                )
                if session_id is None:
                    return False
                session_row = await db.scalar(
                    select(SessionModel)
                    .where(SessionModel.id == session_id)
                    .with_for_update()
                )
                run = await db.get(AgentRunModel, run_id, with_for_update=True)
                if (
                    session_row is None
                    or session_row.active_run_id != run_id
                    or run is None
                    or run.status != RunStatus.RUNNING.value
                    or run.execution_lease_owner != owner
                    or run.execution_generation != generation
                    or not run.execution_lease_until
                    or datetime.fromisoformat(run.execution_lease_until) <= now
                ):
                    return False
                run.execution_lease_until = (
                    now + timedelta(seconds=lease_seconds)
                ).isoformat()
                run.updated_at = now.isoformat()
                return True

    async def release_execution_lease(
        self, run_id: str, *, owner: str, generation: int
    ) -> bool:
        async with self._session_factory() as db:
            async with db.begin():
                session_id = await db.scalar(
                    select(AgentRunModel.session_id).where(
                        AgentRunModel.run_id == run_id
                    )
                )
                if session_id is None:
                    return False
                session_row = await db.scalar(
                    select(SessionModel)
                    .where(SessionModel.id == session_id)
                    .with_for_update()
                )
                run = await db.get(AgentRunModel, run_id, with_for_update=True)
                if (
                    session_row is None
                    or session_row.active_run_id != run_id
                    or run is None
                    or run.execution_lease_owner != owner
                    or run.execution_generation != generation
                    or not run.execution_lease_until
                    or datetime.fromisoformat(run.execution_lease_until) <= datetime.now(timezone.utc)
                ):
                    return False
                run.execution_lease_owner = None
                run.execution_lease_until = None
                return True

    async def finalize_stop(self, run_id: str, *, failed: bool = False) -> bool:
        """Finalize a stopped Root after its caller confirms the invocation ended."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        async with self._session_factory() as db:
            async with db.begin():
                session_id = await db.scalar(
                    select(AgentRunModel.session_id).where(AgentRunModel.run_id == run_id)
                )
                if session_id is None:
                    return False
                session_row = await db.scalar(
                    select(SessionModel)
                    .where(SessionModel.id == session_id)
                    .with_for_update()
                )
                run = await db.get(AgentRunModel, run_id, with_for_update=True)
                if (
                    session_row is None
                    or run is None
                    or run.status != RunStatus.STOPPING.value
                    or session_row.active_run_id != run_id
                ):
                    return False
                plan = await db.scalar(
                    select(AgentPlanModel).where(AgentPlanModel.run_id == run_id)
                )
                lease_until = (
                    datetime.fromisoformat(run.execution_lease_until)
                    if run.execution_lease_until
                    else None
                )
                if lease_until is not None and lease_until > now:
                    return False
                if plan is not None and plan.status not in {"completed", "failed", "cancelled"}:
                    # Worker reconciliation owns the terminal transition while workers
                    # still hold leases; it clears active_run_id with the plan result.
                    return False
                failed = failed or run.error_json is not None
                terminal = RunStatus.FAILED if failed else RunStatus.CANCELLED
                run.status = terminal.value
                run.updated_at = now_iso
                run.execution_lease_owner = None
                run.execution_lease_until = None
                session_row.active_run_id = None
                await self._enqueue_memory_job(db, run, assistant_text="", now_iso=now_iso)
                self._add_completion_outbox(
                    db,
                    run=run,
                    message_id=f"run-final:{run_id}:{terminal.value}",
                    event_type="RunFailed" if failed else "RunCancelled",
                    dedupe_key=f"run:{run_id}:{terminal.value}",
                    payload_json={
                        "error": _json(run.error_json, {"code": "run_failed"}) if failed else None,
                        "external_effect_status": run.cancellation_external_effect_status,
                    },
                    now=now,
                )
                return True

    async def stop_active(
        self,
        session_id: str,
        *,
        request_id: str,
        expected_run_id: str | None,
        displayed_session_seq: int,
    ) -> tuple[RunCancellationResult, bool]:
        """Idempotently stop only the Run the client saw as active."""
        now = datetime.now(timezone.utc)
        async with self._session_factory() as db:
            async with db.begin():
                async def stop() -> RunCancellationResult:
                    session_row = await db.scalar(
                        select(SessionModel)
                        .where(
                            SessionModel.id == session_id,
                            SessionModel.deleted_time.is_(None),
                        )
                        .with_for_update()
                    )
                    if session_row is None:
                        raise LookupError("session_not_found")
                    active_run_id = session_row.active_run_id
                    if active_run_id != expected_run_id:
                        raise ValueError("active_run_changed")
                    if active_run_id is None:
                        return RunCancellationResult(
                            RunStatus.CANCELLED,
                            changed=False,
                            external_effect_status="none",
                            no_op=True,
                        )
                    if displayed_session_seq < 0:
                        raise ValueError("displayed_session_seq_invalid")
                    max_session_seq = int(
                        await db.scalar(
                            select(func.max(AgentEventModel.session_seq)).where(
                                AgentEventModel.session_id == session_id
                            )
                        )
                        or 0
                    )
                    if displayed_session_seq > max_session_seq:
                        raise ValueError("displayed_session_seq_invalid")

                    run = await db.get(AgentRunModel, active_run_id, with_for_update=True)
                    if run is None or run.session_id != session_id:
                        raise ValueError("active_run_changed")
                    if run.status == RunStatus.STOPPING.value:
                        return RunCancellationResult(
                            RunStatus.STOPPING,
                            changed=False,
                            external_effect_status=str(
                                run.cancellation_external_effect_status or "unknown"
                            ),
                            run_id=run.run_id,
                        )
                    if run.status != RunStatus.RUNNING.value:
                        raise ValueError("active_run_changed")

                    run.display_cutoff_session_seq = displayed_session_seq
                    plan = (
                        await db.execute(
                            select(AgentPlanModel)
                            .where(AgentPlanModel.run_id == run.run_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if plan is None or plan.status in {"completed", "failed", "cancelled"}:
                        return await self._begin_stop_without_active_plan(
                            db, run.run_id, run, plan, now
                        )

                    active_execution_count = (
                        await db.execute(
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
                        db,
                        run.run_id,
                        run,
                        plan,
                        reason="USER_REQUEST",
                        now=now,
                        active_execution_count=active_execution_count,
                    )

                result, replayed = await execute_idempotent(
                    db,
                    scope=f"sessions:{session_id}:stop",
                    request_id=request_id,
                    parameters={
                        "expected_run_id": expected_run_id,
                        "displayed_session_seq": displayed_session_seq,
                    },
                    action=stop,
                    encode=_stop_to_json,
                    decode=_stop_from_json,
                )
                return result, replayed

    async def _begin_stop_without_active_plan(
        self,
        session: AsyncSession,
        run_id: str,
        run: AgentRunModel,
        plan: AgentPlanModel | None,
        now: datetime,
    ) -> RunCancellationResult:
        """Set the stop fence and revoke pending work while retaining the pointer."""
        now_iso = now.isoformat()
        external_effect_status = (
            "unknown" if run.status == RunStatus.RUNNING.value else "none"
        )
        run.status = RunStatus.STOPPING.value
        run.cancellation_epoch = int(run.cancellation_epoch or 0) + 1
        run.cancellation_external_effect_status = external_effect_status
        run.updated_at = now_iso
        await self._cancel_root_resume_jobs(session, run_id, now)
        await self._cancel_pending_approvals(session, run_id=run_id, plan=None, now=now)
        session.add(
            OrchestrationOutboxModel(
                message_id=uuid4().hex,
                dedupe_key=f"run:{run_id}:stopping:{run.cancellation_epoch}",
                event_type="RunStopping",
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
        return RunCancellationResult(
            RunStatus.STOPPING,
            True,
            external_effect_status,
            run_id=run_id,
        )

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
        run.status = RunStatus.STOPPING.value
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
            ("RunStopping", f"run:{run_id}"),
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
        return RunCancellationResult(
            RunStatus.STOPPING,
            True,
            external_effect_status,
            run_id=run_id,
        )

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

    async def _enqueue_memory_job(
        self,
        session: AsyncSession,
        run: AgentRunModel,
        *,
        assistant_text: str,
        now_iso: str,
    ) -> None:
        """在 Run 终态事务内排队记忆作业并冻结当时的项目绑定。"""
        request = _run_start_payload_or_none(getattr(run, "request_json", "{}"))
        user_text = request.message.strip() if request is not None else ""
        if not user_text:
            return
        memory_settings = await session.get(SessionMemorySettingsModel, run.session_id)
        if memory_settings is not None and not memory_settings.generate_memories:
            return
        writable, project_id = await ensure_writable_run_scope(
            session,
            session_id=run.session_id,
            turn_id=run.run_id,
            now=now_iso,
        )
        if not writable:
            return
        await session.execute(
            pg_insert(MemoryProcessingJobModel)
            .values(
                job_id=f"memjob_{uuid4().hex}",
                turn_id=run.run_id,
                session_id=run.session_id,
                user_text=user_text,
                assistant_text=assistant_text,
                status="queued",
                attempt=0,
                available_at=now_iso,
                created_at=now_iso,
                updated_at=now_iso,
            )
            .on_conflict_do_nothing(index_elements=["turn_id"])
        )

    async def _finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        response: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
        owner: str | None = None,
        generation: int | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        async with self._session_factory() as session:
            async with session.begin():
                session_id = await session.scalar(
                    select(AgentRunModel.session_id).where(
                        AgentRunModel.run_id == run_id
                    )
                )
                if session_id is None:
                    return False
                session_row = await session.scalar(
                    select(SessionModel)
                    .where(SessionModel.id == session_id)
                    .with_for_update()
                )
                run = await session.get(AgentRunModel, run_id, with_for_update=True)
                allowed_states = (
                    {RunStatus.RUNNING.value}
                    if status == RunStatus.COMPLETED
                    else {RunStatus.RUNNING.value, RunStatus.STOPPING.value}
                )
                if (
                    session_row is None
                    or run is None
                    or session_row.active_run_id != run_id
                    or run.status not in allowed_states
                    or not self._owns_execution_lease(run, owner, generation, datetime.now(timezone.utc))
                ):
                    return False
                run.status = status.value
                run.updated_at = now_iso
                run.execution_lease_owner = None
                run.execution_lease_until = None
                if response is not None:
                    run.response_json = json.dumps(response, ensure_ascii=False)
                if error is not None:
                    run.error_json = json.dumps(error, ensure_ascii=False)
                session_row.active_run_id = None
                changed = True
                if changed:
                    await session.execute(
                        update(RootResumeJobModel)
                        .where(
                            RootResumeJobModel.run_id == run_id,
                            RootResumeJobModel.status.in_(("pending", "running")),
                        )
                        .values(
                            status=(
                                "cancelled" if status == RunStatus.CANCELLED else "completed"
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
                            content = _final_answer_text(response)
                            user_message_id = request.message_id if request else ""
                            await self._enqueue_memory_job(
                                session,
                                run,
                                assistant_text=content,
                                now_iso=now_iso,
                            )
                            if user_message_id and content:
                                assistant_message_id = f"assistant:{run_id}"
                                stream = _answer_stream_metadata(response)
                                if stream["message_id"] != assistant_message_id:
                                    raise ValueError(
                                        "answer_stream_message_id_mismatch"
                                    )
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
                                session_row.updated_at = now_iso
                                if (
                                    session_row.last_activity_at is None
                                    or now_iso > session_row.last_activity_at
                                ):
                                    session_row.last_activity_at = now_iso
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
                    elif status == RunStatus.FAILED:
                        run = await session.get(AgentRunModel, run_id)
                        if run is not None:
                            await self._enqueue_memory_job(
                                session,
                                run,
                                assistant_text="",
                                now_iso=now_iso,
                            )
                            self._add_completion_outbox(
                                session,
                                run=run,
                                message_id=f"run-final:{run_id}:failed",
                                event_type="RunFailed",
                                dedupe_key=f"run:{run_id}:failed",
                                payload_json={"error": error or {"code": "run_failed"}},
                                now=now,
                            )
        return changed

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
            row.message_id,
            row.sequence,
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
