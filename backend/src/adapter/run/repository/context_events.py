"""Run 上下文消息、压缩摘要、用量记录与事件流查询的 PostgreSQL 子仓储。"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Literal, cast

from sqlalchemy import func, select
from sqlalchemy import text as sql_text

from adapter.message.converter.message import conversation_to_domain, execution_to_domain
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.run.converter import (
    context_summary_to_domain,
    event_to_domain,
    run_to_domain,
)
from adapter.run.model import (
    RunEventModel,
    RunModel,
    RunToolCallModel,
    SessionContextSummaryModel,
)
from adapter.run.repository._shared import RunRepositorySupportMixin
from adapter.session.model import SessionModel
from common.ids import now_utc
from domain.message import (
    ConversationMessage,
    ExecutionMessage,
    ExecutionRole,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from domain.run.model import (
    MAX_RUN_SSE_SNAPSHOT_BYTES,
    ContextSummary,
    RecordRunUsageCommand,
    Run,
    RunAttemptUsage,
    RunCompressionCompletedPayload,
    RunEvent,
    RunEventPayload,
    RunSnapshotEvent,
    RunSnapshotPayload,
    RunStatus,
    RunStreamMessage,
    RunStreamSnapshot,
    RunUsage,
    SaveContextSummaryCommand,
)
from domain.run.model.errors import (
    RunConflictError,
)
from domain.skill import (
    SKILL_LOAD_TOOL_ID,
)
from domain.tool import (
    ToolCallStatus,
    ToolOutcome,
    ToolOutcomeStatus,
)

_ACTIVE_STATUSES = (
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_FOR_INPUT.value,
    RunStatus.WAITING_FOR_APPROVAL.value,
    RunStatus.CANCELLING.value,
)
_STREAM_SNAPSHOT_MESSAGES = 8
_STREAM_SNAPSHOT_EVENTS = 100
_STREAM_SNAPSHOT_BYTES = MAX_RUN_SSE_SNAPSHOT_BYTES
_STREAM_SNAPSHOT_PREFIX_RESERVE = 128
_MAX_TOOL_BATCH_SIZE = 8
logger = logging.getLogger(__name__)


class RunContextEventsMixin(RunRepositorySupportMixin):
    """负责上下文消息组装、压缩摘要、用量审计与 SSE 快照查询。"""

    async def record_usage(self, command: RecordRunUsageCommand) -> Run | None:
        """按执行 epoch 和调用序号幂等累加已知 usage。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._lock_worker_run(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or not self._lease_is_valid(
                        row, command.worker_id, command.execution_epoch, db_now
                    )
                    or not self._work_deadline_is_valid(row, db_now)
                ):
                    return None
                if command.attempt < 1:
                    raise ValueError("run_usage_attempt_invalid")
                usage = RunUsage.model_validate(row.usage)
                incoming = RunAttemptUsage(
                    execution_epoch=command.execution_epoch,
                    attempt=command.attempt,
                    purpose=command.purpose,
                    operation_id=command.operation_id,
                    batch_index=command.batch_index,
                    model_profile_id=command.model_profile_id,
                    duration_ms=command.duration_ms,
                    input_tokens=command.input_tokens,
                    output_tokens=command.output_tokens,
                    total_tokens=command.total_tokens,
                    complete=command.complete,
                )
                existing = next(
                    (
                        attempt
                        for attempt in usage.attempts
                        if attempt.execution_epoch == incoming.execution_epoch
                        and attempt.purpose == incoming.purpose
                        and attempt.operation_id == incoming.operation_id
                        and attempt.batch_index == incoming.batch_index
                        and attempt.attempt == incoming.attempt
                    ),
                    None,
                )
                if existing is not None and existing != incoming:
                    raise RunConflictError("run_usage_attempt_conflict")
                if existing is None:
                    row.usage = self._merge_usage(
                        row.usage, RunUsage(attempts=(incoming,))
                    ).model_dump(mode="json")
                result = run_to_domain(row)
        return result

    async def list_context_messages(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[ConversationMessage | ExecutionMessage, ...] | None:
        """读取 Run 固定 cutoff 前的正式消息及本 Run 执行交互。"""
        async with self._session_factory() as session:
            run = await session.scalar(
                select(RunModel)
                .join(SessionModel, SessionModel.id == RunModel.session_id)
                .where(
                    RunModel.id == run_id,
                    RunModel.session_id == session_id,
                    RunModel.owner_id == owner_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if run is None:
                return None
            conversation_rows = (
                await session.scalars(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.session_id == session_id,
                        ConversationMessageModel.sequence <= run.context_cutoff_sequence,
                    )
                    .order_by(ConversationMessageModel.sequence)
                )
            ).all()
            execution_rows = (
                await session.scalars(
                    select(ExecutionMessageModel)
                    .where(
                        ExecutionMessageModel.session_id == session_id,
                        ExecutionMessageModel.run_id == run_id,
                    )
                    .order_by(ExecutionMessageModel.sequence)
                )
            ).all()
            tool_call_rows = (
                await session.scalars(
                    select(RunToolCallModel)
                    .where(RunToolCallModel.run_id == run_id)
                    .order_by(RunToolCallModel.ordinal)
                )
            ).all()
            run_trigger_by_id = dict(
                (
                    await session.execute(
                        select(RunModel.id, RunModel.trigger_message_id).where(
                            RunModel.session_id == session_id,
                            RunModel.owner_id == owner_id,
                        )
                    )
                ).all()
            )
        messages: list[ConversationMessage | ExecutionMessage] = [
            *(
                replace(
                    conversation_to_domain(row),
                    context_turn_id=(
                        row.context_turn_id or run_trigger_by_id.get(row.managed_run_id or "")
                    ),
                )
                for row in conversation_rows
            ),
            *(execution_to_domain(row) for row in execution_rows),
        ]
        result_ids_by_call_message = {
            message_id: tuple(
                row.result_message_id
                for row in tool_call_rows
                if row.call_message_id == message_id and row.result_message_id is not None
            )
            for message_id in {row.call_message_id for row in tool_call_rows}
        }
        result_by_protocol_id = {
            result.tool_call_id: message
            for message in messages
            if isinstance(message, ExecutionMessage)
            and message.role is ExecutionRole.TOOL
            for result in message.parts
            if isinstance(result, ToolResultPart)
        }
        ordered: list[ConversationMessage | ExecutionMessage] = []
        consumed_results: set[str] = set()
        for message in sorted(messages, key=lambda item: item.sequence):
            if (
                isinstance(message, ExecutionMessage)
                and message.role is ExecutionRole.TOOL
                and message.id in consumed_results
            ):
                continue
            ordered.append(message)
            if (
                not isinstance(message, ExecutionMessage)
                or message.role is not ExecutionRole.ASSISTANT
            ):
                continue
            call_parts = [
                part for part in message.parts if isinstance(part, ToolCallPart)
            ]
            if not call_parts:
                continue
            persisted_results = result_ids_by_call_message.get(message.id, ())
            if persisted_results:
                result_ids = persisted_results
            else:
                result_ids = tuple(
                    result.id
                    for call in call_parts
                    if (result := result_by_protocol_id.get(call.tool_call_id)) is not None
                )
            for result_id in result_ids:
                result_message = next(
                    (
                        item
                        for item in messages
                        if isinstance(item, ExecutionMessage) and item.id == result_id
                    ),
                    None,
                )
                if result_message is None or result_message.id in consumed_results:
                    continue
                ordered.append(result_message)
                consumed_results.add(result_message.id)
        return tuple(ordered)

    async def list_successfully_loaded_skill_ids(self, run_id: str) -> tuple[str, ...]:
        """扫描全部已提交 skill_load，不受摘要 cutoff 或分页窗口影响。"""
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunToolCallModel)
                    .where(
                        RunToolCallModel.run_id == run_id,
                        RunToolCallModel.tool_id == SKILL_LOAD_TOOL_ID,
                        RunToolCallModel.status == ToolCallStatus.COMPLETED.value,
                    )
                    .order_by(RunToolCallModel.ordinal)
                )
            ).all()
            loaded: list[str] = []
            seen: set[str] = set()
            for row in rows:
                if row.outcome is None:
                    continue
                outcome = ToolOutcome.model_validate(row.outcome)
                if outcome.status is not ToolOutcomeStatus.OK:
                    continue
                message = await session.get(ExecutionMessageModel, row.call_message_id)
                if message is None:
                    raise ValueError("skill_load_call_message_missing")
                call = next(
                    (
                        part
                        for part in execution_to_domain(message).parts
                        if isinstance(part, ToolCallPart)
                        and part.tool_call_id == row.tool_call_id
                    ),
                    None,
                )
                skill_id = None if call is None else call.arguments.get("skill_id")
                if not isinstance(skill_id, str) or not skill_id:
                    raise ValueError("skill_load_call_arguments_invalid")
                if skill_id not in seen:
                    seen.add(skill_id)
                    loaded.append(skill_id)
        return tuple(loaded)

    async def list_context_summaries(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[ContextSummary, ...] | None:
        """读取 cutoff 兼容的摘要版本；不可见 Run 返回 None。"""
        async with self._session_factory() as session:
            run = await session.scalar(
                select(RunModel)
                .join(SessionModel, SessionModel.id == RunModel.session_id)
                .where(
                    RunModel.id == run_id,
                    RunModel.session_id == session_id,
                    RunModel.owner_id == owner_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if run is None:
                return None
            trigger_sequence = await session.scalar(
                select(ConversationMessageModel.sequence).where(
                    ConversationMessageModel.id == run.trigger_message_id,
                    ConversationMessageModel.session_id == session_id,
                )
            )
            if trigger_sequence is None:
                return ()
            rows: list[SessionContextSummaryModel] = []
            offset = 0
            while True:
                page = (
                    await session.scalars(
                        select(SessionContextSummaryModel)
                        .where(
                            SessionContextSummaryModel.session_id == session_id,
                            SessionContextSummaryModel.owner_id == owner_id,
                            SessionContextSummaryModel.covered_through_sequence < trigger_sequence,
                        )
                        .order_by(
                            SessionContextSummaryModel.covered_through_sequence.desc(),
                            SessionContextSummaryModel.created_at.desc(),
                            SessionContextSummaryModel.id.desc(),
                        )
                        .offset(offset)
                        .limit(100)
                    )
                ).all()
                rows.extend(page)
                if len(page) < 100:
                    break
                offset += len(page)
        return tuple(context_summary_to_domain(row) for row in rows)

    async def save_context_summary(
        self, command: SaveContextSummaryCommand
    ) -> tuple[ContextSummary, RunEvent] | None:
        """校验执行权并原子保存最终摘要和完成事件。"""
        if not command.text.strip() or command.covered_through_sequence <= 0:
            raise ValueError("run_context_summary_invalid")
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._lock_worker_run(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or not self._lease_is_valid(
                        row, command.worker_id, command.execution_epoch, db_now
                    )
                    or not self._work_deadline_is_valid(row, db_now)
                ):
                    return None
                existing = await session.scalar(
                    select(SessionContextSummaryModel).where(
                        SessionContextSummaryModel.run_id == command.run_id,
                        SessionContextSummaryModel.operation_id == command.operation_id,
                    )
                )
                if existing is not None:
                    event_row = await session.scalar(
                        select(RunEventModel).where(
                            RunEventModel.run_id == command.run_id,
                            RunEventModel.type == "compression_completed",
                            RunEventModel.payload["operation_id"].as_string()
                            == command.operation_id,
                        )
                    )
                    if event_row is None:
                        raise RuntimeError("run_compression_event_missing")
                    return context_summary_to_domain(existing), event_to_domain(event_row)
                trigger_sequence = await session.scalar(
                    select(ConversationMessageModel.sequence).where(
                        ConversationMessageModel.id == row.trigger_message_id,
                        ConversationMessageModel.session_id == row.session_id,
                    )
                )
                if trigger_sequence is None or command.covered_through_sequence >= trigger_sequence:
                    raise ValueError("run_context_summary_boundary_invalid")
                if command.parent_summary_id is not None:
                    parent = await session.scalar(
                        select(SessionContextSummaryModel).where(
                            SessionContextSummaryModel.id == command.parent_summary_id,
                            SessionContextSummaryModel.session_id == row.session_id,
                            SessionContextSummaryModel.owner_id == row.owner_id,
                        )
                    )
                    if (
                        parent is None
                        or parent.covered_through_sequence > command.covered_through_sequence
                    ):
                        raise ValueError("run_context_summary_parent_invalid")
                boundary_exists = await session.scalar(
                    select(ConversationMessageModel.id).where(
                        ConversationMessageModel.session_id == row.session_id,
                        ConversationMessageModel.sequence == command.covered_through_sequence,
                    )
                )
                if boundary_exists is None:
                    raise ValueError("run_context_summary_boundary_invalid")
                summary_row = SessionContextSummaryModel(
                    id=command.summary_id,
                    session_id=row.session_id,
                    owner_id=row.owner_id,
                    covered_through_sequence=command.covered_through_sequence,
                    parent_summary_id=command.parent_summary_id,
                    text=command.text,
                    run_id=row.id,
                    execution_epoch=command.execution_epoch,
                    operation_id=command.operation_id,
                    policy_version_id=command.policy_version_id,
                    compression_model_version_id=command.compression_model_version_id,
                    prompt_version=command.prompt_version,
                    compression_prompt_version_id=row.compression_prompt_version_id,
                    created_at=db_now or command.created_at,
                )
                session.add(summary_row)
                payload = RunCompressionCompletedPayload(
                    operation_id=command.operation_id,
                    summary_id=command.summary_id,
                    covered_through_sequence=command.covered_through_sequence,
                    batch_count=command.batch_count,
                )
                await self._append_event(session, row, payload, db_now or command.created_at)
                await session.flush()
                event_row = await session.scalar(
                    select(RunEventModel).where(
                        RunEventModel.run_id == row.id,
                        RunEventModel.sequence == row.event_sequence,
                    )
                )
                if event_row is None:
                    raise RuntimeError("run_compression_event_missing")
                return context_summary_to_domain(summary_row), event_to_domain(event_row)

    async def append_compression_event(
        self, run_id: str, worker_id: str, execution_epoch: int, payload: RunEventPayload
    ) -> RunEvent | None:
        """只在当前执行租约有效时保存压缩状态事件。"""
        if payload.type not in {"compression_started", "compression_failed"}:
            raise ValueError("run_compression_event_invalid")
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._lock_worker_run(session, run_id, worker_id, execution_epoch)
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or not self._lease_is_valid(row, worker_id, execution_epoch, db_now)
                    or not self._work_deadline_is_valid(row, db_now)
                ):
                    return None
                await self._append_event(session, row, payload, db_now or now_utc())
                event_row = await session.scalar(
                    select(RunEventModel).where(
                        RunEventModel.run_id == row.id,
                        RunEventModel.sequence == row.event_sequence,
                    )
                )
                return None if event_row is None else event_to_domain(event_row)

    async def get_stream_snapshot(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunStreamSnapshot | None:
        """同一 PostgreSQL MVCC 快照内读取正式消息、状态和事件游标。"""
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(sql_text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                row = await session.scalar(
                    select(RunModel)
                    .join(SessionModel, SessionModel.id == RunModel.session_id)
                    .where(
                        RunModel.id == run_id,
                        RunModel.session_id == session_id,
                        RunModel.owner_id == owner_id,
                        SessionModel.owner_id == owner_id,
                        SessionModel.deleted_time.is_(None),
                    )
                )
                if row is None:
                    return None
                conversation_rows = (
                    await session.scalars(
                        select(ConversationMessageModel)
                        .where(
                            ConversationMessageModel.session_id == session_id,
                            ConversationMessageModel.run_id == run_id,
                        )
                        .order_by(ConversationMessageModel.sequence.desc())
                        .limit(_STREAM_SNAPSHOT_MESSAGES + 1)
                    )
                ).all()
                event_rows = (
                    await session.scalars(
                        select(RunEventModel)
                        .where(RunEventModel.run_id == run_id)
                        .order_by(RunEventModel.sequence)
                        .limit(_STREAM_SNAPSHOT_EVENTS + 1)
                    )
                ).all()
                messages = list(
                    RunStreamMessage(
                        id=message.id,
                        role=cast(Literal["user", "assistant"], message.role),
                        status=message.status,
                        text="".join(
                            part.text
                            for part in conversation_to_domain(message).parts
                            if isinstance(part, TextPart)
                        ),
                        sequence=message.sequence,
                        created_at=message.created_at,
                    )
                    for message in reversed(conversation_rows[:_STREAM_SNAPSHOT_MESSAGES])
                )
                messages_before_sequence = (
                    messages[0].sequence
                    if len(conversation_rows) > _STREAM_SNAPSHOT_MESSAGES
                    else None
                )
                while (
                    messages
                    and sum(len(message.model_dump_json().encode("utf-8")) for message in messages)
                    > _STREAM_SNAPSHOT_BYTES
                ):
                    removed = messages.pop(0)
                    messages_before_sequence = removed.sequence + 1
                events = list(
                    event_to_domain(event) for event in event_rows[:_STREAM_SNAPSHOT_EVENTS]
                )
                events_after_sequence = (
                    events[-1].sequence if len(event_rows) > _STREAM_SNAPSHOT_EVENTS else None
                )
                events_complete = events_after_sequence is None

                # 预算覆盖完整 JSON envelope 与 SSE event/id/data 前缀；超出时只裁掉
                # 可由既有消息/事件分页接口恢复的首屏内容，并同步推进排他游标。
                while True:
                    payload = RunSnapshotPayload(
                        status=RunStatus(row.status),
                        event_sequence=row.event_sequence,
                        report_message_id=row.report_message_id,
                        messages=tuple(messages),
                        events=tuple(events),
                        messages_complete=messages_before_sequence is None,
                        messages_before_sequence=messages_before_sequence,
                        events_complete=events_complete,
                        events_after_sequence=events_after_sequence,
                    )
                    encoded = (
                        RunSnapshotEvent(
                            run_id=row.id,
                            execution_epoch=row.execution_epoch,
                            stream_sequence=9_999_999_999_999_999_999,
                            payload=payload,
                        )
                        .model_dump_json(exclude_none=True)
                        .encode("utf-8")
                    )
                    if len(encoded) + _STREAM_SNAPSHOT_PREFIX_RESERVE <= _STREAM_SNAPSHOT_BYTES:
                        break
                    if messages:
                        removed = messages.pop(0)
                        messages_before_sequence = removed.sequence + 1
                    elif events:
                        events.pop()
                        events_after_sequence = events[-1].sequence if events else 0
                        events_complete = False
                    else:
                        raise ValueError("run_stream_snapshot_exceeds_size_limit")
                snapshot = RunStreamSnapshot(
                    run_to_domain(row),
                    tuple(messages),
                    tuple(events),
                    messages_complete=messages_before_sequence is None,
                    messages_before_sequence=messages_before_sequence,
                    events_complete=events_complete,
                    events_after_sequence=events_after_sequence,
                )
        return snapshot

    async def list_events(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
        through_sequence: int | None = None,
    ) -> tuple[RunEvent, ...]:
        """先校验 owner/Session 可见性，再按 Run 内序号分页读取。"""
        async with self._session_factory() as session:
            visible = await session.scalar(
                select(RunModel.id)
                .join(SessionModel, SessionModel.id == RunModel.session_id)
                .where(
                    RunModel.id == run_id,
                    RunModel.session_id == session_id,
                    RunModel.owner_id == owner_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if visible is None:
                return ()
            rows = (
                await session.scalars(
                    select(RunEventModel)
                    .where(
                        RunEventModel.run_id == run_id,
                        RunEventModel.session_id == session_id,
                        RunEventModel.sequence > after_sequence,
                        *(
                            (RunEventModel.sequence <= through_sequence,)
                            if through_sequence is not None
                            else ()
                        ),
                    )
                    .order_by(RunEventModel.sequence)
                    .limit(limit)
                )
            ).all()
        return tuple(event_to_domain(row) for row in rows)

