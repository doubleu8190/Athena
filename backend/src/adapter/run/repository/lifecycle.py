"""Run 生命周期、租约抢占与输入澄清的 PostgreSQL 子仓储。"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from adapter.llm.model.profile import (
    ModelProfileVersionModel,
)
from adapter.message.converter.message import conversation_to_domain, execution_to_domain
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.run.converter import (
    input_request_to_domain,
    run_to_domain,
)
from adapter.run.model import (
    RunInputRequestModel,
    RunModel,
    RunToolCallModel,
)
from adapter.run.repository._shared import RunRepositorySupportMixin
from adapter.session.model import SessionModel
from adapter.skill.repository import build_run_skill_snapshot
from adapter.subagent.model import SubagentModel
from adapter.system_prompt.repository import current_system_prompts
from common.ids import new_id, now_utc
from domain.memory import ExtractionStatus
from domain.memory.lifecycle import RunMemoryContextState
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageStatus,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from domain.run.model import (
    MAX_RUN_SSE_SNAPSHOT_BYTES,
    AnswerRunCommand,
    BeginSubagentBatchCommand,
    BeginSubagentBatchResult,
    FailRunCommand,
    FinishRunCommand,
    InitializeRunMemoryContextCommand,
    InputRequestStatus,
    RequestRunInputCommand,
    Run,
    RunCancelledPayload,
    RunCancelRequestedPayload,
    RunCompletedPayload,
    RunEventPayload,
    RunFailedPayload,
    RunInputCall,
    RunInputInteraction,
    RunInputRequest,
    RunLease,
    RunQueuedPayload,
    RunRequestOperation,
    RunRequestResult,
    RunResumedPayload,
    RunStartedPayload,
    RunStatus,
    RunUsage,
    RunWaitingForSubagentsPayload,
    RunWaitingPayload,
    SubmitRunCommand,
    SubmitRunResult,
)
from domain.run.model.errors import (
    RunConflictError,
    RunNotFoundError,
    RunStateError,
    ToolRetryConfirmationRequiredError,
)
from domain.skill import (
    RunSkillSnapshot,
)
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID
from domain.tool import (
    InitializeRunToolsCommand,
    ReserveLlmRequestCommand,
    RuntimeToolSnapshot,
    ToolCallStatus,
    ToolKind,
)

_ACTIVE_STATUSES = (
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_FOR_INPUT.value,
    RunStatus.WAITING_FOR_APPROVAL.value,
    RunStatus.WAITING_FOR_SUBAGENTS.value,
    RunStatus.CANCELLING.value,
)
_STREAM_SNAPSHOT_MESSAGES = 8
_STREAM_SNAPSHOT_EVENTS = 100
_STREAM_SNAPSHOT_BYTES = MAX_RUN_SSE_SNAPSHOT_BYTES
_STREAM_SNAPSHOT_PREFIX_RESERVE = 128
_MAX_TOOL_BATCH_SIZE = 8
logger = logging.getLogger(__name__)


class RunLifecycleMixin(RunRepositorySupportMixin):
    """负责 Run 创建、领取、心跳、澄清与终态收敛。"""

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
                    input_file_ids = await self._validate_and_merge_input_file_ids(
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
                    profile_id = await self._lock_default_profile_id(session)
                    compression_policy_id = await self._current_compression_policy_id(session)
                    system_prompts = await current_system_prompts(session)
                    if system_prompts is None:
                        raise RunStateError("system_prompt_unavailable")
                    message = await self._message_repo.append_conversation_in_session(
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
                    input_file_ids = await self._validate_and_merge_input_file_ids(
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
                    answer = await self._message_repo.append_conversation_in_session(
                        session,
                        command.answer_message,
                        locked_session=locked_session,
                        managed_run_id=row.id,
                    )
                    await self._message_repo.append_execution_in_session(
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
                    # 仅主 Run 的用户回答参与记忆抽取；子 Run 定向回答不写入主 Run 记忆输入。
                    if row.depth == 0 and answer.id not in row.memory_input_message_ids:
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
                    # 仅校验启用状态与能力，使用 FOR SHARE 允许并发认领，仍与管理员禁用/修改互斥。
                    profile = await session.scalar(
                        select(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == row.model_config_version_id)
                        .with_for_update(read=True)
                    )
                    db_now = await session.scalar(select(func.clock_timestamp()))
                    if db_now is None:
                        raise RuntimeError("run_database_clock_unavailable")
                    if db_now.tzinfo is None:
                        db_now = db_now.replace(tzinfo=UTC)
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
                        await self._try_wake_parent_run(session, locked_session, row, db_now)
                        if row.depth == 0:
                            memory_wake_owners.append(row.owner_id)
                        continue
                    row.status = RunStatus.RUNNING.value
                    row.execution_epoch += 1
                    row.worker_id = worker_id
                    row.execution_started_at = db_now
                    row.first_started_at = row.first_started_at or db_now
                    row.lease_expires_at = db_now + timedelta(seconds=lease_seconds)
                    timeout_limit = min(execution_seconds, profile.execution_timeout_seconds)
                    if row.timeout_seconds is not None:
                        timeout_limit = min(timeout_limit, row.timeout_seconds)
                    row.execution_deadline_at = db_now + timedelta(seconds=timeout_limit)
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
                            lease_expires_at=row.lease_expires_at.replace(tzinfo=UTC)
                            if row.lease_expires_at.tzinfo is None
                            else row.lease_expires_at,
                            execution_deadline_at=row.execution_deadline_at.replace(tzinfo=UTC)
                            if row.execution_deadline_at.tzinfo is None
                            else row.execution_deadline_at,
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
                context_state = RunMemoryContextState.model_validate(row.memory_context_state or {})
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
        if not 1 <= command.maximum_count <= 50:
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
                question = await self._message_repo.append_conversation_in_session(
                    session,
                    command.question_message,
                    locked_session=locked_session,
                    managed_run_id=row.id,
                )
                for execution in command.execution_messages:
                    await self._message_repo.append_execution_in_session(
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
                    await self._try_wake_parent_run(
                        session, locked_session, row, command.finished_at
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
                    if row.depth >= 1:
                        # 子 Run 输出仅写入隔离的 execution_messages，不污染主会话对话历史。
                        reply_exec = ExecutionMessageDraft(
                            id=new_id("exec"),
                            session_id=row.session_id,
                            owner_id=row.owner_id,
                            run_id=row.id,
                            thread_id=row.id,
                            role=ExecutionRole.ASSISTANT,
                            parts=command.reply_message.parts,
                            tool_call_id=None,
                            source_message_id=None,
                            created_at=command.finished_at,
                            metadata=command.reply_message.metadata,
                        )
                        persisted_reply = await self._message_repo.append_execution_in_session(
                            session,
                            reply_exec,
                            locked_session=locked_session,
                            managed_run_id=row.id,
                        )
                        assistant_message_id = persisted_reply.id
                    else:
                        reply = await self._message_repo.append_conversation_in_session(
                            session,
                            command.reply_message,
                            locked_session=locked_session,
                            managed_run_id=row.id,
                        )
                        assistant_message_id = reply.id
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
                        RunCompletedPayload(assistant_message_id=assistant_message_id),
                        command.finished_at,
                    )
                    await self._try_wake_parent_run(
                        session, locked_session, row, command.finished_at
                    )
                    result = run_to_domain(row)
        if result is not None and result.depth == 0:
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
                if command.context_stop_reason is not None and not self._work_deadline_is_valid(
                    row, db_now
                ):
                    return None
                if row.status != RunStatus.RUNNING.value or row.cancel_requested_at is not None:
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
                    persisted_report = await self._message_repo.append_conversation_in_session(
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
                await self._try_wake_parent_run(session, locked_session, row, command.failed_at)
                result = run_to_domain(row)
        if result is not None and result.depth == 0:
            await self._wake_memory(result.owner_id)
        return result

    async def settle_cancelled(
        self, run_id: str, worker_id: str, execution_epoch: int, settled_at: datetime
    ) -> Run | None:
        """由持有有效租约的 Worker 确认 cancelling Run 已清理结束，原子收敛为 cancelled 终态。"""
        async with self._session_factory() as session:
            async with session.begin():
                # 校验租约与 cancelling 状态，未满足时返回 None
                row = await self._lock_worker_run(session, run_id, worker_id, execution_epoch)
                db_now = await session.scalar(select(func.clock_timestamp()))
                if not self._lease_is_valid(row, worker_id, execution_epoch, db_now):
                    return None
                if row.status != RunStatus.CANCELLING.value:
                    return None
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                # 终态清理未完成的工具调用并更新 Run 状态为 CANCELLED
                await self._settle_unfinished_tool_calls(
                    session,
                    locked_session,
                    row,
                    settled_at,
                )
                row.status = RunStatus.CANCELLED.value
                row.finished_at = settled_at
                row.updated_at = settled_at
                self._mark_memory_pending(row, settled_at)
                self._clear_lease(row)
                await self._close_pending_input(session, row, settled_at)
                await self._append_event(session, row, RunCancelledPayload(), settled_at)
                await self._try_wake_parent_run(session, locked_session, row, settled_at)
                result = run_to_domain(row)
        if result is not None and result.depth == 0:
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
                    await self._try_wake_parent_run(session, locked_session, row, db_now)
                    reclaimed.append(run_to_domain(row))
        for reclaimed_run in reclaimed:
            if reclaimed_run.depth == 0:
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
                    RunStatus.WAITING_FOR_SUBAGENTS.value,
                }:
                    was_waiting_subagents = row.status == RunStatus.WAITING_FOR_SUBAGENTS.value
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
                    if was_waiting_subagents:
                        # 父 Run 取消时级联取消子 Run：等待中直接取消，running 转 cancelling。
                        active_children = (
                            await session.scalars(
                                select(RunModel)
                                .where(
                                    RunModel.parent_run_id == row.id,
                                    RunModel.session_id == session_id,
                                    RunModel.status.in_(
                                        (
                                            RunStatus.QUEUED.value,
                                            RunStatus.RUNNING.value,
                                            RunStatus.WAITING_FOR_INPUT.value,
                                            RunStatus.WAITING_FOR_APPROVAL.value,
                                        )
                                    ),
                                )
                                .with_for_update()
                            )
                        ).all()
                        for child in active_children:
                            if child.status == RunStatus.RUNNING.value:
                                child.status = RunStatus.CANCELLING.value
                                child.cancel_requested_at = now
                                child.updated_at = now
                                await self._append_event(
                                    session, child, RunCancelRequestedPayload(), now
                                )
                            else:
                                await self._settle_unfinished_tool_calls(
                                    session, locked_session, child, now
                                )
                                child.status = RunStatus.CANCELLED.value
                                child.finished_at = now
                                child.updated_at = now
                                self._mark_memory_pending(child, now)
                                await self._close_pending_input(session, child, now)
                                self._clear_lease(child)
                                await self._append_event(session, child, RunCancelledPayload(), now)
                elif row.status == RunStatus.RUNNING.value:
                    row.status = RunStatus.CANCELLING.value
                    row.cancel_requested_at = now
                    row.updated_at = now
                    payload = RunCancelRequestedPayload()
                else:
                    return run_to_domain(row)
                await self._append_event(session, row, payload, now)
                if row.status == RunStatus.CANCELLED.value and row.parent_run_id is not None:
                    await self._try_wake_parent_run(session, locked_session, row, now)
                result = run_to_domain(row)
        if result is not None and result.status is RunStatus.CANCELLED and result.depth == 0:
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
                            RunModel.depth == 0,
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
                    profile_id = await self._lock_default_profile_id(session)
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
                    input_file_ids = await self._validate_and_merge_input_file_ids(
                        session,
                        session_id,
                        owner_id,
                        existing=(),
                        incoming=tuple(source.input_file_ids),
                    )
                    inherited_skip = (
                        source.memory_extraction_status == ExtractionStatus.SKIPPED.value
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
                            ExtractionStatus.SKIPPED.value if inherited_skip else None
                        ),
                        memory_extracted_at=now if inherited_skip else None,
                        memory_extraction_safe_error_code=(
                            source.memory_extraction_safe_error_code or "memory_source_invalidated"
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

    async def get(self, session_id: str, owner_id: str, run_id: str) -> Run | None:
        """通过未删除 Session 作用域读取 Run，并附带已关联的问答记录。"""
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
            if row is None:
                return None
            interactions = await self._load_input_interactions_in_session(session, row.id)
        return replace(run_to_domain(row), input_interactions=interactions)

    @staticmethod
    async def _load_input_interactions_in_session(
        session: AsyncSession, run_id: str
    ) -> tuple[RunInputInteraction, ...]:
        """读取该 Run 的提问与回答摘要，供子任务详情页回看。"""
        question_msg = aliased(ConversationMessageModel)
        answer_msg = aliased(ConversationMessageModel)
        rows = (
            await session.execute(
                select(RunInputRequestModel, question_msg, answer_msg)
                .join(
                    question_msg,
                    question_msg.id == RunInputRequestModel.question_message_id,
                )
                .outerjoin(
                    answer_msg,
                    answer_msg.id == RunInputRequestModel.answer_message_id,
                )
                .where(RunInputRequestModel.run_id == run_id)
                .order_by(RunInputRequestModel.created_at, RunInputRequestModel.id)
            )
        ).all()
        items: list[RunInputInteraction] = []
        for req, q_row, a_row in rows:
            q_text = "".join(
                part.text
                for part in conversation_to_domain(q_row).parts
                if isinstance(part, TextPart)
            )
            a_text = (
                None
                if a_row is None
                else "".join(
                    part.text
                    for part in conversation_to_domain(a_row).parts
                    if isinstance(part, TextPart)
                )
            )
            items.append(
                RunInputInteraction(
                    input_request_id=req.id,
                    status=InputRequestStatus(req.status),
                    question_text=q_text,
                    answer_text=a_text,
                    created_at=req.created_at,
                    answered_at=req.answered_at,
                )
            )
        return tuple(items)

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
                RunModel.depth == 0,
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

    async def count_subagent_dispatches(self, root_run_id: str) -> int:
        """统计同一主 Run 累计已派发的子 Run 数量。"""
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(RunModel.id)).where(
                    RunModel.root_run_id == root_run_id,
                    RunModel.depth >= 1,
                )
            )
            return int(count or 0)

    async def begin_subagent_batch(
        self, command: BeginSubagentBatchCommand
    ) -> BeginSubagentBatchResult:
        """原子校验并创建子 Run 批次，同时将主 Run 置为 waiting_for_subagents 并释放租约。"""
        if not 1 <= len(command.items) <= 5:
            raise RunStateError("subagent_batch_size_invalid")
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
                if row.status != RunStatus.RUNNING.value or row.cancel_requested_at is not None:
                    raise RunStateError("run_execution_not_active")
                if row.depth != 0:
                    raise RunStateError("subagent_nested_dispatch_forbidden")
                if await self._has_unsettled_tool_calls(session, row.id):
                    raise RunStateError("run_tool_call_unsettled")

                root_run_id = row.root_run_id or row.id
                existing_count = int(
                    await session.scalar(
                        select(func.count(RunModel.id)).where(
                            RunModel.root_run_id == root_run_id,
                            RunModel.depth >= 1,
                        )
                    )
                    or 0
                )
                if existing_count + len(command.items) > 15:
                    raise RunStateError("subagent_dispatch_limit_exceeded")

                if (
                    command.call_message.session_id != row.session_id
                    or command.call_message.owner_id != row.owner_id
                    or command.call_message.run_id != row.id
                    or command.call_message.role is not ExecutionRole.ASSISTANT
                ):
                    raise RunStateError("run_tool_call_message_invalid")

                # 校验每个子任务对应的 Subagent 配置、模型可用性与输入文件归属。
                validated_items: list[tuple[str, tuple[str, ...]]] = []
                for item in command.items:
                    subagent_row = await session.scalar(
                        select(SubagentModel).where(
                            SubagentModel.id == item.subagent_profile_id,
                            SubagentModel.slug == item.subagent_slug,
                            SubagentModel.deleted_at.is_(None),
                            SubagentModel.enabled.is_(True),
                        )
                    )
                    if subagent_row is None:
                        raise RunStateError("subagent_profile_unavailable")
                    if DISPATCH_SUBAGENTS_TOOL_ID in {t.tool_id for t in item.tool_snapshot.tools}:
                        raise RunStateError("subagent_nested_dispatch_forbidden")
                    model_profile_id = subagent_row.model_profile_id or row.model_config_version_id
                    profile_row = await session.scalar(
                        select(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == model_profile_id)
                        .with_for_update(read=True)
                    )
                    if (
                        profile_row is None
                        or not profile_row.enabled
                        or not profile_row.supports_stream
                        or not profile_row.supports_tools
                        or profile_row.api_key_ciphertext is None
                        or profile_row.api_key_nonce is None
                        or profile_row.api_key_key_version is None
                    ):
                        raise RunStateError("model_profile_unavailable")
                    validated_files = await self._validate_and_merge_input_file_ids(
                        session,
                        row.session_id,
                        row.owner_id,
                        existing=(),
                        incoming=item.input_file_ids,
                    )
                    validated_items.append((model_profile_id, validated_files))

                # 父 Run 行锁已覆盖本查询，派发批次必须续接同一 Run 的全局工具序号。
                max_tool_ordinal = await session.scalar(
                    select(func.max(RunToolCallModel.ordinal)).where(
                        RunToolCallModel.run_id == row.id
                    )
                )
                dispatch_ordinal = int(max_tool_ordinal or 0) + 1
                persisted_call_msg = await self._message_repo.append_execution_in_session(
                    session,
                    command.call_message,
                    locked_session=locked_session,
                    managed_run_id=row.id,
                )
                session.add(
                    RunToolCallModel(
                        id=command.record_id,
                        run_id=row.id,
                        session_id=row.session_id,
                        tool_call_id=command.tool_call_id,
                        ordinal=dispatch_ordinal,
                        tool_id=DISPATCH_SUBAGENTS_TOOL_ID,
                        tool_kind=ToolKind.BUILTIN.value,
                        model_name=DISPATCH_SUBAGENTS_TOOL_ID,
                        execution_epoch=command.execution_epoch,
                        round=command.round,
                        status=ToolCallStatus.EXECUTING.value,
                        call_message_id=persisted_call_msg.id,
                        requires_approval=False,
                        started_at=command.requested_at,
                    )
                )

                child_rows: list[RunModel] = []
                for idx, item in enumerate(command.items):
                    model_profile_id, validated_files = validated_items[idx]
                    child_created_at = await self._next_run_created_at(session, row.session_id)
                    child = RunModel(
                        id=item.sub_run_id,
                        session_id=row.session_id,
                        owner_id=row.owner_id,
                        status=RunStatus.QUEUED.value,
                        trigger_message_id=row.trigger_message_id,
                        context_cutoff_sequence=row.context_cutoff_sequence,
                        retry_of_run_id=None,
                        model_config_version_id=model_profile_id,
                        main_prompt_version_id=row.main_prompt_version_id,
                        compression_prompt_version_id=row.compression_prompt_version_id,
                        # 子 Run 上下文隔离且无正式会话历史，不继承主 Run 压缩策略。
                        compression_policy_version_id=None,
                        skill_snapshot=(
                            None
                            if item.skill_snapshot is None
                            else item.skill_snapshot.model_dump(mode="json")
                        ),
                        input_file_ids=list(validated_files),
                        memory_project_id=None,
                        memory_input_message_ids=[],
                        memory_extraction_status=ExtractionStatus.SKIPPED.value,
                        memory_extracted_at=command.requested_at,
                        tool_snapshot=item.tool_snapshot.model_dump(mode="json"),
                        auth_session_id=row.auth_session_id,
                        llm_request_count=0,
                        prompt_template_version=row.prompt_template_version,
                        event_sequence=0,
                        execution_epoch=0,
                        queued_at=command.requested_at,
                        created_at=child_created_at,
                        updated_at=max(command.requested_at, child_created_at),
                        usage=RunUsage().model_dump(mode="json"),
                        parent_run_id=row.id,
                        root_run_id=root_run_id,
                        parent_tool_call_id=command.tool_call_id,
                        depth=1,
                        subagent_profile_id=item.subagent_profile_id,
                        subagent_slug=item.subagent_slug,
                        subagent_name=item.subagent_name,
                        subagent_system_prompt=item.system_prompt,
                        max_llm_requests=item.max_llm_requests,
                        timeout_seconds=item.timeout_seconds,
                    )
                    session.add(child)
                    await session.flush()
                    await self._message_repo.append_execution_in_session(
                        session,
                        item.task_message,
                        locked_session=locked_session,
                        managed_run_id=child.id,
                    )
                    await self._append_event(
                        session,
                        child,
                        RunQueuedPayload(trigger_message_id=row.trigger_message_id),
                        command.requested_at,
                    )
                    child_rows.append(child)

                # 主 Run 转入 waiting_for_subagents 并清空 worker_id 与租约，退出本轮 Executor。
                row.status = RunStatus.WAITING_FOR_SUBAGENTS.value
                self._clear_lease(row)
                row.updated_at = command.requested_at
                await self._append_event(
                    session,
                    row,
                    RunWaitingForSubagentsPayload(
                        tool_call_id=command.tool_call_id,
                        subagent_run_ids=tuple(item.sub_run_id for item in command.items),
                    ),
                    command.requested_at,
                )
                return BeginSubagentBatchResult(
                    run=run_to_domain(row),
                    sub_runs=tuple(run_to_domain(child) for child in child_rows),
                )
