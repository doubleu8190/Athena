"""Run 原子命令的 PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Literal, cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.file.model import SessionFileModel
from adapter.file.repository import (
    count_published_session_files,
    lock_session_file_quota,
    pending_file_to_model,
)
from adapter.llm.model.profile import (
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.message.converter.message import conversation_to_domain, execution_to_domain
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.run.converter import (
    context_summary_to_domain,
    event_payload_to_json,
    event_to_domain,
    input_request_to_domain,
    run_to_domain,
    tool_call_to_domain,
)
from adapter.run.model import (
    RunEventModel,
    RunInputRequestModel,
    RunModel,
    RunRequestModel,
    RunToolCallModel,
    SessionContextSummaryModel,
)
from adapter.session.model import SessionModel
from adapter.skill.model import SkillExecutionModel
from adapter.skill.repository import build_run_skill_snapshot
from adapter.system_prompt.repository import current_system_prompts
from common.ids import new_id, now_utc
from domain.file import PendingSessionFile
from domain.memory import ExtractionStatus, MemoryRunWakeupPort
from domain.memory.lifecycle import RunMemoryContextState
from domain.message import (
    ArtifactFileRef,
    ConversationMessage,
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessage,
    ExecutionMessageDraft,
    ExecutionRole,
    FileRefPart,
    MessageMetadata,
    MessageStatus,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import (
    MAX_RUN_SSE_SNAPSHOT_BYTES,
    AnswerRunCommand,
    ContextSummary,
    FailRunCommand,
    FinishRunCommand,
    InitializeRunMemoryContextCommand,
    InputRequestStatus,
    RecordRunUsageCommand,
    RequestRunInputCommand,
    Run,
    RunApprovalResumedPayload,
    RunAttemptUsage,
    RunCancelledPayload,
    RunCancelRequestedPayload,
    RunCompletedPayload,
    RunCompressionCompletedPayload,
    RunEvent,
    RunEventPayload,
    RunFailedPayload,
    RunInputCall,
    RunInputRequest,
    RunLease,
    RunQueuedPayload,
    RunRequestOperation,
    RunRequestResult,
    RunResumedPayload,
    RunSnapshotEvent,
    RunSnapshotPayload,
    RunStartedPayload,
    RunStatus,
    RunStreamMessage,
    RunStreamSnapshot,
    RunToolApprovalDecidedPayload,
    RunToolCallFinishedPayload,
    RunToolCallStartedPayload,
    RunUsage,
    RunWaitingForApprovalPayload,
    RunWaitingPayload,
    SaveContextSummaryCommand,
    SubmitRunCommand,
    SubmitRunResult,
)
from domain.run.model.errors import (
    RunConflictError,
    RunInputResourceError,
    RunNotFoundError,
    RunStateError,
    ToolRetryConfirmationRequiredError,
)
from domain.run.ports import RunRepository
from domain.skill import (
    SKILL_EXECUTE_TOOL_ID,
    SKILL_LOAD_TOOL_ID,
    RunSkillSnapshot,
    SafeSkillApprovalProjection,
    SkillExecutionArtifact,
    SkillExecutionCompletion,
    SkillExecutionStatus,
)
from domain.tool import (
    AdmitToolCallCommand,
    BeginToolCallBatchCommand,
    BeginToolCallBatchResult,
    BeginToolCallCommand,
    BeginToolCallResult,
    ConvergeToolBatchCommand,
    DecideToolApprovalCommand,
    FinishToolCallCommand,
    InitializeRunToolsCommand,
    PendingToolApproval,
    ReserveLlmRequestCommand,
    RuntimeToolSnapshot,
    RunToolBatch,
    RunToolBatchCall,
    RunToolCallDetails,
    RunToolCallPage,
    RunToolCallRecord,
    ToolApprovalDecision,
    ToolCallStatus,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRetryContext,
    bounded_outcome,
    persisted_status_for_outcome,
    protocol_status_for_outcome,
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


def _purpose_total(attempts: tuple[RunAttemptUsage, ...], purpose: str) -> int | None:
    """按调用用途汇总已知 Token 数。"""
    values = [
        item.total_tokens
        for item in attempts
        if item.purpose == purpose and item.total_tokens is not None
    ]
    return None if not values else sum(values)


class SqlAlchemyRunRepository(RunRepository):
    """按 Session → Run → 输入请求顺序锁定并写入事件化状态。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        message_repository: SqlAlchemyMessageRepository,
        *,
        memory_wakeup: MemoryRunWakeupPort | None = None,
        memory_lifecycle_enabled: bool = False,
    ) -> None:
        """保存共享数据库工厂和消息事务内部写入端口。"""
        self._session_factory = session_factory
        self._messages = message_repository
        self._memory_wakeup = memory_wakeup
        self._memory_lifecycle_enabled = memory_lifecycle_enabled

    async def submit(
        self, session_id: str, owner_id: str, command: SubmitRunCommand
    ) -> SubmitRunResult:
        """原子写触发消息、Run、queued event 及 submit 幂等记录。"""
        async with self._session_factory() as session:
            async with session.begin():
                locked_session = await self._lock_session(session, session_id, owner_id)
                replay = await self._find_request(
                    session, owner_id, session_id, RunRequestOperation.SUBMIT, command.request_id
                )
                if replay is not None:
                    self._check_fingerprint(replay, command.fingerprint)
                    run, message = await self._load_submit_result(session, replay)
                    result = SubmitRunResult(run, message, True)
                else:
                    skill_snapshot = await build_run_skill_snapshot(
                        session,
                        command.selected_skill_ids,
                    )
                    input_file_ids = await self._validated_input_file_ids(
                        session,
                        session_id,
                        owner_id,
                        existing=(),
                        incoming=command.input_files.file_ids,
                    )
                    if (
                        command.message.session_id != session_id
                        or command.message.owner_id != owner_id
                        or command.message.run_id != command.run_id
                        or command.message.role is not ConversationRole.USER
                    ):
                        raise RunStateError("run_trigger_message_invalid")
                    active = await session.scalar(
                        select(RunModel.id).where(
                            RunModel.session_id == session_id,
                            RunModel.status.in_(_ACTIVE_STATUSES),
                        )
                    )
                    if active is not None:
                        raise RunConflictError("run_already_active")
                    profile_id = await self._lock_default_profile(session)
                    compression_policy_id = await self._current_compression_policy_id(session)
                    system_prompts = await current_system_prompts(session)
                    if system_prompts is None:
                        raise RunStateError("system_prompt_unavailable")
                    message = await self._messages.append_conversation_in_session(
                        session,
                        command.message,
                        locked_session=locked_session,
                    )
                    created_at = await self._next_run_created_at(session, session_id)
                    current = now_utc()
                    row = RunModel(
                        id=command.run_id,
                        session_id=session_id,
                        owner_id=owner_id,
                        status=RunStatus.QUEUED.value,
                        trigger_message_id=message.id,
                        context_cutoff_sequence=locked_session.last_message_sequence,
                        retry_of_run_id=None,
                        model_config_version_id=profile_id,
                        main_prompt_version_id=system_prompts.main.id,
                        compression_prompt_version_id=system_prompts.compression.id,
                        compression_policy_version_id=compression_policy_id,
                        skill_snapshot=skill_snapshot.model_dump(mode="json"),
                        input_file_ids=list(input_file_ids),
                        memory_project_id=locked_session.project_id,
                        memory_input_message_ids=[message.id],
                        prompt_template_version=command.prompt_template_version,
                        event_sequence=0,
                        execution_epoch=0,
                        auth_session_id=command.auth_session_id,
                        llm_request_count=0,
                        queued_at=command.queued_at,
                        created_at=created_at,
                        updated_at=max(current, created_at),
                        usage=RunUsage().model_dump(mode="json"),
                    )
                    session.add(row)
                    await session.flush()
                    message_row = await session.get(ConversationMessageModel, message.id)
                    if message_row is None:
                        raise RuntimeError("run_trigger_message_missing")
                    message_row.managed_run_id = command.run_id
                    await self._append_event(
                        session,
                        row,
                        RunQueuedPayload(trigger_message_id=message.id),
                        command.queued_at,
                    )
                    await self._store_request(
                        session,
                        owner_id,
                        session_id,
                        RunRequestOperation.SUBMIT,
                        command.request_id,
                        command.fingerprint,
                        RunRequestResult(run_id=row.id, message_id=message.id),
                        current,
                    )
                    result = SubmitRunResult(run_to_domain(row), message, False)
        return result

    async def answer(self, session_id: str, owner_id: str, command: AnswerRunCommand) -> Run:
        """原子保存回答和协议结果，再将 waiting Run 重新排队。"""
        async with self._session_factory() as session:
            async with session.begin():
                locked_session = await self._lock_session(session, session_id, owner_id)
                row = await self._lock_run(session, session_id, command.run_id)
                replay = await self._find_request(
                    session, owner_id, session_id, RunRequestOperation.ANSWER, command.request_id
                )
                if replay is not None:
                    self._check_fingerprint(replay, command.fingerprint)
                    run_id = RunRequestResult.model_validate(replay.result).run_id
                    replay_row = await session.scalar(
                        select(RunModel).where(
                            RunModel.id == run_id,
                            RunModel.session_id == session_id,
                        )
                    )
                    if replay_row is None:
                        raise RunNotFoundError("run_not_found")
                    result = run_to_domain(replay_row)
                else:
                    if (
                        command.answer_message.session_id != session_id
                        or command.answer_message.owner_id != owner_id
                        or command.answer_message.run_id != row.id
                        or command.answer_message.role is not ConversationRole.USER
                        or command.execution_message.session_id != session_id
                        or command.execution_message.owner_id != owner_id
                        or command.execution_message.run_id != row.id
                        or command.execution_message.role is not ExecutionRole.TOOL
                    ):
                        raise RunStateError("run_answer_messages_invalid")
                    input_file_ids = await self._validated_input_file_ids(
                        session,
                        session_id,
                        owner_id,
                        existing=tuple(row.input_file_ids),
                        incoming=command.input_files.file_ids,
                    )
                    request = await session.scalar(
                        select(RunInputRequestModel)
                        .where(
                            RunInputRequestModel.id == command.input_request_id,
                            RunInputRequestModel.run_id == row.id,
                            RunInputRequestModel.session_id == session_id,
                        )
                        .with_for_update()
                    )
                    if row.status != RunStatus.WAITING_FOR_INPUT.value or request is None:
                        raise RunStateError("run_input_request_not_pending")
                    if request.status != InputRequestStatus.PENDING.value:
                        raise RunStateError("run_input_request_not_pending")
                    input_call = await self._input_call_for_question(
                        session,
                        row.id,
                        session_id,
                        request.question_message_id,
                    )
                    result_part = (
                        command.execution_message.parts[0]
                        if len(command.execution_message.parts) == 1
                        else None
                    )
                    if (
                        input_call is None
                        or command.execution_message.tool_call_id != input_call.tool_call_id
                        or command.execution_message.thread_id != input_call.thread_id
                        or not isinstance(result_part, ToolResultPart)
                        or result_part.tool_call_id != input_call.tool_call_id
                    ):
                        raise RunStateError("run_input_call_reference_invalid")
                    answer = await self._messages.append_conversation_in_session(
                        session,
                        command.answer_message,
                        locked_session=locked_session,
                        managed_run_id=row.id,
                    )
                    await self._messages.append_execution_in_session(
                        session,
                        command.execution_message,
                        locked_session=locked_session,
                        managed_run_id=row.id,
                    )
                    request.status = InputRequestStatus.ANSWERED.value
                    request.answer_message_id = answer.id
                    request.answered_at = command.queued_at
                    if command.auth_session_id is not None:
                        row.auth_session_id = command.auth_session_id
                    row.status = RunStatus.QUEUED.value
                    row.input_file_ids = list(input_file_ids)
                    if answer.id not in row.memory_input_message_ids:
                        row.memory_input_message_ids = [
                            *row.memory_input_message_ids,
                            answer.id,
                        ]
                    row.queued_at = command.queued_at
                    row.worker_id = None
                    row.lease_expires_at = None
                    row.execution_started_at = None
                    row.execution_deadline_at = None
                    row.updated_at = command.queued_at
                    await self._append_event(
                        session,
                        row,
                        RunResumedPayload(
                            input_request_id=request.id,
                            answer_message_id=answer.id,
                        ),
                        command.queued_at,
                    )
                    await self._store_request(
                        session,
                        owner_id,
                        session_id,
                        RunRequestOperation.ANSWER,
                        command.request_id,
                        command.fingerprint,
                        RunRequestResult(
                            run_id=row.id,
                            message_id=answer.id,
                            input_request_id=request.id,
                        ),
                        command.queued_at,
                    )
                    result = run_to_domain(row)
        return result

    async def claim(
        self, worker_id: str, *, lease_seconds: int, execution_seconds: int, limit: int
    ) -> tuple[RunLease, ...]:
        """先枚举候选，再按 Session → Run 锁序领取并用 DB 时钟设置期限。"""
        if not worker_id or not 1 <= lease_seconds <= 3600 or not 1 <= execution_seconds <= 86400:
            raise ValueError("run_claim_parameters_invalid")
        if not 1 <= limit <= 128:
            raise ValueError("run_claim_limit_invalid")
        async with self._session_factory() as session:
            candidate_ids = (
                await session.scalars(
                    select(RunModel.id)
                    .where(RunModel.status == RunStatus.QUEUED.value)
                    .order_by(RunModel.queued_at, RunModel.id)
                    .limit(limit * 8)
                )
            ).all()
        leases: list[RunLease] = []
        memory_wake_owners: list[str] = []
        for candidate_id in candidate_ids:
            if len(leases) >= limit:
                break
            async with self._session_factory() as session:
                async with session.begin():
                    candidate = await session.scalar(
                        select(RunModel.session_id).where(RunModel.id == candidate_id)
                    )
                    if candidate is None:
                        continue
                    locked_session = await session.scalar(
                        select(SessionModel)
                        .where(
                            SessionModel.id == candidate,
                            SessionModel.deleted_time.is_(None),
                        )
                        .with_for_update(skip_locked=True)
                    )
                    if locked_session is None:
                        continue
                    row = await session.scalar(
                        select(RunModel)
                        .where(
                            RunModel.id == candidate_id,
                            RunModel.session_id == locked_session.id,
                            RunModel.status == RunStatus.QUEUED.value,
                        )
                        .with_for_update()
                    )
                    if row is None:
                        continue
                    profile = await session.scalar(
                        select(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == row.model_config_version_id)
                        .with_for_update()
                    )
                    db_now = await session.scalar(select(func.clock_timestamp()))
                    if db_now is None:
                        raise RuntimeError("run_database_clock_unavailable")
                    if (
                        profile is None
                        or not profile.enabled
                        or not profile.supports_stream
                        or not profile.supports_tools
                    ):
                        row.status = RunStatus.FAILED.value
                        row.safe_error_code = "model_profile_unavailable"
                        row.finished_at = db_now
                        row.updated_at = db_now
                        self._mark_memory_pending(row, db_now)
                        await self._append_event(
                            session,
                            row,
                            RunFailedPayload(safe_error_code="model_profile_unavailable"),
                            db_now,
                        )
                        memory_wake_owners.append(row.owner_id)
                        continue
                    row.status = RunStatus.RUNNING.value
                    row.execution_epoch += 1
                    row.worker_id = worker_id
                    row.execution_started_at = db_now
                    row.first_started_at = row.first_started_at or db_now
                    row.lease_expires_at = db_now + timedelta(seconds=lease_seconds)
                    row.execution_deadline_at = db_now + timedelta(
                        seconds=min(execution_seconds, profile.execution_timeout_seconds)
                    )
                    row.updated_at = db_now
                    await self._append_event(
                        session,
                        row,
                        RunStartedPayload(
                            execution_epoch=row.execution_epoch,
                            worker_id=worker_id,
                        ),
                        db_now,
                    )
                    leases.append(
                        RunLease(
                            run=run_to_domain(row),
                            lease_expires_at=row.lease_expires_at,
                            execution_deadline_at=row.execution_deadline_at,
                        )
                    )
        for owner_id in dict.fromkeys(memory_wake_owners):
            await self._wake_memory(owner_id)
        return tuple(leases)

    async def heartbeat(
        self, run_id: str, worker_id: str, execution_epoch: int, *, lease_seconds: int
    ) -> Run | None:
        """续约仍由当前 Worker 持有的有效租约，包括取消清理阶段。"""
        async with self._session_factory() as session:
            async with session.begin():
                session_id = await session.scalar(
                    select(RunModel.session_id).where(RunModel.id == run_id)
                )
                if session_id is None:
                    return None
                await self._lock_session_by_id(session, session_id)
                row = await self._lock_run(session, session_id, run_id)
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    db_now is None
                    or row.status not in {RunStatus.RUNNING.value, RunStatus.CANCELLING.value}
                    or row.worker_id != worker_id
                    or row.execution_epoch != execution_epoch
                    or row.lease_expires_at is None
                    or row.lease_expires_at <= db_now
                ):
                    return None
                row.lease_expires_at = db_now + timedelta(seconds=lease_seconds)
                row.updated_at = db_now
                return run_to_domain(row)

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

    async def initialize_tools(
        self, command: InitializeRunToolsCommand
    ) -> RuntimeToolSnapshot | None:
        """Freeze the validated snapshot once under the current execution lease."""
        snapshot = RuntimeToolSnapshot.model_validate(command.snapshot.model_dump(mode="json"))
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
                if row.tool_snapshot is None:
                    row.tool_snapshot = snapshot.model_dump(mode="json")
                    row.updated_at = db_now or now_utc()
                    return snapshot
                return RuntimeToolSnapshot.model_validate(row.tool_snapshot)

    async def initialize_memory_context(
        self, command: InitializeRunMemoryContextCommand
    ) -> RunMemoryContextState | None:
        """在当前有效租约下原子固定记忆引用；竞争时只返回先写入的快照。"""
        proposed = RunMemoryContextState.model_validate(
            command.context_state.model_dump(mode="json")
        )
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
                context_state = RunMemoryContextState.model_validate(
                    row.memory_context_state or {}
                )
                if context_state.initialized:
                    return context_state
                if not proposed.initialized:
                    raise ValueError("run_memory_context_state_invalid")
                # 固定上下文独立持久化，后续终态提取不得清空已初始化的空快照。
                row.memory_context_state = proposed.model_dump(mode="json")
                row.updated_at = db_now or now_utc()
                return proposed

    async def reserve_llm_request(self, command: ReserveLlmRequestCommand) -> int | None:
        """Atomically reserve one provider call under a live execution lease."""
        if not 1 <= command.maximum_count <= 40:
            raise ValueError("run_llm_request_limit_invalid")
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
                    or row.llm_request_count >= command.maximum_count
                ):
                    return None
                row.llm_request_count += 1
                row.updated_at = db_now or now_utc()
                return row.llm_request_count

    async def begin_tool_call(self, command: BeginToolCallCommand) -> BeginToolCallResult:
        """Atomically persist a call and return the only execution admission."""
        if (
            not command.tool_call_id
            or len(command.tool_call_id) > 256
            or not command.model_name
            or len(command.model_name) > 256
        ):
            raise ValueError("run_tool_call_identity_invalid")
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._lock_worker_run(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or not self._lease_is_valid(
                        row, command.worker_id, command.execution_epoch, db_now
                    )
                    or not self._work_deadline_is_valid(row, db_now)
                ):
                    raise RunStateError("run_execution_lease_lost")
                call = self._single_tool_call(command.call_message)
                if (
                    call.tool_call_id != command.tool_call_id
                    or call.name != command.model_name
                    or command.call_message.session_id != row.session_id
                    or command.call_message.owner_id != row.owner_id
                    or command.call_message.run_id != row.id
                    or command.call_message.role is not ExecutionRole.ASSISTANT
                ):
                    raise RunStateError("run_tool_call_message_invalid")
                existing = await session.scalar(
                    select(RunToolCallModel)
                    .where(
                        RunToolCallModel.run_id == row.id,
                        RunToolCallModel.tool_call_id == command.tool_call_id,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    if (
                        existing.model_name != command.model_name
                        or existing.tool_id != command.tool_id
                        or existing.tool_kind
                        != (None if command.tool_kind is None else command.tool_kind.value)
                        or existing.call_message_id != command.call_message.id
                    ):
                        raise RunConflictError("run_tool_call_conflict")
                    return BeginToolCallResult(tool_call_to_domain(existing), False)
                ordinal = (
                    int(
                        (
                            await session.scalar(
                                select(func.max(RunToolCallModel.ordinal)).where(
                                    RunToolCallModel.run_id == row.id
                                )
                            )
                        )
                        or 0
                    )
                    + 1
                )
                await self._messages.append_execution_in_session(
                    session,
                    command.call_message,
                    locked_session=locked_session,
                    managed_run_id=row.id,
                )
                record = RunToolCallModel(
                    id=command.record_id,
                    run_id=row.id,
                    session_id=row.session_id,
                    tool_call_id=command.tool_call_id,
                    ordinal=ordinal,
                    tool_id=command.tool_id,
                    tool_kind=None if command.tool_kind is None else command.tool_kind.value,
                    model_name=command.model_name,
                    execution_epoch=command.execution_epoch,
                    status=ToolCallStatus.EXECUTING.value,
                    call_message_id=command.call_message.id,
                    result_message_id=None,
                    started_at=command.started_at,
                    finished_at=None,
                    safe_error_code=None,
                    outcome=None,
                    round=command.round,
                )
                session.add(record)
                await session.flush()
                await self._append_event(
                    session,
                    row,
                    RunToolCallStartedPayload(
                        call_id=record.id,
                        ordinal=ordinal,
                        round=command.round,
                    ),
                    command.started_at,
                )
                return BeginToolCallResult(tool_call_to_domain(record), True)

    async def begin_tool_call_batch(
        self, command: BeginToolCallBatchCommand
    ) -> BeginToolCallBatchResult:
        """Persist one assistant tool batch, approval barrier, and local failures atomically."""
        # 入库前先校验批次契约：数量上限、id 唯一性与协议 id 合法性。
        if not 1 <= len(command.calls) <= _MAX_TOOL_BATCH_SIZE:
            raise ValueError("run_tool_batch_size_invalid")
        record_ids = [item.record_id for item in command.calls]
        protocol_ids = [item.tool_call_id for item in command.calls]
        if len(set(record_ids)) != len(record_ids) or len(set(protocol_ids)) != len(protocol_ids):
            raise ValueError("run_tool_batch_duplicate")
        if any(not item.tool_call_id or len(item.tool_call_id) > 256 for item in command.calls):
            raise ValueError("run_tool_call_identity_invalid")
        # 助手消息必须只包含与 calls 顺序一致的 ToolCallPart，且不能混入工具结果。
        call_parts = [
            part
            for part in command.call_message.parts
            if isinstance(part, ToolCallPart)
        ]
        if (
            command.call_message.role is not ExecutionRole.ASSISTANT
            or any(isinstance(part, ToolResultPart) for part in command.call_message.parts)
            or [part.tool_call_id for part in call_parts] != protocol_ids
            or [part.name for part in call_parts] != [item.model_name for item in command.calls]
        ):
            raise RunStateError("run_tool_call_message_invalid")
        # 即时结果与结果消息必须成对出现：要么本地已终结，要么留给后续执行。
        for item in command.calls:
            if (item.immediate_outcome is None) != (item.result_message is None):
                raise RunStateError("run_tool_result_message_invalid")

        async with self._session_factory() as session:
            async with session.begin():
                # 锁定会话与 Run，并用 worker_id + execution_epoch 做 fencing，拒绝失租写入。
                row = await self._lock_worker_run(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                # 统一使用数据库时钟判断租约与执行截止时间，避免进程间时钟偏差。
                db_now = await session.scalar(select(func.clock_timestamp()))
                # 只有仍在执行、未被取消且租约/截止时间有效的 Run 才能写入工具批次。
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or not self._lease_is_valid(
                        row, command.worker_id, command.execution_epoch, db_now
                    )
                    or not self._work_deadline_is_valid(row, db_now)
                ):
                    raise RunStateError("run_execution_lease_lost")
                # 助手消息必须属于当前 Run，防止跨 Run 写入。
                if (
                    command.call_message.session_id != row.session_id
                    or command.call_message.owner_id != row.owner_id
                    or command.call_message.run_id != row.id
                ):
                    raise RunStateError("run_tool_call_message_invalid")

                # 幂等重放：同一批助手消息已落库时，校验内容一致后直接返回已有批次。
                existing_message = await session.get(
                    ExecutionMessageModel, command.call_message.id
                )
                if existing_message is not None:
                    existing_records = list(
                        (
                            await session.scalars(
                                select(RunToolCallModel)
                                .where(
                                    RunToolCallModel.run_id == row.id,
                                    RunToolCallModel.call_message_id == command.call_message.id,
                                )
                                .order_by(RunToolCallModel.ordinal)
                            )
                        ).all()
                    )
                    # 调用 id、模型名或参数列表不一致说明不是同一批次，按冲突处理。
                    if (
                        [record.tool_call_id for record in existing_records] != protocol_ids
                        or [
                            record.model_name for record in existing_records
                        ] != [item.model_name for item in command.calls]
                        or [
                            part
                            for part in execution_to_domain(existing_message).parts
                            if isinstance(part, ToolCallPart)
                        ]
                        != call_parts
                    ):
                        raise RunConflictError("run_tool_batch_conflict")
                    if [
                        None
                        if record.safe_skill_projection is None
                        else SafeSkillApprovalProjection.model_validate(
                            record.safe_skill_projection
                        )
                        for record in existing_records
                    ] != [
                        item.safe_skill_projection for item in command.calls
                    ]:
                        raise RunConflictError("run_tool_batch_conflict")
                    # replayed 标志为 True；是否仍在审批等待取决于 Run 当前状态。
                    return BeginToolCallBatchResult(
                        await self._batch_from_records(session, row.id, existing_records),
                        row.status == RunStatus.WAITING_FOR_APPROVAL.value,
                        True,
                    )

                # 续接 Run 内已有工具调用序号，保证批次内 ordinal 单调递增。
                ordinal = int(
                    (
                        await session.scalar(
                            select(func.max(RunToolCallModel.ordinal)).where(
                                RunToolCallModel.run_id == row.id
                            )
                        )
                    )
                    or 0
                )
                # 先写入助手工具调用消息，再逐条关联工具调用记录。
                await self._messages.append_execution_in_session(
                    session,
                    command.call_message,
                    locked_session=locked_session,
                    managed_run_id=row.id,
                )
                records: list[RunToolCallModel] = []
                pending_record_ids: list[str] = []
                # 逐条落库工具调用：有即时结果的直接收敛，需要审批的进入屏障。
                for item in command.calls:
                    ordinal += 1
                    outcome = (
                        None
                        if item.immediate_outcome is None
                        else bounded_outcome(item.immediate_outcome)
                    )
                    # 默认状态：需审批且无即时结果时等待审批，否则直接标记为可执行。
                    status = (
                        ToolCallStatus.PENDING_APPROVAL
                        if item.requires_approval and outcome is None
                        else ToolCallStatus.APPROVED
                    )
                    # 即时结果（如工具不可用、参数非法）按业务结果直接落为对应终态。
                    if outcome is not None:
                        status = persisted_status_for_outcome(outcome)
                    record = RunToolCallModel(
                        id=item.record_id,
                        run_id=row.id,
                        session_id=row.session_id,
                        tool_call_id=item.tool_call_id,
                        ordinal=ordinal,
                        tool_id=item.tool_id,
                        tool_kind=(
                            None if item.tool_kind is None else item.tool_kind.value
                        ),
                        model_name=item.model_name,
                        execution_epoch=command.execution_epoch,
                        status=status.value,
                        call_message_id=command.call_message.id,
                        result_message_id=None,
                        requires_approval=item.requires_approval,
                        requested_at=command.requested_at,
                        approval_decision=None,
                        approval_decided_at=None,
                        started_at=None,
                        finished_at=None if outcome is None else command.requested_at,
                        safe_error_code=(
                            None
                            if outcome is None or outcome.status is ToolOutcomeStatus.OK
                            else outcome.code
                        ),
                        outcome=None if outcome is None else outcome.model_dump(mode="json"),
                        safe_skill_projection=(
                            None
                            if item.safe_skill_projection is None
                            else item.safe_skill_projection.model_dump(mode="json")
                        ),
                        round=command.round,
                    )
                    # 先把新记录写入事务，供后续结果消息与事件关联。
                    session.add(record)
                    await session.flush()
                    # 有即时结果：校验结果消息形状并追加，同时记录工具调用完成事件。
                    if outcome is not None:
                        if item.result_message is None:
                            raise RunStateError("run_tool_result_message_invalid")
                        self._validate_tool_result_message(
                            record, item.result_message, row, outcome
                        )
                        await self._messages.append_execution_in_session(
                            session,
                            item.result_message,
                            locked_session=locked_session,
                            managed_run_id=row.id,
                        )
                        record.result_message_id = item.result_message.id
                        await self._append_event(
                            session,
                            row,
                            RunToolCallFinishedPayload(
                                call_id=record.id,
                                ordinal=record.ordinal,
                                status=status,
                                round=record.round,
                                safe_error_code=record.safe_error_code,
                            ),
                            command.requested_at,
                        )
                    # 无即时结果且需审批：收集起来用于后面的审批屏障事件。
                    elif status is ToolCallStatus.PENDING_APPROVAL:
                        pending_record_ids.append(record.id)
                    records.append(record)

                # 存在待审批调用时建立审批屏障：Run 暂停执行并释放租约。
                if pending_record_ids:
                    row.status = RunStatus.WAITING_FOR_APPROVAL.value
                    row.updated_at = command.requested_at
                    self._clear_lease(row)
                    # 事件携带本批全部待审批调用，供前端展示与后续决策校验。
                    await self._append_event(
                        session,
                        row,
                        RunWaitingForApprovalPayload(
                            batch_message_id=command.call_message.id,
                            pending_call_ids=tuple(pending_record_ids),
                        ),
                        command.requested_at,
                    )
                batch = await self._batch_from_records(session, row.id, records)
                # 在事务内构造返回结果，提交成功后由下方返回给调用方。
                result = BeginToolCallBatchResult(
                    batch,
                    bool(pending_record_ids),
                    False,
                )
        return result

    async def get_resumable_tool_batch(self, run_id: str) -> RunToolBatch | None:
        """Return the earliest approved batch before any new model request."""
        async with self._session_factory() as session:
            first = await session.scalar(
                select(RunToolCallModel)
                .where(
                    RunToolCallModel.run_id == run_id,
                    RunToolCallModel.status == ToolCallStatus.APPROVED.value,
                )
                .order_by(RunToolCallModel.ordinal)
                .limit(1)
            )
            if first is None:
                return None
            records = list(
                (
                    await session.scalars(
                        select(RunToolCallModel)
                        .where(
                            RunToolCallModel.run_id == run_id,
                            RunToolCallModel.call_message_id == first.call_message_id,
                        )
                        .order_by(RunToolCallModel.ordinal)
                    )
                ).all()
            )
            if any(
                record.status == ToolCallStatus.PENDING_APPROVAL.value
                for record in records
            ):
                return None
            return await self._batch_from_records(session, run_id, records)

    async def admit_tool_call(
        self, command: AdmitToolCallCommand
    ) -> RunToolCallRecord | None:
        """Atomically claim one approved call under the current execution lease."""
        async with self._session_factory() as session:
            async with session.begin():
                session_id = await session.scalar(
                    select(RunModel.session_id).where(RunModel.id == command.run_id)
                )
                if session_id is None:
                    raise RunNotFoundError("run_not_found")
                await self._lock_session_by_id(session, session_id)
                row = await self._lock_run(session, session_id, command.run_id)
                if (
                    row.worker_id != command.worker_id
                    or row.execution_epoch != command.execution_epoch
                ):
                    return None
                record = await session.scalar(
                    select(RunToolCallModel)
                    .where(
                        RunToolCallModel.id == command.record_id,
                        RunToolCallModel.run_id == row.id,
                        RunToolCallModel.session_id == row.session_id,
                    )
                    .with_for_update()
                )
                if record is None:
                    raise RunNotFoundError("run_tool_call_not_found")
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or not self._lease_is_valid(
                        row, command.worker_id, command.execution_epoch, db_now
                    )
                    or not self._work_deadline_is_valid(row, db_now)
                    or record.status != ToolCallStatus.APPROVED.value
                ):
                    return None
                record.status = ToolCallStatus.EXECUTING.value
                record.execution_epoch = command.execution_epoch
                record.started_at = command.admitted_at
                row.updated_at = command.admitted_at
                await self._append_event(
                    session,
                    row,
                    RunToolCallStartedPayload(
                        call_id=record.id,
                        ordinal=record.ordinal,
                        round=record.round,
                    ),
                    command.admitted_at,
                )
                result = tool_call_to_domain(record)
        return result

    async def converge_tool_batch(self, command: ConvergeToolBatchCommand) -> Run | None:
        """Cancel undispatched calls and fail the Run after started calls settle."""
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
                ):
                    return None
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                unsettled = list(
                    (
                        await session.scalars(
                            select(RunToolCallModel)
                            .where(
                                RunToolCallModel.run_id == row.id,
                                RunToolCallModel.call_message_id == command.call_message_id,
                                RunToolCallModel.status.in_(
                                    (
                                        ToolCallStatus.PENDING_APPROVAL.value,
                                        ToolCallStatus.APPROVED.value,
                                        ToolCallStatus.EXECUTING.value,
                                    )
                                ),
                            )
                            .order_by(RunToolCallModel.ordinal)
                            .with_for_update()
                        )
                    ).all()
                )
                await self._settle_unfinished_tool_calls(
                    session,
                    locked_session,
                    row,
                    command.failed_at,
                    records=unsettled,
                )
                await self._fail_run_in_transaction(
                    session,
                    locked_session,
                    row,
                    command.safe_error_code,
                    command.failed_at,
                )
                result = run_to_domain(row)
        await self._wake_memory(result.owner_id)
        return result

    async def decide_tool_approval(
        self, command: DecideToolApprovalCommand
    ) -> Run | None:
        """Apply one idempotent approval decision and queue only after the last pending."""
        async with self._session_factory() as session:
            # 决策写入、工具结果消息、事件与幂等台账都在同一事务内提交。
            async with session.begin():
                # 先锁会话再锁 Run，与其它 Run 操作保持一致的加锁顺序，避免死锁。
                locked_session = await self._lock_session(
                    session, command.session_id, command.owner_id
                )
                row = await self._lock_run(session, command.session_id, command.run_id)
                # 请求级幂等：同一 request_id 已处理过则直接返回上次结果。
                replay = await self._find_request(
                    session,
                    command.owner_id,
                    command.session_id,
                    RunRequestOperation.DECIDE_TOOL_APPROVAL,
                    command.request_id,
                )
                if replay is not None:
                    # request_id 被复用于不同参数时视为冲突，防止误认领他人结果。
                    self._check_fingerprint(replay, command.fingerprint)
                    replay_run_id = RunRequestResult.model_validate(replay.result).run_id
                    replay_row = await session.scalar(
                        select(RunModel).where(
                            RunModel.id == replay_run_id,
                            RunModel.session_id == command.session_id,
                        )
                    )
                    if replay_row is None:
                        raise RunNotFoundError("run_not_found")
                    return run_to_domain(replay_row)

                # 行锁工具调用记录，串行化同一调用上的并发决策。
                record = await session.scalar(
                    select(RunToolCallModel)
                    .where(
                        RunToolCallModel.id == command.record_id,
                        RunToolCallModel.run_id == row.id,
                        RunToolCallModel.session_id == row.session_id,
                    )
                    .with_for_update()
                )
                if record is None:
                    raise RunNotFoundError("run_tool_call_not_found")
                # 记录级幂等：换一个 request_id 重复提交同一决策也视为成功。
                if record.approval_decision is not None:
                    if record.approval_decision == command.decision.value:
                        await self._store_request(
                            session,
                            command.owner_id,
                            command.session_id,
                            RunRequestOperation.DECIDE_TOOL_APPROVAL,
                            command.request_id,
                            command.fingerprint,
                            RunRequestResult(run_id=row.id),
                            command.decided_at,
                        )
                        return run_to_domain(row)
                    # 已做出相反决策时不允许覆盖。
                    raise RunConflictError("run_tool_approval_already_decided")
                if (
                    command.decision is ToolApprovalDecision.APPROVE
                    and record.tool_id == SKILL_EXECUTE_TOOL_ID
                    and record.safe_skill_projection is None
                ):
                    raise RunStateError("skill_approval_projection_unavailable")
                if (
                    command.decision is ToolApprovalDecision.APPROVE
                    and record.tool_id == SKILL_EXECUTE_TOOL_ID
                ):
                    projection = await self._refresh_safe_skill_projection(
                        session,
                        record,
                    )
                    if projection is None or not projection.can_approve:
                        raise RunStateError("skill_approval_metadata_unavailable")
                # 只有 Run 处于等待审批且该调用仍待审批时才接受决策。
                if (
                    row.status != RunStatus.WAITING_FOR_APPROVAL.value
                    or record.status != ToolCallStatus.PENDING_APPROVAL.value
                ):
                    raise RunStateError("run_tool_approval_not_pending")

                # 落库决策本身；批准后调用保持 APPROVED，等待 Run 重新入队后执行。
                record.approval_decision = command.decision.value
                record.approval_decided_at = command.decided_at
                if command.decision is ToolApprovalDecision.APPROVE:
                    record.status = ToolCallStatus.APPROVED.value
                else:
                    # 拒绝：写入一条失败的 tool 结果消息，让模型下一轮看到未执行及原因。
                    call_message = await session.get(
                        ExecutionMessageModel, record.call_message_id
                    )
                    if call_message is None:
                        raise RuntimeError("run_tool_call_message_missing")
                    # 用结构化 error 结果表达拒绝，模型可据此调整后续计划。
                    denied = ToolOutcome(
                        status=ToolOutcomeStatus.ERROR,
                        code="tool_approval_denied",
                        message="用户拒绝了该工具调用，未执行外部操作。",
                    )
                    result_message_id = new_id("exec")
                    result_message = ExecutionMessageDraft(
                        id=result_message_id,
                        session_id=row.session_id,
                        owner_id=row.owner_id,
                        run_id=row.id,
                        thread_id=call_message.thread_id,
                        role=ExecutionRole.TOOL,
                        parts=(
                            ToolResultPart(
                                tool_call_id=record.tool_call_id,
                                status=ToolResultStatus.ERROR,
                                output=denied.canonical_output(),
                            ),
                        ),
                        tool_call_id=record.tool_call_id,
                        source_message_id=None,
                        created_at=command.decided_at,
                        metadata=MessageMetadata(),
                    )
                    await self._messages.append_execution_in_session(
                        session,
                        result_message,
                        locked_session=locked_session,
                        managed_run_id=row.id,
                    )
                    # 记录终态与可审计的 outcome，并关联刚追加的结果消息。
                    record.status = ToolCallStatus.FAILED.value
                    record.result_message_id = result_message_id
                    record.finished_at = command.decided_at
                    record.safe_error_code = denied.code
                    record.outcome = denied.model_dump(mode="json")

                # 追加审批结果事件到 Run 事件流，供前端推送与断线对账。
                await self._append_event(
                    session,
                    row,
                    RunToolApprovalDecidedPayload(
                        call_id=record.id,
                        ordinal=record.ordinal,
                        decision=command.decision,
                        status=ToolCallStatus(record.status),
                    ),
                    command.decided_at,
                )
                row.updated_at = command.decided_at
                # 记录决策所依据的登录会话，供审计追踪。
                if command.auth_session_id is not None:
                    row.auth_session_id = command.auth_session_id
                # 先 flush，确保下面的待审批计数能看到本次状态变更。
                await session.flush()
                # 统计本 Run 是否还有未决审批；最后一个决策完成后才恢复执行。
                remaining = await session.scalar(
                    select(func.count(RunToolCallModel.id)).where(
                        RunToolCallModel.run_id == row.id,
                        RunToolCallModel.status == ToolCallStatus.PENDING_APPROVAL.value,
                    )
                )
                if not remaining:
                    # 全部审批完成：Run 重新入队并清空租约，等待 Worker 领取继续执行。
                    row.status = RunStatus.QUEUED.value
                    row.queued_at = command.decided_at
                    row.updated_at = command.decided_at
                    self._clear_lease(row)
                    await self._append_event(
                        session,
                        row,
                        RunApprovalResumedPayload(
                            batch_message_id=record.call_message_id
                        ),
                        command.decided_at,
                    )
                # 写入幂等台账，供相同 request_id 的重试直接返回。
                await self._store_request(
                    session,
                    command.owner_id,
                    command.session_id,
                    RunRequestOperation.DECIDE_TOOL_APPROVAL,
                    command.request_id,
                    command.fingerprint,
                    RunRequestResult(run_id=row.id),
                    command.decided_at,
                )
                # 在事务内构造返回快照，提交成功后才把结果交给调用方。
                result = run_to_domain(row)
        return result

    async def list_pending_approvals(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[PendingToolApproval, ...] | None:
        """Read the safe pending-call projection for an owner-visible Run."""
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
                return None
            rows = (
                await session.scalars(
                    select(RunToolCallModel)
                    .where(
                        RunToolCallModel.run_id == run_id,
                        RunToolCallModel.session_id == session_id,
                        RunToolCallModel.status == ToolCallStatus.PENDING_APPROVAL.value,
                    )
                    .order_by(RunToolCallModel.ordinal)
                )
            ).all()
            pending: list[PendingToolApproval] = []
            for row in rows:
                if row.requested_at is None:
                    raise RuntimeError("run_tool_call_requested_at_missing")
                pending.append(
                    PendingToolApproval(
                        call_id=row.id,
                        batch_message_id=row.call_message_id,
                        ordinal=row.ordinal,
                        model_name=row.model_name,
                        tool_kind=(
                            None if row.tool_kind is None else ToolKind(row.tool_kind)
                        ),
                        requested_at=row.requested_at,
                        safe_skill_projection=await self._refresh_safe_skill_projection(
                            session, row
                        ),
                    )
                )
        return tuple(pending)

    async def finish_tool_call(self, command: FinishToolCallCommand) -> Run | None:
        """Persist one result and atomically converge/fail when required."""
        return await self._finish_tool_call_transaction(command)

    async def _finish_tool_call_transaction(
        self,
        command: FinishToolCallCommand,
    ) -> Run | None:
        """在工具失败事务提交后补做记忆唤醒，提交失败不唤醒。"""
        result = await self._finish_tool_call_transaction_inner(command)
        if result is not None and result.status is RunStatus.FAILED:
            await self._wake_memory(result.owner_id)
        return result

    async def _finish_tool_call_transaction_inner(
        self,
        command: FinishToolCallCommand,
    ) -> Run | None:
        """在 Session/Run 既有序内完成结果、对象引用与配额提交。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._lock_worker_run(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                record = await session.scalar(
                    select(RunToolCallModel)
                    .where(
                        RunToolCallModel.id == command.record_id,
                        RunToolCallModel.run_id == row.id,
                        RunToolCallModel.session_id == row.session_id,
                    )
                    .with_for_update()
                )
                if record is None:
                    raise RunStateError("run_tool_call_not_found")
                outcome = bounded_outcome(command.outcome)
                self._validate_tool_result_message(record, command.result_message, row, outcome)
                pending_artifacts = self._validated_pending_artifacts(
                    record,
                    command.result_message,
                    command.artifacts,
                )
                self._validate_delivery_message(
                    record,
                    command.delivery_message,
                    pending_artifacts,
                    row,
                )
                if record.status != ToolCallStatus.EXECUTING.value:
                    requested_outcome = outcome.model_dump(mode="json")
                    if (
                        record.outcome == requested_outcome
                        and record.result_message_id == command.result_message.id
                        and await self._artifact_rows_match(
                            session,
                            record,
                            pending_artifacts,
                        )
                        and await self._skill_completion_matches(
                            session,
                            record,
                            command.skill_execution_completion,
                            pending_artifacts,
                            outcome=outcome,
                        )
                        and await self._delivery_message_matches(
                            session,
                            command.delivery_message,
                        )
                    ):
                        return run_to_domain(row)
                    raise RunConflictError("run_tool_call_already_finished")
                db_now = await session.scalar(select(func.clock_timestamp()))
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                    or record.execution_epoch != command.execution_epoch
                    or not self._lease_is_valid(
                        row, command.worker_id, command.execution_epoch, db_now
                    )
                    or not self._work_deadline_is_valid(row, db_now)
                ):
                    return None
                if pending_artifacts:
                    await lock_session_file_quota(session, row.session_id)
                    count = await count_published_session_files(session, row.session_id)
                    if count + len(pending_artifacts) > 100:
                        raise ValueError("session_file_quota_exceeded")
                    for artifact in pending_artifacts:
                        session.add(
                            pending_file_to_model(
                                artifact,
                                session_id=row.session_id,
                                owner_id=row.owner_id,
                                source="artifact",
                                source_call_id=record.id,
                            )
                        )
                if command.skill_execution_completion is not None:
                    await self._finish_skill_execution_in_transaction(
                        session,
                        record,
                        row,
                        command.skill_execution_completion,
                        pending_artifacts,
                    )
                elif outcome.status is ToolOutcomeStatus.UNKNOWN:
                    # 启动回查失败时没有可信 execution_id，不能猜测新 ID；
                    # 工具结果已确定 UNKNOWN，按 tool_call_record_id 收敛 running 审计。
                    await self._settle_running_skill_execution(
                        session,
                        record.id,
                        command.finished_at,
                    )
                await self._messages.append_execution_in_session(
                    session,
                    command.result_message,
                    locked_session=locked_session,
                    managed_run_id=row.id,
                )
                if command.delivery_message is not None:
                    # 产物交付同时作为普通助手消息发布，使分页刷新后的公开消息流
                    # 可恢复同一组 FileRefPart；内部 TOOL 消息仍保持唯一。
                    await self._messages.append_conversation_in_session(
                        session,
                        command.delivery_message,
                        locked_session=locked_session,
                        managed_run_id=row.id,
                    )
                record.status = persisted_status_for_outcome(outcome).value
                record.result_message_id = command.result_message.id
                record.finished_at = command.finished_at
                record.safe_error_code = (
                    None if outcome.status is ToolOutcomeStatus.OK else outcome.code
                )
                record.outcome = outcome.model_dump(mode="json")
                await self._append_event(
                    session,
                    row,
                    RunToolCallFinishedPayload(
                        call_id=record.id,
                        ordinal=record.ordinal,
                        status=ToolCallStatus(record.status),
                        round=record.round,
                        safe_error_code=record.safe_error_code,
                    ),
                    command.finished_at,
                )
                fail_code = (
                    "tool_outcome_unknown"
                    if outcome.status is ToolOutcomeStatus.UNKNOWN
                    else command.terminate_error_code
                )
                if fail_code is not None and not command.defer_terminal_convergence:
                    await self._fail_run_in_transaction(
                        session,
                        locked_session,
                        row,
                        fail_code,
                        command.finished_at,
                    )
                return run_to_domain(row)

    async def list_tool_calls(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_ordinal: int,
        limit: int,
    ) -> RunToolCallPage | None:
        """Read one owner-visible ordinal page."""
        if after_ordinal < 0 or not 1 <= limit <= 100:
            raise ValueError("run_tool_call_page_invalid")
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
                return None
            rows = (
                await session.scalars(
                    select(RunToolCallModel)
                    .where(
                        RunToolCallModel.run_id == run_id,
                        RunToolCallModel.session_id == session_id,
                        RunToolCallModel.ordinal > after_ordinal,
                    )
                    .order_by(RunToolCallModel.ordinal)
                    .limit(limit + 1)
                )
            ).all()
        has_more = len(rows) > limit
        items = tuple(tool_call_to_domain(row) for row in rows[:limit])
        return RunToolCallPage(
            items,
            items[-1].ordinal if has_more and items else None,
        )

    async def get_tool_call(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        call_id: str,
    ) -> RunToolCallDetails | None:
        """Read arguments and output from their unique execution messages."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(RunToolCallModel)
                .join(RunModel, RunModel.id == RunToolCallModel.run_id)
                .join(SessionModel, SessionModel.id == RunModel.session_id)
                .where(
                    RunToolCallModel.id == call_id,
                    RunToolCallModel.run_id == run_id,
                    RunToolCallModel.session_id == session_id,
                    RunModel.owner_id == owner_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if row is None:
                return None
            call_message = await session.get(ExecutionMessageModel, row.call_message_id)
            result_message = (
                None
                if row.result_message_id is None
                else await session.get(ExecutionMessageModel, row.result_message_id)
            )
        if call_message is None:
            raise ValueError("run_tool_call_message_missing")
        call_domain = execution_to_domain(call_message)
        call_part = next(
            (
                part
                for part in call_domain.parts
                if isinstance(part, ToolCallPart) and part.tool_call_id == row.tool_call_id
            ),
            None,
        )
        output: str | None = None
        artifacts: tuple[ArtifactFileRef, ...] = ()
        if result_message is not None:
            result_domain = execution_to_domain(result_message)
            result_part = next(
                (
                    part
                    for part in result_domain.parts
                    if isinstance(part, ToolResultPart) and part.tool_call_id == row.tool_call_id
                ),
                None,
            )
            output = None if result_part is None else result_part.output
            artifacts = () if result_part is None else result_part.artifacts
        async with self._session_factory() as session:
            safe_skill_projection = await self._refresh_safe_skill_projection(session, row)
        return RunToolCallDetails(
            tool_call_to_domain(row),
            {} if call_part is None else dict(call_part.arguments),
            output,
            artifacts,
            safe_skill_projection,
        )

    async def list_retry_context(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
    ) -> tuple[ToolRetryContext, ...] | None:
        """Read only safe unknown summaries from the direct source Run."""
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
            if run.retry_of_run_id is None:
                return ()
            rows = (
                await session.scalars(
                    select(RunToolCallModel)
                    .where(
                        RunToolCallModel.run_id == run.retry_of_run_id,
                        RunToolCallModel.session_id == session_id,
                        RunToolCallModel.status == ToolCallStatus.UNKNOWN.value,
                    )
                    .order_by(RunToolCallModel.ordinal)
                )
            ).all()
        return tuple(
            ToolRetryContext(
                run_id=row.run_id,
                tool_name=row.model_name,
                safe_error_code=row.safe_error_code or "tool_outcome_unknown",
            )
            for row in rows
        )

    @staticmethod
    def _aggregate_usage(attempts: tuple[RunAttemptUsage, ...]) -> RunUsage:
        def total(field: str) -> int | None:
            values = [getattr(item, field) for item in attempts]
            return (
                None
                if not any(value is not None for value in values)
                else sum(value for value in values if value is not None)
            )

        return RunUsage(
            input_tokens=total("input_tokens"),
            output_tokens=total("output_tokens"),
            total_tokens=total("total_tokens"),
            complete=bool(attempts) and all(item.complete for item in attempts),
            attempts=attempts,
            answer_tokens=_purpose_total(attempts, "answer"),
            compression_tokens=_purpose_total(attempts, "compression"),
        )

    @classmethod
    def _merge_usage(cls, current_json: dict[str, object], incoming: RunUsage) -> RunUsage:
        current = RunUsage.model_validate(current_json)
        by_key = {
            (
                item.execution_epoch,
                item.purpose,
                item.operation_id,
                item.batch_index,
                item.attempt,
            ): item
            for item in current.attempts
        }
        for item in incoming.attempts:
            key = (
                item.execution_epoch,
                item.purpose,
                item.operation_id,
                item.batch_index,
                item.attempt,
            )
            existing = by_key.get(key)
            if existing is not None and existing != item:
                raise RunConflictError("run_usage_attempt_conflict")
            by_key[key] = item
        if not incoming.attempts:
            if any(
                value is not None
                for value in (incoming.input_tokens, incoming.output_tokens, incoming.total_tokens)
            ):
                raise RunConflictError("run_usage_attempts_required")
            return current
        if not current.attempts:
            legacy_input = current.input_tokens
            legacy_output = current.output_tokens
            legacy_total = current.total_tokens
            legacy_answer = current.total_tokens
            legacy_complete = current.complete
        else:
            legacy_input = current.legacy_input_tokens
            legacy_output = current.legacy_output_tokens
            legacy_total = current.legacy_total_tokens
            legacy_answer = current.legacy_answer_tokens
            legacy_complete = current.legacy_complete
        keys = sorted(
            by_key,
            key=lambda item: (item[0], item[1], item[2] or "", item[3], item[4]),
        )
        aggregate = cls._aggregate_usage(tuple(by_key[key] for key in keys))
        if any(value is not None for value in (legacy_input, legacy_output, legacy_total)):
            aggregate = aggregate.model_copy(
                update={
                    "input_tokens": None
                    if aggregate.input_tokens is None and legacy_input is None
                    else (aggregate.input_tokens or 0) + (legacy_input or 0),
                    "output_tokens": None
                    if aggregate.output_tokens is None and legacy_output is None
                    else (aggregate.output_tokens or 0) + (legacy_output or 0),
                    "total_tokens": None
                    if aggregate.total_tokens is None and legacy_total is None
                    else (aggregate.total_tokens or 0) + (legacy_total or 0),
                    "legacy_input_tokens": legacy_input,
                    "legacy_output_tokens": legacy_output,
                    "legacy_total_tokens": legacy_total,
                    "legacy_answer_tokens": legacy_answer,
                    "legacy_complete": legacy_complete,
                    "answer_tokens": None
                    if aggregate.answer_tokens is None and legacy_answer is None
                    else (aggregate.answer_tokens or 0) + (legacy_answer or 0),
                    "complete": aggregate.complete and legacy_complete,
                }
            )
        return aggregate

    async def request_input(self, command: RequestRunInputCommand) -> RunInputRequest:
        """原子保存澄清消息、执行交互、输入请求和 waiting event。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._lock_worker_run(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                db_now = await session.scalar(select(func.clock_timestamp()))
                if not self._lease_is_valid(
                    row, command.worker_id, command.execution_epoch, db_now
                ) or not self._work_deadline_is_valid(row, db_now):
                    raise RunStateError("run_execution_lease_lost")
                if row.status != RunStatus.RUNNING.value:
                    raise RunStateError("run_execution_not_active")
                if await self._has_unsettled_tool_calls(session, row.id):
                    raise RunStateError("run_tool_call_unsettled")
                if (
                    command.question_message.session_id != row.session_id
                    or command.question_message.owner_id != row.owner_id
                    or command.question_message.run_id != row.id
                    or command.question_message.role is not ConversationRole.ASSISTANT
                    or any(
                        item.session_id != row.session_id
                        or item.owner_id != row.owner_id
                        or item.run_id != row.id
                        for item in command.execution_messages
                    )
                ):
                    raise RunStateError("run_input_messages_invalid")
                await self._validate_input_interaction(
                    session,
                    row.id,
                    row.session_id,
                    command.question_message,
                    command.execution_messages,
                )
                question = await self._messages.append_conversation_in_session(
                    session,
                    command.question_message,
                    locked_session=locked_session,
                    managed_run_id=row.id,
                )
                for execution in command.execution_messages:
                    await self._messages.append_execution_in_session(
                        session,
                        execution,
                        locked_session=locked_session,
                        managed_run_id=row.id,
                    )
                request = RunInputRequestModel(
                    id=command.input_request_id,
                    run_id=row.id,
                    session_id=row.session_id,
                    question_message_id=question.id,
                    status=InputRequestStatus.PENDING.value,
                    answer_message_id=None,
                    created_at=command.created_at,
                )
                session.add(request)
                row.status = RunStatus.WAITING_FOR_INPUT.value
                row.usage = self._merge_usage(row.usage, command.usage).model_dump(mode="json")
                row.worker_id = None
                row.lease_expires_at = None
                row.execution_started_at = None
                row.execution_deadline_at = None
                row.updated_at = command.created_at
                await self._append_event(
                    session,
                    row,
                    RunWaitingPayload(
                        input_request_id=request.id,
                        question_message_id=question.id,
                    ),
                    command.created_at,
                )
                await session.flush()
                result = input_request_to_domain(request)
        return result

    @staticmethod
    async def _batch_from_records(
        session: AsyncSession,
        run_id: str,
        records: list[RunToolCallModel],
    ) -> RunToolBatch:
        """Build a typed batch from one persisted assistant message."""
        if not records:
            raise ValueError("run_tool_batch_empty")
        call_message_id = records[0].call_message_id
        if any(record.call_message_id != call_message_id for record in records):
            raise ValueError("run_tool_batch_message_invalid")
        message = await session.get(ExecutionMessageModel, call_message_id)
        if message is None:
            raise RuntimeError("run_tool_call_message_missing")
        call_message = execution_to_domain(message)
        arguments = {
            part.tool_call_id: dict(part.arguments)
            for part in call_message.parts
            if isinstance(part, ToolCallPart)
        }
        ordered = sorted(records, key=lambda item: item.ordinal)
        if any(record.run_id != run_id for record in ordered) or any(
            record.tool_call_id not in arguments for record in ordered
        ):
            raise ValueError("run_tool_batch_call_invalid")
        return RunToolBatch(
            call_message_id=call_message_id,
            thread_id=call_message.thread_id,
            round=ordered[0].round,
            calls=tuple(
                RunToolBatchCall(
                    record=tool_call_to_domain(record),
                    arguments=arguments[record.tool_call_id],
                )
                for record in ordered
            ),
        )

    @staticmethod
    async def _validate_input_interaction(
        session: AsyncSession,
        run_id: str,
        session_id: str,
        question: ConversationMessageDraft,
        execution_messages: tuple[ExecutionMessageDraft, ...],
    ) -> None:
        """写入前确认单一澄清调用与 question、线程来源一致。"""
        if len(question.parts) != 1 or not isinstance(question.parts[0], TextPart):
            raise RunStateError("run_input_question_invalid")
        question_text = question.parts[0].text
        matches = [
            (message, part)
            for message in execution_messages
            if message.role is ExecutionRole.ASSISTANT
            for part in message.parts
            if isinstance(part, ToolCallPart) and part.name == "request_user_input"
        ]
        if len(matches) != 1:
            raise RunStateError("run_input_call_invalid")
        call_message, call = matches[0]
        if (
            call_message.source_message_id != question.id
            or not call_message.thread_id
            or any(message.thread_id != call_message.thread_id for message in execution_messages)
            or not isinstance(call.arguments.get("question"), str)
            or call.arguments["question"] != question_text
        ):
            raise RunStateError("run_input_call_reference_invalid")
        existing_thread_ids = set(
            (
                await session.scalars(
                    select(ExecutionMessageModel.thread_id).where(
                        ExecutionMessageModel.run_id == run_id,
                        ExecutionMessageModel.session_id == session_id,
                    )
                )
            ).all()
        )
        if len(existing_thread_ids) > 1 or (
            existing_thread_ids and call_message.thread_id not in existing_thread_ids
        ):
            raise RunStateError("run_input_thread_reference_invalid")

    async def finish(self, command: FinishRunCommand) -> Run | None:
        """在租约有效时原子保存最终消息、usage 和 completed event。"""
        result: Run | None = None
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._lock_worker_run(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                db_now = await session.scalar(select(func.clock_timestamp()))
                if not self._lease_is_valid(
                    row, command.worker_id, command.execution_epoch, db_now
                ) or not self._work_deadline_is_valid(row, db_now):
                    return None
                if row.status != RunStatus.RUNNING.value:
                    return None
                if await self._has_unsettled_tool_calls(session, row.id):
                    await self._settle_unfinished_tool_calls(
                        session,
                        locked_session,
                        row,
                        command.finished_at,
                    )
                    row.status = RunStatus.FAILED.value
                    row.safe_error_code = "tool_outcome_unknown"
                    row.finished_at = command.finished_at
                    row.updated_at = command.finished_at
                    self._mark_memory_pending(row, command.finished_at)
                    self._clear_lease(row)
                    await self._close_pending_input(session, row, command.finished_at)
                    await self._append_event(
                        session,
                        row,
                        RunFailedPayload(safe_error_code="tool_outcome_unknown"),
                        command.finished_at,
                    )
                    result = run_to_domain(row)
                elif (
                    command.reply_message.session_id != row.session_id
                    or command.reply_message.owner_id != row.owner_id
                    or command.reply_message.run_id != row.id
                    or command.reply_message.role is not ConversationRole.ASSISTANT
                ):
                    raise RunStateError("run_reply_message_invalid")
                else:
                    reply = await self._messages.append_conversation_in_session(
                        session,
                        command.reply_message,
                        locked_session=locked_session,
                        managed_run_id=row.id,
                    )
                    row.status = RunStatus.COMPLETED.value
                    row.finished_at = command.finished_at
                    row.updated_at = command.finished_at
                    row.safe_error_code = None
                    self._mark_memory_pending(row, command.finished_at)
                    row.usage = self._merge_usage(row.usage, command.usage).model_dump(mode="json")
                    self._clear_lease(row)
                    await self._append_event(
                        session,
                        row,
                        RunCompletedPayload(assistant_message_id=reply.id),
                        command.finished_at,
                    )
                    result = run_to_domain(row)
        if result is not None:
            await self._wake_memory(result.owner_id)
        return result

    async def fail(self, command: FailRunCommand) -> Run | None:
        """在租约有效时原子保存安全失败状态和事件。"""
        async with self._session_factory() as session:
            async with session.begin():
                session_id = await session.scalar(
                    select(RunModel.session_id).where(RunModel.id == command.run_id)
                )
                if session_id is None:
                    raise RunNotFoundError("run_not_found")
                await self._lock_session_by_id(session, session_id)
                row = await self._lock_run(session, session_id, command.run_id)
                # 同一稳定报告已提交后，并发重放应返回既有终态而不是因租约已清理误报失租。
                if (
                    row.status == RunStatus.FAILED.value
                    and row.cancel_requested_at is None
                    and command.report_message is not None
                    and row.report_message_id == command.report_message.id
                    and row.safe_error_code == command.safe_error_code
                ):
                    return run_to_domain(row)
                if (
                    row.worker_id != command.worker_id
                    or row.execution_epoch != command.execution_epoch
                ):
                    raise RunStateError("run_execution_lease_lost")
                db_now = await session.scalar(select(func.clock_timestamp()))
                if not self._lease_is_valid(
                    row, command.worker_id, command.execution_epoch, db_now
                ):
                    return None
                # 安全报告不得在 deadline 已失效后补写；普通失败仍允许记录 execution_timeout。
                if (
                    command.context_stop_reason is not None
                    and not self._work_deadline_is_valid(row, db_now)
                ):
                    return None
                if (
                    row.status != RunStatus.RUNNING.value
                    or row.cancel_requested_at is not None
                ):
                    return None
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                await self._settle_unfinished_tool_calls(
                    session,
                    locked_session,
                    row,
                    command.failed_at,
                )
                report_message_id: str | None = None
                if command.report_message is not None:
                    report = command.report_message
                    if (
                        not report.id
                        or len(report.id) > 64
                        or report.session_id != row.session_id
                        or report.owner_id != row.owner_id
                        or report.run_id != row.id
                        or report.role is not ConversationRole.ASSISTANT
                        or report.status is not MessageStatus.COMPLETED
                        or report.reply_to_message_id != row.trigger_message_id
                        or report.context_turn_id != row.trigger_message_id
                        or len(report.parts) != 1
                        or not isinstance(report.parts[0], TextPart)
                        or not report.parts[0].text.strip()
                    ):
                        raise RunStateError("run_context_report_invalid")
                    trigger_sequence = await session.scalar(
                        select(ConversationMessageModel.sequence).where(
                            ConversationMessageModel.id == row.trigger_message_id,
                            ConversationMessageModel.session_id == row.session_id,
                        )
                    )
                    if trigger_sequence is None:
                        raise RunStateError("run_context_report_trigger_invalid")
                    persisted_report = await self._messages.append_conversation_in_session(
                        session,
                        report,
                        locked_session=locked_session,
                        managed_run_id=row.id,
                    )
                    if (
                        persisted_report.run_id != row.id
                        or persisted_report.reply_to_message_id != row.trigger_message_id
                        or persisted_report.sequence <= trigger_sequence
                    ):
                        raise RunStateError("run_context_report_reference_invalid")
                    report_message_id = persisted_report.id
                row.status = RunStatus.FAILED.value
                row.safe_error_code = command.safe_error_code
                row.finished_at = command.failed_at
                row.updated_at = command.failed_at
                self._mark_memory_pending(row, command.failed_at)
                row.report_message_id = report_message_id
                row.usage = self._merge_usage(row.usage, command.usage).model_dump(mode="json")
                self._clear_lease(row)
                await self._close_pending_input(session, row, command.failed_at)
                await self._append_event(
                    session,
                    row,
                    RunFailedPayload(
                        safe_error_code=command.safe_error_code,
                        report_message_id=report_message_id,
                        context_stop_reason=command.context_stop_reason,
                    ),
                    command.failed_at,
                )
                result = run_to_domain(row)
        if result is not None:
            await self._wake_memory(result.owner_id)
        return result

    async def settle_cancelled(
        self, run_id: str, worker_id: str, execution_epoch: int, cancelled_at: datetime
    ) -> Run | None:
        """由持有有效租约的 Worker 确认 cancelling Run 已清理结束。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await self._lock_worker_run(session, run_id, worker_id, execution_epoch)
                db_now = await session.scalar(select(func.clock_timestamp()))
                if not self._lease_is_valid(row, worker_id, execution_epoch, db_now):
                    return None
                if row.status != RunStatus.CANCELLING.value:
                    return None
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                await self._settle_unfinished_tool_calls(
                    session,
                    locked_session,
                    row,
                    cancelled_at,
                )
                row.status = RunStatus.CANCELLED.value
                row.finished_at = cancelled_at
                row.updated_at = cancelled_at
                self._mark_memory_pending(row, cancelled_at)
                self._clear_lease(row)
                await self._close_pending_input(session, row, cancelled_at)
                await self._append_event(session, row, RunCancelledPayload(), cancelled_at)
                result = run_to_domain(row)
        if result is not None:
            await self._wake_memory(result.owner_id)
        return result

    async def reclaim_expired(self, *, limit: int) -> tuple[Run, ...]:
        """按 Session → Run 顺序撤销过期执行租约并写终态事件。"""
        if not 1 <= limit <= 128:
            raise ValueError("run_reclaim_limit_invalid")
        async with self._session_factory() as session:
            candidate_ids = (
                await session.scalars(
                    select(RunModel.id)
                    .where(
                        RunModel.status.in_([RunStatus.RUNNING.value, RunStatus.CANCELLING.value]),
                        RunModel.lease_expires_at <= func.clock_timestamp(),
                    )
                    .order_by(RunModel.lease_expires_at, RunModel.id)
                    .limit(limit * 8)
                )
            ).all()
        reclaimed: list[Run] = []
        for run_id in candidate_ids:
            if len(reclaimed) >= limit:
                break
            async with self._session_factory() as session:
                async with session.begin():
                    session_id = await session.scalar(
                        select(RunModel.session_id).where(RunModel.id == run_id)
                    )
                    if session_id is None:
                        continue
                    locked_session = await session.scalar(
                        select(SessionModel)
                        .where(SessionModel.id == session_id)
                        .with_for_update(skip_locked=True)
                    )
                    if locked_session is None:
                        continue
                    row = await session.scalar(
                        select(RunModel)
                        .where(
                            RunModel.id == run_id,
                            RunModel.session_id == session_id,
                            RunModel.status.in_(
                                [RunStatus.RUNNING.value, RunStatus.CANCELLING.value]
                            ),
                        )
                        .with_for_update()
                    )
                    if row is None:
                        continue
                    db_now = await session.scalar(select(func.clock_timestamp()))
                    if (
                        db_now is None
                        or row.lease_expires_at is None
                        or row.lease_expires_at > db_now
                    ):
                        continue
                    was_cancelling = row.status == RunStatus.CANCELLING.value
                    await self._settle_unfinished_tool_calls(
                        session,
                        locked_session,
                        row,
                        db_now,
                    )
                    row.status = (
                        RunStatus.CANCELLED.value if was_cancelling else RunStatus.FAILED.value
                    )
                    row.safe_error_code = None if was_cancelling else "execution_interrupted"
                    row.finished_at = db_now
                    row.updated_at = db_now
                    self._mark_memory_pending(row, db_now)
                    row.execution_epoch += 1
                    self._clear_lease(row)
                    await self._close_pending_input(session, row, db_now)
                    payload: RunEventPayload = (
                        RunCancelledPayload()
                        if was_cancelling
                        else RunFailedPayload(safe_error_code="execution_interrupted")
                    )
                    await self._append_event(session, row, payload, db_now)
                    reclaimed.append(run_to_domain(row))
        for reclaimed_run in reclaimed:
            await self._wake_memory(reclaimed_run.owner_id)
        return tuple(reclaimed)

    async def cancel(
        self, session_id: str, owner_id: str, run_id: str, now: datetime
    ) -> Run | None:
        """在 Session 锁内串行取消排队/等待任务或请求 Worker 中断。"""
        async with self._session_factory() as session:
            async with session.begin():
                locked_session = await self._lock_session(session, session_id, owner_id)
                row = await session.scalar(
                    select(RunModel)
                    .where(
                        RunModel.id == run_id,
                        RunModel.session_id == session_id,
                        RunModel.owner_id == owner_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                if row.status in {
                    RunStatus.QUEUED.value,
                    RunStatus.WAITING_FOR_INPUT.value,
                    RunStatus.WAITING_FOR_APPROVAL.value,
                }:
                    await self._settle_unfinished_tool_calls(
                        session,
                        locked_session,
                        row,
                        now,
                    )
                    row.status = RunStatus.CANCELLED.value
                    row.finished_at = now
                    row.updated_at = now
                    self._mark_memory_pending(row, now)
                    await self._close_pending_input(session, row, now)
                    self._clear_lease(row)
                    payload: RunEventPayload = RunCancelledPayload()
                elif row.status == RunStatus.RUNNING.value:
                    row.status = RunStatus.CANCELLING.value
                    row.cancel_requested_at = now
                    row.updated_at = now
                    payload = RunCancelRequestedPayload()
                else:
                    return run_to_domain(row)
                await self._append_event(session, row, payload, now)
                result = run_to_domain(row)
        if result is not None and result.status is RunStatus.CANCELLED:
            await self._wake_memory(result.owner_id)
        return result

    async def retry(
        self,
        session_id: str,
        owner_id: str,
        source_run_id: str,
        request_id: str,
        fingerprint: str,
        new_run_id: str,
        prompt_template_version: str,
        now: datetime,
        confirm_unknown: bool = False,
        auth_session_id: str | None = None,
    ) -> Run | None:
        """幂等重试 failed Run，沿用原 trigger 和 cutoff、绑定当前默认版本。"""
        async with self._session_factory() as session:
            async with session.begin():
                locked_session = await self._lock_session(session, session_id, owner_id)
                source = await session.scalar(
                    select(RunModel)
                    .where(
                        RunModel.id == source_run_id,
                        RunModel.session_id == session_id,
                        RunModel.owner_id == owner_id,
                    )
                    .with_for_update()
                )
                if source is None:
                    return None
                replay = await self._find_request(
                    session, owner_id, session_id, RunRequestOperation.RETRY, request_id
                )
                if replay is not None:
                    self._check_fingerprint(replay, fingerprint)
                    run_id = RunRequestResult.model_validate(replay.result).run_id
                    row = await session.scalar(select(RunModel).where(RunModel.id == run_id))
                    result = None if row is None else run_to_domain(row)
                else:
                    if source.status != RunStatus.FAILED.value:
                        raise RunStateError("run_not_retryable")
                    has_unknown = await session.scalar(
                        select(RunToolCallModel.id)
                        .where(
                            RunToolCallModel.run_id == source.id,
                            RunToolCallModel.session_id == session_id,
                            RunToolCallModel.status == ToolCallStatus.UNKNOWN.value,
                        )
                        .limit(1)
                    )
                    if has_unknown is not None and not confirm_unknown:
                        raise ToolRetryConfirmationRequiredError("tool_retry_confirmation_required")
                    # Session 行锁串行化 Run 创建；此查询与 Run 列表使用相同排序，
                    # 确保只能基于用户当前看到的最新一条 Run 发起重试。
                    latest_run_id = await session.scalar(
                        select(RunModel.id)
                        .where(
                            RunModel.session_id == session_id,
                            RunModel.owner_id == owner_id,
                        )
                        .order_by(RunModel.created_at.desc(), RunModel.id.desc())
                        .limit(1)
                    )
                    if latest_run_id != source.id:
                        raise RunStateError("run_not_latest")
                    active = await session.scalar(
                        select(RunModel.id).where(
                            RunModel.session_id == session_id,
                            RunModel.status.in_(_ACTIVE_STATUSES),
                        )
                    )
                    if active is not None:
                        raise RunConflictError("run_already_active")
                    profile_id = await self._lock_default_profile(session)
                    compression_policy_id = await self._current_compression_policy_id(session)
                    system_prompts = await current_system_prompts(session)
                    if system_prompts is None:
                        raise RunStateError("system_prompt_unavailable")
                    selected_skill_ids = (
                        ()
                        if source.skill_snapshot is None
                        else RunSkillSnapshot.model_validate(
                            source.skill_snapshot
                        ).selected_skill_ids
                    )
                    skill_snapshot = await build_run_skill_snapshot(
                        session,
                        selected_skill_ids,
                    )
                    input_file_ids = await self._validated_input_file_ids(
                        session,
                        session_id,
                        owner_id,
                        existing=(),
                        incoming=tuple(source.input_file_ids),
                    )
                    inherited_skip = (
                        source.memory_extraction_status
                        == ExtractionStatus.SKIPPED.value
                    )
                    created_at = await self._next_run_created_at(session, session_id)
                    row = RunModel(
                        id=new_run_id,
                        session_id=session_id,
                        owner_id=owner_id,
                        status=RunStatus.QUEUED.value,
                        trigger_message_id=source.trigger_message_id,
                        context_cutoff_sequence=source.context_cutoff_sequence,
                        retry_of_run_id=source.id,
                        model_config_version_id=profile_id,
                        main_prompt_version_id=system_prompts.main.id,
                        compression_prompt_version_id=system_prompts.compression.id,
                        compression_policy_version_id=compression_policy_id,
                        skill_snapshot=skill_snapshot.model_dump(mode="json"),
                        input_file_ids=list(input_file_ids),
                        memory_project_id=locked_session.project_id,
                        memory_input_message_ids=[source.trigger_message_id],
                        memory_extraction_status=(
                            ExtractionStatus.SKIPPED.value
                            if inherited_skip
                            else None
                        ),
                        memory_extracted_at=now if inherited_skip else None,
                        memory_extraction_safe_error_code=(
                            source.memory_extraction_safe_error_code
                            or "memory_source_invalidated"
                            if inherited_skip
                            else None
                        ),
                        prompt_template_version=prompt_template_version,
                        event_sequence=0,
                        execution_epoch=0,
                        auth_session_id=auth_session_id,
                        llm_request_count=0,
                        queued_at=now,
                        created_at=created_at,
                        updated_at=max(now, created_at),
                        usage=RunUsage().model_dump(mode="json"),
                    )
                    session.add(row)
                    await session.flush()
                    await self._append_event(
                        session,
                        row,
                        RunQueuedPayload(trigger_message_id=source.trigger_message_id),
                        now,
                    )
                    await self._store_request(
                        session,
                        owner_id,
                        session_id,
                        RunRequestOperation.RETRY,
                        request_id,
                        fingerprint,
                        RunRequestResult(run_id=row.id),
                        now,
                    )
                    result = run_to_domain(row)
        return result

    def _mark_memory_pending(self, row: RunModel, terminal_at: datetime) -> None:
        """在 Run 终态事务内标记待提取，并保留已失效输入和固定上下文。"""
        if not self._memory_lifecycle_enabled:
            return
        if row.memory_extraction_status == ExtractionStatus.SKIPPED.value:
            row.memory_extracted_at = row.memory_extracted_at or terminal_at
            row.updated_at = terminal_at
            return
        row.memory_extraction_status = ExtractionStatus.PENDING.value
        row.memory_extracted_at = None
        row.memory_extraction_batch_id = None
        row.memory_extraction_safe_error_code = None
        row.updated_at = terminal_at

    async def _wake_memory(self, owner_id: str) -> None:
        """在 Run 事务提交后尽力唤醒固定用户任务，失败由扫描补偿。"""
        if self._memory_wakeup is None:
            return
        try:
            await self._memory_wakeup.wake_user(owner_id)
        except Exception as error:
            logger.warning(
                "memory_wakeup_failed owner_id=%s exception_type=%s",
                owner_id,
                type(error).__name__,
            )

    async def get(self, session_id: str, owner_id: str, run_id: str) -> Run | None:
        """通过未删除 Session 作用域读取 Run。"""
        async with self._session_factory() as session:
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
        return None if row is None else run_to_domain(row)

    async def list_runs(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_created_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> tuple[Run, ...] | None:
        """读取 owner 可见 Session 中稳定倒序排列的一页 Run。"""
        if not 1 <= limit <= 101:
            raise ValueError("run_list_limit_invalid")
        async with self._session_factory() as session:
            visible = await session.scalar(
                select(SessionModel.id).where(
                    SessionModel.id == session_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if visible is None:
                return None
            query = select(RunModel).where(
                RunModel.session_id == session_id,
                RunModel.owner_id == owner_id,
            )
            if before_created_at is not None and before_id is not None:
                query = query.where(
                    or_(
                        RunModel.created_at < before_created_at,
                        and_(
                            RunModel.created_at == before_created_at,
                            RunModel.id < before_id,
                        ),
                    )
                )
            rows = (
                await session.scalars(
                    query.order_by(RunModel.created_at.desc(), RunModel.id.desc()).limit(limit)
                )
            ).all()
        return tuple(run_to_domain(row) for row in rows)

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

    async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
        """确认执行权、租约和 deadline 仍有效且 Run 尚未取消。"""
        async with self._session_factory() as session:
            return bool(
                await session.scalar(
                    select(RunModel.id).where(
                        RunModel.id == run_id,
                        RunModel.execution_epoch == execution_epoch,
                        RunModel.status == RunStatus.RUNNING.value,
                        RunModel.lease_expires_at > func.clock_timestamp(),
                        RunModel.execution_deadline_at > func.clock_timestamp(),
                        RunModel.cancel_requested_at.is_(None),
                    )
                )
            )

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

    async def get_pending_input(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunInputRequest | None:
        """读取可见 Run 当前待回答的输入请求。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(RunInputRequestModel)
                .join(RunModel, RunModel.id == RunInputRequestModel.run_id)
                .join(SessionModel, SessionModel.id == RunModel.session_id)
                .where(
                    RunInputRequestModel.session_id == session_id,
                    RunInputRequestModel.run_id == run_id,
                    RunInputRequestModel.status == InputRequestStatus.PENDING.value,
                    RunModel.owner_id == owner_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
        return None if row is None else input_request_to_domain(row)

    async def get_input_call(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        input_request_id: str,
    ) -> RunInputCall | None:
        """读取 pending 或已回答请求对应的唯一模型调用。"""
        async with self._session_factory() as session:
            request = await session.scalar(
                select(RunInputRequestModel)
                .join(RunModel, RunModel.id == RunInputRequestModel.run_id)
                .join(SessionModel, SessionModel.id == RunModel.session_id)
                .where(
                    RunInputRequestModel.id == input_request_id,
                    RunInputRequestModel.session_id == session_id,
                    RunInputRequestModel.run_id == run_id,
                    RunInputRequestModel.status.in_(
                        [InputRequestStatus.PENDING.value, InputRequestStatus.ANSWERED.value]
                    ),
                    RunModel.owner_id == owner_id,
                    SessionModel.owner_id == owner_id,
                    SessionModel.deleted_time.is_(None),
                )
            )
            if request is None:
                return None
            return await self._input_call_for_question(
                session, run_id, session_id, request.question_message_id
            )

    @staticmethod
    async def _input_call_for_question(
        session: AsyncSession,
        run_id: str,
        session_id: str,
        question_message_id: str,
    ) -> RunInputCall | None:
        rows = (
            await session.scalars(
                select(ExecutionMessageModel).where(
                    ExecutionMessageModel.run_id == run_id,
                    ExecutionMessageModel.session_id == session_id,
                    ExecutionMessageModel.source_message_id == question_message_id,
                    ExecutionMessageModel.role == ExecutionRole.ASSISTANT.value,
                )
            )
        ).all()
        calls = [
            (row, part)
            for row in rows
            for part in execution_to_domain(row).parts
            if isinstance(part, ToolCallPart) and part.name == "request_user_input"
        ]
        if len(calls) != 1:
            return None
        row, call = calls[0]
        return RunInputCall(
            tool_call_id=call.tool_call_id,
            thread_id=row.thread_id,
            question_message_id=question_message_id,
        )

    @staticmethod
    def _single_tool_call(message: ExecutionMessageDraft) -> ToolCallPart:
        """Require exactly one business call in the assistant execution message."""
        calls = [part for part in message.parts if isinstance(part, ToolCallPart)]
        if (
            len(calls) != 1
            or any(isinstance(part, ToolResultPart) for part in message.parts)
            or calls[0].name == "request_user_input"
        ):
            raise RunStateError("run_tool_call_message_invalid")
        return calls[0]

    @staticmethod
    def _validate_tool_result_message(
        record: RunToolCallModel,
        message: ExecutionMessageDraft,
        run: RunModel,
        outcome: ToolOutcome,
    ) -> None:
        """Require one matching result whose protocol shape is the stored outcome."""
        results = [part for part in message.parts if isinstance(part, ToolResultPart)]
        if (
            message.session_id != run.session_id
            or message.owner_id != run.owner_id
            or message.run_id != run.id
            or message.role is not ExecutionRole.TOOL
            or message.tool_call_id != record.tool_call_id
            or len(results) != 1
            or results[0].tool_call_id != record.tool_call_id
            or results[0].status is not protocol_status_for_outcome(outcome)
            or results[0].output != outcome.canonical_output()
        ):
            raise RunStateError("run_tool_result_message_invalid")

    @staticmethod
    def _validated_pending_artifacts(
        record: RunToolCallModel,
        message: ExecutionMessageDraft,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> tuple[PendingSessionFile, ...]:
        """确保待发布文件与唯一 TOOL 结果中的有序引用完全一致。"""
        if len(artifacts) > 100:
            raise RunStateError("session_file_quota_exceeded")
        if tuple(item.output_index for item in artifacts) != tuple(range(len(artifacts))):
            raise RunStateError("run_tool_artifacts_invalid")
        if len({item.id for item in artifacts}) != len(artifacts) or len(
            {item.object_key for item in artifacts}
        ) != len(artifacts):
            raise RunStateError("run_tool_artifacts_invalid")
        result = next(
            part for part in message.parts if isinstance(part, ToolResultPart)
        )
        expected = tuple(
            ArtifactFileRef(
                file_id=item.id,
                filename=item.filename,
                label=item.label,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
            )
            for item in artifacts
        )
        if result.artifacts != expected:
            raise RunStateError("run_tool_result_message_invalid")
        return artifacts

    @staticmethod
    def _validate_delivery_message(
        record: RunToolCallModel,
        message: ConversationMessageDraft | None,
        artifacts: tuple[PendingSessionFile, ...],
        run: RunModel,
    ) -> None:
        """产物必须同时形成一条安全、可恢复的普通助手文件引用消息。"""
        if not artifacts:
            if message is not None:
                raise RunStateError("run_tool_delivery_message_invalid")
            return
        if message is None:
            raise RunStateError("run_tool_delivery_message_invalid")
        references = tuple(
            ArtifactFileRef(
                file_id=file.id,
                filename=file.filename,
                label=file.label,
                mime_type=file.mime_type,
                size_bytes=file.size_bytes,
            )
            for file in artifacts
        )
        file_parts = tuple(part for part in message.parts if isinstance(part, FileRefPart))
        expected_parts = tuple(
            FileRefPart(
                file_id=item.file_id,
                filename=item.filename,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
            )
            for item in references
        )
        if (
            message.session_id != run.session_id
            or message.owner_id != run.owner_id
            or message.run_id != run.id
            or message.role is not ConversationRole.ASSISTANT
            or file_parts != expected_parts
        ):
            raise RunStateError("run_tool_delivery_message_invalid")

    @staticmethod
    async def _delivery_message_matches(
        session: AsyncSession,
        message: ConversationMessageDraft | None,
    ) -> bool:
        """重放已终态调用时核对已提交的普通助手交付消息。"""
        if message is None:
            return True
        existing = await session.get(ConversationMessageModel, message.id)
        if existing is None:
            return False
        domain = conversation_to_domain(existing)
        return (
            domain.session_id == message.session_id
            and domain.role is message.role
            and domain.status is message.status
            and domain.parts == message.parts
            and domain.run_id == message.run_id
        )

    @staticmethod
    async def _finish_skill_execution_in_transaction(
        session: AsyncSession,
        record: RunToolCallModel,
        run: RunModel,
        completion: SkillExecutionCompletion,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> None:
        """脚本审计记录与文件、结果消息和调用终态使用同一事务。"""
        skill_execution = await session.scalar(
            select(SkillExecutionModel)
            .where(
                SkillExecutionModel.id == completion.execution_id,
                SkillExecutionModel.run_id == run.id,
                SkillExecutionModel.session_id == run.session_id,
                SkillExecutionModel.tool_call_record_id == record.id,
            )
            .with_for_update()
        )
        if skill_execution is None:
            raise RunStateError("skill_execution_record_missing")
        if skill_execution.status != SkillExecutionStatus.RUNNING.value:
            if not _skill_execution_row_matches(
                skill_execution,
                completion,
                artifacts,
            ):
                raise RunConflictError("skill_execution_completion_conflict")
            return
        skill_execution.status = completion.status.value
        skill_execution.log_object_key = completion.log_object_key
        skill_execution.log_truncated = completion.log_truncated
        skill_execution.log_error_code = completion.log_error_code
        skill_execution.error_code = completion.error_code
        skill_execution.cleanup_error_code = completion.cleanup_error_code
        skill_execution.result_artifacts = [
            SkillExecutionArtifact(
                file_id=artifact.id,
                filename=artifact.filename,
                label=artifact.label,
                mime_type=artifact.mime_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
                object_key=artifact.object_key,
                output_index=artifact.output_index,
            ).model_dump(mode="json")
            for artifact in artifacts
        ]
        skill_execution.finished_at = completion.finished_at
        await session.flush()

    @staticmethod
    async def _skill_completion_matches(
        session: AsyncSession,
        record: RunToolCallModel,
        completion: SkillExecutionCompletion | None,
        artifacts: tuple[PendingSessionFile, ...],
        *,
        outcome: ToolOutcome,
    ) -> bool:
        """已提交工具调用重放时核对脚本审计结论，避免返回冲突版本。"""
        if completion is None:
            if record.tool_id != SKILL_EXECUTE_TOOL_ID:
                return True
            # 启动结果无法确认时没有可信 execution_id，完成路径会按调用身份
            # 收口 running 审计后再提交 UNKNOWN；重放必须接受“无审计记录”或
            # “审计已是终态”，但不能接受仍遗留 running 的不一致状态。
            if outcome.status is not ToolOutcomeStatus.UNKNOWN:
                return False
            status = await session.scalar(
                select(SkillExecutionModel.status).where(
                    SkillExecutionModel.tool_call_record_id == record.id
                )
            )
            return (
                status is None
                or status != SkillExecutionStatus.RUNNING.value
            )
        row = await session.scalar(
            select(SkillExecutionModel).where(
                SkillExecutionModel.id == completion.execution_id,
                SkillExecutionModel.tool_call_record_id == record.id,
            )
        )
        return row is not None and _skill_execution_row_matches(
            row,
            completion,
            artifacts,
        )

    @staticmethod
    async def _refresh_safe_skill_projection(
        session: AsyncSession,
        row: RunToolCallModel,
    ) -> SafeSkillApprovalProjection | None:
        """按冻结摘要重新核对输入文件，不读取当前 Skill 管理配置。"""
        if row.safe_skill_projection is None:
            return None
        projection = SafeSkillApprovalProjection.model_validate(
            row.safe_skill_projection
        )
        if not projection.input_files:
            return projection
        file_ids = tuple(item.file_id for item in projection.input_files)
        visible_ids = set(
            (
                await session.scalars(
                    select(SessionFileModel.id).where(
                        SessionFileModel.session_id == row.session_id,
                        SessionFileModel.id.in_(file_ids),
                    )
                )
            ).all()
        )
        files = tuple(
            item.model_copy(update={"available": item.file_id in visible_ids})
            for item in projection.input_files
        )
        all_available = all(item.available for item in files)
        return projection.model_copy(
            update={
                "input_files": files,
                "can_approve": projection.can_approve and all_available,
                "unavailable_reason": (
                    projection.unavailable_reason
                    if all_available
                    else "skill_approval_input_unavailable"
                ),
            }
        )

    @staticmethod
    async def _artifact_rows_match(
        session: AsyncSession,
        record: RunToolCallModel,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> bool:
        """确认已完成调用已发布的文件 ID 和顺序与本次重放命令一致。"""
        rows = list(
            (
                await session.scalars(
                    select(SessionFileModel)
                    .where(SessionFileModel.source_call_id == record.id)
                    .order_by(SessionFileModel.output_index)
                )
            ).all()
        )
        if len(rows) != len(artifacts):
            return False
        return all(
            row.id == artifact.id
            and row.filename == artifact.filename
            and row.label == artifact.label
            and row.mime_type == artifact.mime_type
            and row.size_bytes == artifact.size_bytes
            and row.sha256 == artifact.sha256
            and row.object_key == artifact.object_key
            and row.output_index == artifact.output_index
            for row, artifact in zip(rows, artifacts, strict=True)
        )

    @staticmethod
    async def _has_unsettled_tool_calls(session: AsyncSession, run_id: str) -> bool:
        return bool(
            await session.scalar(
                select(RunToolCallModel.id)
                .where(
                    RunToolCallModel.run_id == run_id,
                    RunToolCallModel.status.in_(
                        (
                            ToolCallStatus.PENDING_APPROVAL.value,
                            ToolCallStatus.APPROVED.value,
                            ToolCallStatus.EXECUTING.value,
                        )
                    ),
                )
                .limit(1)
            )
        )

    async def _settle_unfinished_tool_calls(
        self,
        session: AsyncSession,
        locked_session: SessionModel,
        run: RunModel,
        settled_at: datetime,
        *,
        records: list[RunToolCallModel] | None = None,
    ) -> None:
        """Cancel undispatched and mark already-started calls unknown."""
        # 由调用方持有事务与 Session→Run 锁；这里只收敛未完成调用，不提交事务。
        if records is None:
            # 未由调用方预先锁定时，在事务内按调用序号加行锁读取全部未完成调用，
            # 避免与并发审批或工具执行产生状态竞争。
            records = list(
                (
                    await session.scalars(
                        select(RunToolCallModel)
                        .where(
                            RunToolCallModel.run_id == run.id,
                            RunToolCallModel.status.in_(
                                (
                                    ToolCallStatus.PENDING_APPROVAL.value,
                                    ToolCallStatus.APPROVED.value,
                                    ToolCallStatus.EXECUTING.value,
                                )
                            ),
                        )
                        .order_by(RunToolCallModel.ordinal)
                        .with_for_update()
                    )
                ).all()
            )
        for record in records:
            # 幂等防线：等待行锁期间调用可能已被并发流程收敛为终态，直接跳过。
            if record.status not in {
                ToolCallStatus.PENDING_APPROVAL.value,
                ToolCallStatus.APPROVED.value,
                ToolCallStatus.EXECUTING.value,
            }:
                continue
            # 结果消息需沿用调用消息的 thread 并保持调用/结果协议配对；
            # 找不到调用消息说明持久化数据不一致，不能静默继续。
            call_message = await session.get(ExecutionMessageModel, record.call_message_id)
            if call_message is None:
                raise RuntimeError("run_tool_call_message_missing")
            if record.status == ToolCallStatus.EXECUTING.value:
                # 已进入 EXECUTING 的调用可能已产生外部副作用，但结果无法确认：
                # 优先使用已持久化的 outcome，否则标记 UNKNOWN，绝不重新派发。
                outcome = (
                    ToolOutcome.model_validate(record.outcome)
                    if record.outcome is not None
                    else ToolOutcome(
                        status=ToolOutcomeStatus.UNKNOWN,
                        code="tool_outcome_unknown",
                        message="工具执行结果无法确认。",
                    )
                )
                persisted_status = persisted_status_for_outcome(outcome)
            else:
                # 仍待审批或已批准但未派发的调用没有外部副作用，可安全标记为未执行。
                outcome = ToolOutcome(
                    status=ToolOutcomeStatus.CANCELLED,
                    code="tool_not_executed",
                    message="该工具调用未执行。",
                )
                persisted_status = ToolCallStatus.CANCELLED
            if record.result_message_id is None:
                # 为没有结果的调用补写 TOOL 结果消息，保持每条调用都有配对的协议结果。
                result_message_id = new_id("exec")
                result_draft = ExecutionMessageDraft(
                    id=result_message_id,
                    session_id=run.session_id,
                    owner_id=run.owner_id,
                    run_id=run.id,
                    thread_id=call_message.thread_id,
                    role=ExecutionRole.TOOL,
                    parts=(
                        ToolResultPart(
                            tool_call_id=record.tool_call_id,
                            status=ToolResultStatus.ERROR,
                            output=outcome.canonical_output(),
                        ),
                    ),
                    tool_call_id=record.tool_call_id,
                    source_message_id=None,
                    created_at=settled_at,
                    metadata=MessageMetadata(),
                )
                await self._messages.append_execution_in_session(
                    session,
                    result_draft,
                    locked_session=locked_session,
                    managed_run_id=run.id,
                )
                record.result_message_id = result_message_id
                record.outcome = outcome.model_dump(mode="json")
            # 落库终态与安全错误码；成功结果不写 safe_error_code。
            record.status = persisted_status.value
            record.finished_at = settled_at
            record.safe_error_code = (
                outcome.code if outcome.status is not ToolOutcomeStatus.OK else None
            )
            if record.status == ToolCallStatus.UNKNOWN.value:
                # 脚本附加记录不能比工具调用更早变为可见终态；二者正常完成时
                # 同事务提交，这里只收敛此前事务失败或进程中断遗留的 running。
                await self._settle_running_skill_execution(
                    session,
                    record.id,
                    settled_at,
                )
            # 每个调用都追加终态事件，保证 SSE 与读取模型看到一致的收敛结果。
            await self._append_event(
                session,
                run,
                RunToolCallFinishedPayload(
                    call_id=record.id,
                    ordinal=record.ordinal,
                    status=persisted_status,
                    round=record.round,
                    safe_error_code=record.safe_error_code,
                ),
                settled_at,
            )

    @staticmethod
    async def _settle_running_skill_execution(
        session: AsyncSession,
        tool_call_record_id: str,
        settled_at: datetime,
    ) -> None:
        """按工具调用身份收敛遗留 running 审计，不使用回查失败时的新 ID。"""
        skill_execution = await session.scalar(
            select(SkillExecutionModel)
            .where(
                SkillExecutionModel.tool_call_record_id == tool_call_record_id,
                SkillExecutionModel.status == SkillExecutionStatus.RUNNING.value,
            )
            .with_for_update()
        )
        if skill_execution is not None:
            skill_execution.status = SkillExecutionStatus.UNKNOWN.value
            skill_execution.error_code = "tool_outcome_unknown"
            skill_execution.finished_at = settled_at

    async def _fail_run_in_transaction(
        self,
        session: AsyncSession,
        locked_session: SessionModel,
        run: RunModel,
        safe_error_code: str,
        failed_at: datetime,
    ) -> None:
        """Converge unfinished calls before committing the failed terminal state."""
        await self._settle_unfinished_tool_calls(
            session,
            locked_session,
            run,
            failed_at,
        )
        run.status = RunStatus.FAILED.value
        run.safe_error_code = safe_error_code
        run.finished_at = failed_at
        run.updated_at = failed_at
        self._mark_memory_pending(run, failed_at)
        self._clear_lease(run)
        await self._close_pending_input(session, run, failed_at)
        await self._append_event(
            session,
            run,
            RunFailedPayload(safe_error_code=safe_error_code),
            failed_at,
        )

    async def _lock_default_profile(self, session: AsyncSession) -> str:
        profile_id = await session.scalar(
            select(DefaultModelProfileModel.profile_id).where(
                DefaultModelProfileModel.singleton_id == 1
            )
        )
        if profile_id is None:
            raise RunStateError("llm_default_profile_unavailable")
        profile = await session.scalar(
            select(ModelProfileVersionModel)
            .where(
                ModelProfileVersionModel.id == profile_id,
            )
            .with_for_update()
        )
        default = await session.scalar(
            select(DefaultModelProfileModel)
            .where(DefaultModelProfileModel.singleton_id == 1)
            .with_for_update()
        )
        if default is None or default.profile_id != profile_id:
            raise RunConflictError("llm_default_profile_changed")
        if profile is None:
            raise RunStateError("llm_default_profile_unavailable")
        if not profile.enabled or not profile.supports_stream or not profile.supports_tools:
            raise RunStateError("llm_default_profile_unavailable")
        if (
            profile.api_key_ciphertext is None
            or profile.api_key_nonce is None
            or profile.api_key_key_version is None
        ):
            raise RunStateError("llm_default_profile_unavailable")
        return profile.id

    @staticmethod
    async def _current_compression_policy_id(session: AsyncSession) -> str | None:
        """读取当前策略版本，供新 Run 固定配置快照。"""
        return await session.scalar(
            select(CurrentCompressionPolicyModel.policy_version_id).where(
                CurrentCompressionPolicyModel.singleton_id == 1
            )
        )

    @staticmethod
    async def _next_run_created_at(session: AsyncSession, session_id: str) -> datetime:
        """在 Session 锁内生成严格晚于已有 Run 的创建时间。"""
        database_now = cast(datetime | None, await session.scalar(select(func.clock_timestamp())))
        latest_created_at = cast(
            datetime | None,
            await session.scalar(
                select(func.max(RunModel.created_at)).where(RunModel.session_id == session_id)
            ),
        )
        if database_now is None:
            raise RuntimeError("run_database_clock_unavailable")
        if latest_created_at is None or database_now > latest_created_at:
            return database_now
        return latest_created_at + timedelta(microseconds=1)

    async def _validated_input_file_ids(
        self,
        session: AsyncSession,
        session_id: str,
        owner_id: str,
        *,
        existing: tuple[str, ...],
        incoming: tuple[str, ...],
    ) -> tuple[str, ...]:
        """在同一 Run 事务中验证文件归属，并按首次提供顺序合并。"""
        if len(incoming) != len(set(incoming)):
            raise RunInputResourceError("run_input_file_ids_duplicate")
        ordered = tuple(dict.fromkeys((*existing, *incoming)))
        if len(ordered) != len(existing) + len(incoming):
            raise RunInputResourceError("run_input_file_ids_duplicate")
        if not incoming:
            return ordered
        visible = set(
            (
                await session.scalars(
                    select(SessionFileModel.id).where(
                        SessionFileModel.id.in_(incoming),
                        SessionFileModel.session_id == session_id,
                        SessionFileModel.owner_id == owner_id,
                    )
                )
            ).all()
        )
        if visible != set(incoming):
            raise RunInputResourceError("run_input_file_unavailable")
        return ordered

    @staticmethod
    async def _lock_session(session: AsyncSession, session_id: str, owner_id: str) -> SessionModel:
        row = await session.scalar(
            select(SessionModel)
            .where(
                SessionModel.id == session_id,
                SessionModel.owner_id == owner_id,
                SessionModel.deleted_time.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            raise SessionNotFoundError("session_not_found")
        return row

    @staticmethod
    async def _lock_session_by_id(session: AsyncSession, session_id: str) -> SessionModel:
        row = await session.scalar(
            select(SessionModel)
            .where(SessionModel.id == session_id, SessionModel.deleted_time.is_(None))
            .with_for_update()
        )
        if row is None:
            raise SessionNotFoundError("session_not_found")
        return row

    @staticmethod
    async def _lock_run(session: AsyncSession, session_id: str, run_id: str) -> RunModel:
        row = await session.scalar(
            select(RunModel)
            .where(RunModel.id == run_id, RunModel.session_id == session_id)
            .with_for_update()
        )
        if row is None:
            raise RunNotFoundError("run_not_found")
        return row

    async def _lock_worker_run(
        self, session: AsyncSession, run_id: str, worker_id: str, epoch: int
    ) -> RunModel:
        session_id = await session.scalar(select(RunModel.session_id).where(RunModel.id == run_id))
        if session_id is None:
            raise RunNotFoundError("run_not_found")
        await self._lock_session_by_id(session, session_id)
        row = await self._lock_run(session, session_id, run_id)
        if row.worker_id != worker_id or row.execution_epoch != epoch:
            raise RunStateError("run_execution_lease_lost")
        return row

    @staticmethod
    def _lease_is_valid(row: RunModel, worker_id: str, epoch: int, db_now: datetime | None) -> bool:
        return bool(
            db_now is not None
            and row.worker_id == worker_id
            and row.execution_epoch == epoch
            and row.lease_expires_at is not None
            and row.lease_expires_at > db_now
        )

    @staticmethod
    def _work_deadline_is_valid(row: RunModel, db_now: datetime | None) -> bool:
        """判断当前执行是否仍可继续产生业务结果。"""
        return bool(
            db_now is not None
            and row.execution_deadline_at is not None
            and row.execution_deadline_at > db_now
        )

    @staticmethod
    def _clear_lease(row: RunModel) -> None:
        row.worker_id = None
        row.lease_expires_at = None
        row.execution_started_at = None
        row.execution_deadline_at = None

    @staticmethod
    async def _append_event(
        session: AsyncSession,
        run: RunModel,
        payload: RunEventPayload,
        created_at: datetime,
    ) -> None:
        run.event_sequence += 1
        event = RunEventModel(
            id=new_id("runevt"),
            run_id=run.id,
            session_id=run.session_id,
            sequence=run.event_sequence,
            type=payload.type,
            created_at=created_at,
            payload=event_payload_to_json(payload),
        )
        session.add(event)
        await session.flush()

    @staticmethod
    async def _close_pending_input(
        session: AsyncSession, run: RunModel, closed_at: datetime
    ) -> None:
        request = await session.scalar(
            select(RunInputRequestModel)
            .where(
                RunInputRequestModel.run_id == run.id,
                RunInputRequestModel.status == InputRequestStatus.PENDING.value,
            )
            .with_for_update()
        )
        if request is not None:
            request.status = InputRequestStatus.CLOSED.value
            request.closed_at = closed_at

    @staticmethod
    async def _find_request(
        session: AsyncSession,
        owner_id: str,
        session_id: str,
        operation: RunRequestOperation,
        request_id: str,
    ) -> RunRequestModel | None:
        return await session.scalar(
            select(RunRequestModel).where(
                RunRequestModel.owner_id == owner_id,
                RunRequestModel.session_id == session_id,
                RunRequestModel.operation == operation.value,
                RunRequestModel.request_id == request_id,
            )
        )

    @staticmethod
    def _check_fingerprint(row: RunRequestModel, fingerprint: str) -> None:
        if row.fingerprint != fingerprint:
            raise RunConflictError("run_request_id_conflict")

    @staticmethod
    async def _store_request(
        session: AsyncSession,
        owner_id: str,
        session_id: str,
        operation: RunRequestOperation,
        request_id: str,
        fingerprint: str,
        result: RunRequestResult,
        created_at: datetime,
    ) -> None:
        session.add(
            RunRequestModel(
                id=new_id("runreq"),
                owner_id=owner_id,
                session_id=session_id,
                operation=operation.value,
                request_id=request_id,
                fingerprint=fingerprint,
                result=result.model_dump(mode="json", exclude_none=True),
                created_at=created_at,
            )
        )
        await session.flush()

    @staticmethod
    async def _load_submit_result(
        session: AsyncSession, request: RunRequestModel
    ) -> tuple[Run, ConversationMessage]:
        result = RunRequestResult.model_validate(request.result)
        run = await session.get(RunModel, result.run_id)
        message = await session.get(ConversationMessageModel, result.message_id)
        if run is None or message is None:
            raise RunNotFoundError("run_request_result_missing")
        return run_to_domain(run), conversation_to_domain(message)


def _skill_execution_row_matches(
    row: SkillExecutionModel,
    completion: SkillExecutionCompletion,
    artifacts: tuple[PendingSessionFile, ...],
) -> bool:
    """比较脚本审计终态，重放不能替换日志、错误或产物集合。"""
    expected_artifacts = [
        SkillExecutionArtifact(
            file_id=artifact.id,
            filename=artifact.filename,
            label=artifact.label,
            mime_type=artifact.mime_type,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            object_key=artifact.object_key,
            output_index=artifact.output_index,
        ).model_dump(mode="json")
        for artifact in artifacts
    ]
    return (
        row.status == completion.status.value
        and row.log_object_key == completion.log_object_key
        and row.log_truncated == completion.log_truncated
        and row.log_error_code == completion.log_error_code
        and row.error_code == completion.error_code
        and row.cleanup_error_code == completion.cleanup_error_code
        and row.result_artifacts == expected_artifacts
        and row.finished_at == completion.finished_at
    )


__all__ = ["SqlAlchemyRunRepository"]
