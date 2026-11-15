"""Run 仓储共享的锁序、租约、事件、幂等请求与用量聚合辅助。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.file.model import SessionFileModel
from adapter.llm.model.profile import (
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.message.converter.message import conversation_to_domain
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.run.converter import (
    event_payload_to_json,
    run_to_domain,
)
from adapter.run.model import (
    RunEventModel,
    RunInputRequestModel,
    RunModel,
    RunRequestModel,
    RunToolCallModel,
)
from adapter.session.model import SessionModel
from adapter.skill.model import SkillExecutionModel
from common.ids import new_id
from domain.file import PendingSessionFile
from domain.memory import ExtractionStatus, MemoryRunWakeupPort
from domain.message import (
    ConversationMessage,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    SessionNotFoundError,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import (
    MAX_RUN_SSE_SNAPSHOT_BYTES,
    InputRequestStatus,
    Run,
    RunAttemptUsage,
    RunEventPayload,
    RunFailedPayload,
    RunRequestOperation,
    RunRequestResult,
    RunStatus,
    RunToolCallFinishedPayload,
    RunUsage,
)
from domain.run.model.errors import (
    RunConflictError,
    RunInputResourceError,
    RunNotFoundError,
    RunStateError,
)
from domain.skill import (
    SkillExecutionArtifact,
    SkillExecutionCompletion,
    SkillExecutionStatus,
)
from domain.tool import (
    ToolCallStatus,
    ToolOutcome,
    ToolOutcomeStatus,
    persisted_status_for_outcome,
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



class RunRepositorySupportMixin:
    """提供跨生命周期、工具批处理与上下文子模块共享的事务内辅助能力。"""

    _session_factory: async_sessionmaker[AsyncSession]
    _messages: SqlAlchemyMessageRepository
    _memory_wakeup: MemoryRunWakeupPort | None
    _memory_lifecycle_enabled: bool

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
