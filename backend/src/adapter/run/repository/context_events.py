"""Run 上下文消息、压缩摘要、用量记录与事件流查询的 PostgreSQL 组合式 Store。"""

from __future__ import annotations

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.message.converter import conversation_to_domain, execution_to_domain
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
from adapter.run.repository._subagent_helpers import load_subagent_details_in_session
from adapter.run.repository._tx_helpers import (
    append_event,
    get_visible_run,
    lease_is_valid,
    lock_worker_run,
    lock_worker_run_only,
    merge_usage,
    sync_llm_request_count,
    work_deadline_is_valid,
)
from common.date import utc_now
from domain.message import (
    ConversationMessage,
    ExecutionMessage,
    ExecutionRole,
    FileRefPart,
    MessagePart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from domain.run.model import (
    MAX_RUN_SSE_SNAPSHOT_BYTES,
    ContextSummary,
    PendingSubagentApproval,
    PendingSubagentInput,
    RecordRunUsageCommand,
    Run,
    RunAttemptUsage,
    RunCompletedPayload,
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
    SubagentTaskSummary,
)
from domain.skill import SKILL_LOAD_TOOL_ID
from domain.tool import ToolCallStatus, ToolOutcome, ToolOutcomeStatus

_STREAM_SNAPSHOT_MESSAGES = 8
_STREAM_SNAPSHOT_EVENTS = 100
_STREAM_SNAPSHOT_BYTES = MAX_RUN_SSE_SNAPSHOT_BYTES
_STREAM_SNAPSHOT_PREFIX_RESERVE = 128
_RUN_EVENT_ADAPTER: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)


def _stream_message_parts(parts: tuple[MessagePart, ...]) -> tuple[dict[str, object], ...]:
    """将消息内容块投影为与 GET /api/v1/sessions/{id}/messages 一致的 JSON 结构。"""
    projected: list[dict[str, object]] = []
    for part in parts:
        if isinstance(part, TextPart):
            projected.append({"type": "text", "text": part.text})
        elif isinstance(part, FileRefPart):
            projected.append(
                {
                    "type": "file_ref",
                    "file_id": part.file_id,
                    "filename": part.filename,
                    "mime_type": part.mime_type,
                    "size_bytes": part.size_bytes,
                }
            )
        elif isinstance(part, ToolCallPart):
            projected.append(
                {
                    "type": "tool_call",
                    "tool_call_id": part.tool_call_id,
                    "name": part.name,
                    "arguments": part.arguments,
                }
            )
        else:
            projected.append(
                {
                    "type": "tool_result",
                    "tool_call_id": part.tool_call_id,
                    "status": part.status.value,
                    "output": part.output,
                    "artifacts": [
                        {
                            "file_id": artifact.file_id,
                            "filename": artifact.filename,
                            "label": artifact.label,
                            "mime_type": artifact.mime_type,
                            "size_bytes": artifact.size_bytes,
                        }
                        for artifact in part.artifacts
                    ],
                }
            )
    return tuple(projected)


class RunContextEventStore:
    """负责上下文消息组装、压缩摘要、用量审计与 SSE 快照查询的独立 Store。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存数据库会话工厂。"""
        self._session_factory = session_factory

    async def record_usage(self, command: RecordRunUsageCommand) -> Run | None:
        """按执行 epoch 和调用序号幂等累加已知 usage；仅锁 runs 单行，不阻塞父表 sessions。"""
        if command.attempt < 1:
            raise ValueError("run_usage_attempt_invalid")
        async with self._session_factory() as session:
            async with session.begin():
                # 仅锁 runs 单行并校验租约归属，避免 LLM 流期间写 usage 争抢 sessions 行锁。
                row = await lock_worker_run_only(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or not lease_is_valid(row, command.worker_id, command.execution_epoch, db_now)
                    or not work_deadline_is_valid(row, db_now)
                ):
                    return None
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
                row.usage = merge_usage(row.usage, RunUsage(attempts=(incoming,))).model_dump(
                    mode="json"
                )
                sync_llm_request_count(row, command.llm_request_count)
                result = run_to_domain(row)
        return result

    async def list_context_messages(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[ConversationMessage | ExecutionMessage, ...] | None:
        """读取 Run 固定 cutoff 前的正式消息及本 Run 执行交互。"""
        async with self._session_factory() as session:
            run = await get_visible_run(session, session_id, owner_id, run_id)
            if run is None:
                return None
            # 子 Run 严格上下文隔离：不拉取主会话正式对话历史，仅加载本子 Run 执行消息。
            # 主 Run 上下文也须隔离掉子 Run（depth >= 1）的提问与回答；
            # conversation_messages.run_id 与 context_turn_id 均已收紧为非空，直接内连接过滤即可。
            conversation_rows = (
                ()
                if run.depth >= 1
                else (
                    await session.scalars(
                        select(ConversationMessageModel)
                        .join(
                            RunModel,
                            RunModel.id == ConversationMessageModel.run_id,
                        )
                        .where(
                            ConversationMessageModel.session_id == session_id,
                            ConversationMessageModel.sequence <= run.context_cutoff_sequence,
                            RunModel.depth == 0,
                        )
                        .order_by(ConversationMessageModel.sequence)
                    )
                ).all()
            )
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
        messages: list[ConversationMessage | ExecutionMessage] = [
            *(conversation_to_domain(row) for row in conversation_rows),
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
        result_message_by_tool_call_id = {
            part.tool_call_id: message
            for message in messages
            if isinstance(message, ExecutionMessage) and message.role is ExecutionRole.TOOL
            for part in message.parts
            if isinstance(part, ToolResultPart)
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
            call_parts = [part for part in message.parts if isinstance(part, ToolCallPart)]
            if not call_parts:
                continue
            persisted_results = result_ids_by_call_message.get(message.id, ())
            if persisted_results:
                result_ids = persisted_results
            else:
                result_ids = tuple(
                    result_message.id
                    for call in call_parts
                    if (result_message := result_message_by_tool_call_id.get(call.tool_call_id))
                    is not None
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
                        if isinstance(part, ToolCallPart) and part.tool_call_id == row.tool_call_id
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
            run = await get_visible_run(session, session_id, owner_id, run_id)
            if run is None:
                return None
            if run.depth >= 1:
                return ()
            trigger_sequence = await session.scalar(
                select(ConversationMessageModel.sequence).where(
                    ConversationMessageModel.id == run.trigger_message_id,
                    ConversationMessageModel.session_id == session_id,
                )
            )
            if trigger_sequence is None:
                return ()
            row = await session.scalar(
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
                .limit(1)
            )
        return () if row is None else (context_summary_to_domain(row),)

    async def save_context_summary(
        self, command: SaveContextSummaryCommand
    ) -> tuple[ContextSummary, RunEvent] | None:
        """校验执行权并原子保存最终摘要和完成事件。"""
        if not command.text.strip() or command.covered_through_sequence <= 0:
            raise ValueError("run_context_summary_invalid")
        async with self._session_factory() as session:
            async with session.begin():
                row = await lock_worker_run(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or not lease_is_valid(row, command.worker_id, command.execution_epoch, db_now)
                    or not work_deadline_is_valid(row, db_now)
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
                await append_event(session, row, payload, db_now or command.created_at)
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
                row = await lock_worker_run(session, run_id, worker_id, execution_epoch)
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or not lease_is_valid(row, worker_id, execution_epoch, db_now)
                    or not work_deadline_is_valid(row, db_now)
                ):
                    return None
                await append_event(session, row, payload, db_now or utc_now())
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
        """同一 PostgreSQL MVCC 快照内读取消息、状态和事件游标，事务外执行裁剪序列化。"""
        async with self._session_factory() as session:
            async with session.begin():
                # 步骤 1：设置可重复读隔离级别（SET TRANSACTION ISOLATION LEVEL REPEATABLE READ），
                # 确保 Run、消息、事件与子任务视图处于同一全局一致点，
                # 消除并发写入时的不可重复读与状态错位。
                await session.execute(sql_text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

                # 步骤 2：校验租户所有权与会话软删除状态，拒绝未授权或失效数据访问。
                row = await get_visible_run(session, session_id, owner_id, run_id)
                if row is None:
                    return None

                # 步骤 3：多读一条记录（+1 探针机制），
                # 免去额外 COUNT 查询即可精确判断首屏外是否残留历史消息或未读事件。
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

                # 步骤 4（事务内只读查询）：仅根任务（row.depth == 0）聚合并发 Subagent 任务树及
                # 人工协同（HITL）待审批/待澄清状态，复用共享聚合方法消除重复查库。
                subagent_tasks: tuple[SubagentTaskSummary, ...] = ()
                pending_subagent_inputs: tuple[PendingSubagentInput, ...] = ()
                pending_subagent_approvals: tuple[PendingSubagentApproval, ...] = ()
                if row.depth == 0:
                    (
                        subagent_tasks,
                        pending_subagent_inputs,
                        pending_subagent_approvals,
                    ) = await load_subagent_details_in_session(session, session_id, row.id)

                # 在退出事务与归还连接前完成全部轻量领域对象转换，避免事务外访问 ORM 实体。
                run = run_to_domain(row)
                has_more_messages = len(conversation_rows) > _STREAM_SNAPSHOT_MESSAGES
                messages: list[RunStreamMessage] = []
                for message in reversed(conversation_rows[:_STREAM_SNAPSHOT_MESSAGES]):
                    domain_message = conversation_to_domain(message)
                    messages.append(
                        RunStreamMessage(
                            id=domain_message.id,
                            role=domain_message.role.value,
                            status=domain_message.status.value,
                            text="".join(
                                part.text
                                for part in domain_message.parts
                                if isinstance(part, TextPart)
                            ),
                            sequence=domain_message.sequence,
                            created_at=domain_message.created_at,
                            session_id=domain_message.session_id,
                            run_id=domain_message.run_id,
                            reply_to_message_id=domain_message.reply_to_message_id,
                            parts=_stream_message_parts(domain_message.parts),
                            completed_at=domain_message.completed_at,
                        )
                    )
                has_more_events = len(event_rows) > _STREAM_SNAPSHOT_EVENTS
                events = [event_to_domain(event) for event in event_rows[:_STREAM_SNAPSHOT_EVENTS]]

                # 终态 completed 时在同一一致快照事务内填充 assistant_message_id；
                # 首屏事件已覆盖时直接内存提取，仅超过首屏上限时回查持久化 completed 事件。
                assistant_message_id: str | None = None
                if run.status is RunStatus.COMPLETED:
                    for event in reversed(events):
                        if isinstance(event.payload, RunCompletedPayload):
                            assistant_message_id = event.payload.assistant_message_id
                            break
                    if assistant_message_id is None and has_more_events:
                        completed_event_row = await session.scalar(
                            select(RunEventModel)
                            .where(
                                RunEventModel.run_id == run_id,
                                RunEventModel.type == "completed",
                            )
                            .order_by(RunEventModel.sequence.desc())
                            .limit(1)
                        )
                        if completed_event_row is not None:
                            completed_event = event_to_domain(completed_event_row)
                            if isinstance(completed_event.payload, RunCompletedPayload):
                                assistant_message_id = completed_event.payload.assistant_message_id
                    if assistant_message_id is None and row.depth == 0:
                        assistant_message_id = next(
                            (msg.id for msg in reversed(messages) if msg.role == "assistant"),
                            None,
                        )
        # 事务边界结束：数据库只读快照已完整取出，连接立即归还连接池，
        # 后续 CPU 密集的消息字节预裁剪与 512KB 快照序列化裁剪全部在事务外执行。

        # 步骤 5：事务外单趟字节预算裁剪，确保完整 SSE 快照帧严格 <= 512 KiB。
        messages_before_sequence = messages[0].sequence if has_more_messages and messages else None
        events_after_sequence = events[-1].sequence if has_more_events and events else None
        events_complete = events_after_sequence is None

        envelope_bytes = (
            len(
                RunSnapshotEvent(
                    run_id=run.id,
                    execution_epoch=run.execution_epoch,
                    stream_sequence=9_999_999_999_999_999_999,
                    payload=RunSnapshotPayload(
                        status=run.status,
                        event_sequence=run.event_sequence,
                        report_message_id=run.report_message_id,
                        assistant_message_id=assistant_message_id,
                        messages=(),
                        events=(),
                        messages_complete=False,
                        messages_before_sequence=9_999_999_999_999,
                        events_complete=False,
                        events_after_sequence=9_999_999_999_999,
                        subagent_tasks=subagent_tasks,
                        pending_subagent_inputs=pending_subagent_inputs,
                        pending_subagent_approvals=pending_subagent_approvals,
                    ),
                )
                .model_dump_json(exclude_none=True)
                .encode("utf-8")
            )
            + _STREAM_SNAPSHOT_PREFIX_RESERVE
        )
        budget = _STREAM_SNAPSHOT_BYTES - envelope_bytes
        if budget < 0:
            raise ValueError("run_stream_snapshot_exceeds_size_limit")

        msg_sizes = [
            len(item.model_dump_json(exclude_none=True).encode("utf-8")) + 1 for item in messages
        ]
        evt_sizes = [
            len(_RUN_EVENT_ADAPTER.dump_json(item, exclude_none=True)) + 1 for item in events
        ]
        total_bytes = sum(msg_sizes) + sum(evt_sizes)
        # 优先裁旧消息；仅剩终态消息仍超限时先裁尾部事件保终态回复；单条超大则留空交由前端兜底。
        protected_message_id = assistant_message_id or run.report_message_id
        msg_start = 0
        evt_end = len(events)
        while total_bytes > budget:
            remaining_msgs = len(messages) - msg_start
            if remaining_msgs > 1 or (
                remaining_msgs == 1
                and (messages[msg_start].id != protected_message_id or evt_end == 0)
            ):
                messages_before_sequence = messages[msg_start].sequence + 1
                total_bytes -= msg_sizes[msg_start]
                msg_start += 1
            elif evt_end > 0:
                evt_end -= 1
                total_bytes -= evt_sizes[evt_end]
                events_after_sequence = events[evt_end - 1].sequence if evt_end else 0
                events_complete = False
            else:
                raise ValueError("run_stream_snapshot_exceeds_size_limit")
        messages = messages[msg_start:]
        events = events[:evt_end]

        # 步骤 6：组装并返回不可变快照对象。
        return RunStreamSnapshot(
            run,
            tuple(messages),
            tuple(events),
            messages_complete=messages_before_sequence is None,
            messages_before_sequence=messages_before_sequence,
            events_complete=events_complete,
            events_after_sequence=events_after_sequence,
            subagent_tasks=subagent_tasks,
            pending_subagent_inputs=pending_subagent_inputs,
            pending_subagent_approvals=pending_subagent_approvals,
            assistant_message_id=assistant_message_id,
        )

    async def list_visible_events(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
        through_sequence: int | None = None,
    ) -> tuple[RunEvent, ...] | None:
        """在单数据库会话内合并 Run 可见性校验与事件分页读取，不可见时返回 None。"""
        async with self._session_factory() as session:
            visible = await get_visible_run(session, session_id, owner_id, run_id)
            if visible is None:
                return None
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
        """复用 list_visible_events 校验可见性并分页读取，不可见时返回空元组。"""
        events = await self.list_visible_events(
            session_id,
            owner_id,
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            through_sequence=through_sequence,
        )
        return () if events is None else events

    async def list_subagent_tasks(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[SubagentTaskSummary, ...]:
        """读取主 Run 下属全部子任务的聚合状态投影。"""
        async with self._session_factory() as session:
            if await get_visible_run(session, session_id, owner_id, parent_run_id) is None:
                return ()
            tasks, _, _ = await load_subagent_details_in_session(
                session,
                session_id,
                parent_run_id,
                include_inputs=False,
                include_approvals=False,
            )
            return tasks

    async def list_pending_subagent_inputs(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[PendingSubagentInput, ...]:
        """读取主 Run 下属子 Run 当前待回答的澄清请求。"""
        async with self._session_factory() as session:
            if await get_visible_run(session, session_id, owner_id, parent_run_id) is None:
                return ()
            _, inputs, _ = await load_subagent_details_in_session(
                session,
                session_id,
                parent_run_id,
                include_tasks=False,
                include_approvals=False,
            )
            return inputs

    async def list_pending_subagent_approvals(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[PendingSubagentApproval, ...]:
        """读取主 Run 下属子 Run 当前待人工审批的工具调用。"""
        async with self._session_factory() as session:
            if await get_visible_run(session, session_id, owner_id, parent_run_id) is None:
                return ()
            _, _, approvals = await load_subagent_details_in_session(
                session,
                session_id,
                parent_run_id,
                include_tasks=False,
                include_inputs=False,
            )
            return approvals
