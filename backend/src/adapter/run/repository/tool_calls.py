"""Run 工具批处理、审批与产物发布原子事务的 PostgreSQL 子仓储。"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.file.model import SessionFileModel
from adapter.file.repository import (
    count_published_session_files,
    lock_session_file_quota,
    pending_file_to_model,
)
from adapter.message.converter.message import conversation_to_domain, execution_to_domain
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.run.converter import (
    run_to_domain,
    tool_call_to_domain,
)
from adapter.run.model import (
    RunModel,
    RunToolCallModel,
)
from adapter.run.repository._shared import (
    RunRepositorySupportMixin,
    _skill_execution_row_matches,
)
from adapter.session.model import SessionModel
from adapter.skill.model import SkillExecutionModel
from common.ids import new_id
from domain.file import PendingSessionFile
from domain.message import (
    ArtifactFileRef,
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    FileRefPart,
    MessageMetadata,
    SessionNotFoundError,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import (
    MAX_RUN_SSE_SNAPSHOT_BYTES,
    Run,
    RunApprovalResumedPayload,
    RunRequestOperation,
    RunRequestResult,
    RunStatus,
    RunToolApprovalDecidedPayload,
    RunToolCallFinishedPayload,
    RunToolCallStartedPayload,
    RunWaitingForApprovalPayload,
)
from domain.run.model.errors import (
    RunConflictError,
    RunNotFoundError,
    RunStateError,
)
from domain.skill import (
    SKILL_EXECUTE_TOOL_ID,
    SafeSkillApprovalProjection,
    SkillExecutionArtifact,
    SkillExecutionCompletion,
    SkillExecutionStatus,
)
from domain.tool import (
    AdmitToolCallCommand,
    BeginToolCallBatchCommand,
    BeginToolCallBatchResult,
    ConvergeToolBatchCommand,
    DecideToolApprovalCommand,
    FinishToolCallCommand,
    PendingToolApproval,
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


class RunToolCallsMixin(RunRepositorySupportMixin):
    """负责工具批次持久化、准入、审批决策、完成及 Skill 产物发布。"""

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
