"""Run 工具批次预校验、落库、信号量并发准入与产物配额补偿执行器。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from app.run.config import RunWorkerConfig
from app.tool.ports import RunToolRuntime
from app.tool.runtime import MCP_TOOL_WATCHDOG_GRACE_SECONDS
from domain.file import PendingSessionFile
from domain.llm.model import LlmError, LlmResponse, LlmToolCall
from domain.message import (
    ArtifactFileRef,
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    FileRefPart,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from domain.run.model import FailRunCommand, Run
from domain.run.model.errors import RunStateError
from domain.run.ports import RunRepository
from domain.skill import SKILL_EXECUTE_TOOL_ID, SkillExecutionStatus
from domain.tool import (
    AdmitToolCallCommand,
    AuthorizedRunContext,
    BeginToolCallBatchCommand,
    BeginToolCallBatchItem,
    FinishToolCallCommand,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    RunToolBatch,
    RunToolBatchCall,
    ToolCallPreparation,
    ToolCallStatus,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
    bounded_outcome,
    protocol_status_for_outcome,
)

logger = logging.getLogger(__name__)


class ToolBatchExecutor:
    """负责单轮工具批次的校验、持久化、信号量并发调度与产物配额回滚。"""

    def __init__(
        self,
        *,
        repository: RunRepository,
        tool_runtime: RunToolRuntime | None,
        config: RunWorkerConfig,
        id_factory: Callable[[str], str],
        now: Callable[[], datetime],
    ) -> None:
        """保存仓储、工具运行时、有界配置与 ID/时钟工厂。"""
        self._repository = repository
        self._tool_runtime = tool_runtime
        self._config = config
        self._id_factory = id_factory
        self._now = now

    @staticmethod
    def definition_for_call(
        snapshot: RuntimeToolSnapshot,
        model_name: str,
    ) -> RuntimeToolDefinition | None:
        """只允许冻结快照中存在的工具，其余一律视为未知工具。"""
        return snapshot.by_model_name(model_name)

    def validate_business_call(
        self,
        definition: RuntimeToolDefinition | None,
        call: LlmToolCall,
    ) -> ToolOutcome | None:
        """进入审批前执行纯本地参数与定义校验，不发起网络调用。"""
        if self._tool_runtime is None:
            return ToolOutcome(
                status=ToolOutcomeStatus.ERROR,
                code="tool_unavailable",
                message="当前 Run 未启用业务工具执行。",
            )
        return self._tool_runtime.validate_call(definition, call.name, dict(call.arguments))

    async def prepare_business_call(
        self,
        run: Run,
        authorized: AuthorizedRunContext | None,
        definition: RuntimeToolDefinition | None,
        call: LlmToolCall,
        *,
        record_id: str,
    ) -> ToolCallPreparation:
        """先做本地与 Run 固定 Skill 预校验，再决定是否进入审批屏障。"""
        if self._tool_runtime is not None:
            return await self._tool_runtime.prepare_call(
                definition,
                call.name,
                dict(call.arguments),
                run,
                authorized,
                call_id=record_id,
            )
        outcome = self.validate_business_call(definition, call)
        return ToolCallPreparation(
            outcome=outcome,
            requires_approval=bool(
                outcome is None and definition is not None and definition.requires_approval
            ),
        )

    def tool_result_draft(
        self,
        run: Run,
        *,
        tool_call_id: str,
        outcome: ToolOutcome,
        created_at: datetime,
        thread_id: str | None = None,
        artifacts: tuple[ArtifactFileRef, ...] = (),
    ) -> ExecutionMessageDraft:
        """构造与工具调用协议匹配的 TOOL 执行结果消息草稿。"""
        return ExecutionMessageDraft(
            id=self._id_factory("exec"),
            session_id=run.session_id,
            owner_id=run.owner_id,
            run_id=run.id,
            thread_id=thread_id or run.id,
            role=ExecutionRole.TOOL,
            parts=(
                ToolResultPart(
                    tool_call_id=tool_call_id,
                    status=protocol_status_for_outcome(outcome),
                    output=outcome.canonical_output(),
                    artifacts=artifacts,
                ),
            ),
            tool_call_id=tool_call_id,
            source_message_id=None,
            created_at=created_at,
            metadata=MessageMetadata(),
        )

    async def submit_and_execute_batch(
        self,
        *,
        run: Run,
        authorized: AuthorizedRunContext | None,
        snapshot: RuntimeToolSnapshot,
        loop_deadline: float,
        round_number: int,
        llm_request_count: int,
        calls: tuple[LlmToolCall, ...],
        explanation: str,
        response: LlmResponse,
        publish_events: Callable[[], Awaitable[None]],
        execute_persisted_batch: Callable[[RunToolBatch], Awaitable[bool]] | None = None,
    ) -> bool:
        """整批持久化工具调用与即时错误；无审批屏障时立即并发执行已批准调用。"""
        call_message_id = self._id_factory("exec")
        created_at = self._now()
        call_message = ExecutionMessageDraft(
            id=call_message_id,
            session_id=run.session_id,
            owner_id=run.owner_id,
            run_id=run.id,
            thread_id=run.id,
            role=ExecutionRole.ASSISTANT,
            parts=(
                *((TextPart(explanation),) if explanation.strip() else ()),
                *(ToolCallPart(call.call_id, call.name, dict(call.arguments)) for call in calls),
            ),
            tool_call_id=None,
            source_message_id=None,
            created_at=created_at,
            metadata=MessageMetadata(provider="chat_completions", model=response.model),
        )
        items: list[BeginToolCallBatchItem] = []
        # 逐条先做本地校验：校验失败生成即时错误结果，不进入审批或外部执行。
        for call in calls:
            definition = self.definition_for_call(snapshot, call.name)
            record_id = self._id_factory("toolcall")
            prepared = await self.prepare_business_call(
                run,
                authorized,
                definition,
                call,
                record_id=record_id,
            )
            outcome = prepared.outcome
            items.append(
                BeginToolCallBatchItem(
                    record_id=record_id,
                    tool_call_id=call.call_id,
                    model_name=call.name,
                    tool_id=None if definition is None else definition.tool_id,
                    tool_kind=(None if definition is None else ToolKind(definition.source.kind)),
                    # 只有校验通过且定义要求审批的调用才进入审批屏障。
                    requires_approval=prepared.requires_approval,
                    immediate_outcome=outcome,
                    result_message=(
                        None
                        if outcome is None
                        else self.tool_result_draft(
                            run,
                            tool_call_id=call.call_id,
                            outcome=bounded_outcome(
                                outcome,
                                maximum_bytes=self._config.tool_result_bytes,
                            ),
                            created_at=created_at,
                        )
                    ),
                    safe_skill_projection=prepared.safe_skill_projection,
                )
            )
        # 仓储在同一事务内写入消息、调用记录、即时结果与事件，
        # 并根据是否存在待审批调用决定 Run 是否进入等待状态。
        committed = await self._repository.begin_tool_call_batch(
            BeginToolCallBatchCommand(
                run_id=run.id,
                worker_id=run.worker_id or "",
                execution_epoch=run.execution_epoch,
                call_message=call_message,
                calls=tuple(items),
                requested_at=created_at,
                round=round_number,
                llm_request_count=llm_request_count,
            )
        )
        await publish_events()
        # 进入审批屏障时结束本次执行，等待用户决策后 Run 会被重新入队。
        if committed.waiting_for_approval:
            return False
        if execute_persisted_batch is not None:
            return await execute_persisted_batch(committed.batch)
        return await self.execute_persisted_batch(
            run=run,
            authorized=authorized,
            snapshot=snapshot,
            loop_deadline=loop_deadline,
            llm_request_count=llm_request_count,
            batch=committed.batch,
            publish_events=publish_events,
        )

    async def execute_persisted_batch(
        self,
        *,
        run: Run,
        authorized: AuthorizedRunContext | None,
        snapshot: RuntimeToolSnapshot,
        loop_deadline: float,
        llm_request_count: int,
        batch: RunToolBatch,
        publish_events: Callable[[], Awaitable[None]],
    ) -> bool:
        """按 ordinal 顺序排入信号量并发执行已批准调用，并在收尾统一通过 fail 收敛错误批次。"""
        # 存在结果未知的调用时必须先收敛整个批次与 Run，避免重复执行产生外部副作用。
        if any(call.record.status is ToolCallStatus.UNKNOWN for call in batch.calls):
            failed = await self._repository.fail(
                FailRunCommand(
                    run_id=run.id,
                    worker_id=run.worker_id or "",
                    execution_epoch=run.execution_epoch,
                    safe_error_code="tool_outcome_unknown",
                    failed_at=self._now(),
                    llm_request_count=llm_request_count,
                )
            )
            if failed is None:
                raise LlmError("run_execution_lease_lost")
            await publish_events()
            return False
        # 只调度 APPROVED 的调用；待审批或已终态的调用不再执行。
        approved = [call for call in batch.calls if call.record.status is ToolCallStatus.APPROVED]
        if not approved:
            return True

        semaphore = asyncio.Semaphore(self._config.tool_concurrency)
        # 串行化准入临界区，保证高并发下派发严格遵循 ordinal 序且 stop_dispatch 后绝不超额准入。
        admit_lock = asyncio.Lock()
        stop_dispatch = asyncio.Event()
        stop_error_code: str | None = None
        lease_lost = False

        async def run_one(call: RunToolBatchCall) -> None:
            nonlocal stop_error_code, lease_lost
            async with semaphore:
                async with admit_lock:
                    if stop_dispatch.is_set():
                        return
                    admitted = await self._repository.admit_tool_call(
                        AdmitToolCallCommand(
                            run_id=run.id,
                            worker_id=run.worker_id or "",
                            execution_epoch=run.execution_epoch,
                            record_id=call.record.id,
                            admitted_at=self._now(),
                        )
                    )
                    if admitted is None:
                        lease_lost = True
                        stop_dispatch.set()
                        return
                call_stop_error, call_lease_lost = await self.execute_admitted_call(
                    run=run,
                    authorized=authorized,
                    snapshot=snapshot,
                    loop_deadline=loop_deadline,
                    call=call,
                    thread_id=batch.thread_id,
                    stop_dispatch=stop_dispatch,
                )
                if call_lease_lost:
                    lease_lost = True
                    stop_dispatch.set()
                if call_stop_error is not None:
                    stop_error_code = stop_error_code or call_stop_error
                    stop_dispatch.set()

        tasks = [asyncio.create_task(run_one(call)) for call in approved]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # 取消或异常时先取消并等待所有活跃工具任务，避免遗留后台外部调用。
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # 失租优先上报，由 Worker 监督循环决定是否收敛状态。
        if lease_lost:
            raise LlmError("run_execution_lease_lost")
        # 终止性错误：复用 lifecycle.fail() 原子结算未执行调用、写终态事件并唤醒父 Run。
        if stop_error_code is not None:
            failed = await self._repository.fail(
                FailRunCommand(
                    run_id=run.id,
                    worker_id=run.worker_id or "",
                    execution_epoch=run.execution_epoch,
                    safe_error_code=stop_error_code,
                    failed_at=self._now(),
                    llm_request_count=llm_request_count,
                )
            )
            if failed is None:
                raise LlmError("run_execution_lease_lost")
            await publish_events()
            return False
        # 批次正常结束：发布事件后返回 True，主循环继续请求模型。
        await publish_events()
        return True

    async def execute_admitted_call(
        self,
        *,
        run: Run,
        authorized: AuthorizedRunContext | None,
        snapshot: RuntimeToolSnapshot,
        loop_deadline: float,
        call: RunToolBatchCall,
        thread_id: str,
        stop_dispatch: asyncio.Event,
    ) -> tuple[str | None, bool]:
        """执行单个已准入调用并持久化唯一结果；返回 (stop_error_code, lease_lost)。"""
        definition = self.definition_for_call(snapshot, call.record.model_name)
        result = await self.execute_tool(
            run=run,
            authorized=authorized,
            loop_deadline=loop_deadline,
            definition=definition,
            call=LlmToolCall(
                call.record.tool_call_id,
                call.record.model_name,
                call.arguments,
            ),
            record_id=call.record.id,
        )
        outcome = bounded_outcome(
            result.outcome,
            maximum_bytes=self._config.tool_result_bytes,
        )
        artifact_refs = tuple(
            ArtifactFileRef(
                file_id=artifact.id,
                filename=artifact.filename,
                label=artifact.label,
                mime_type=artifact.mime_type,
                size_bytes=artifact.size_bytes,
            )
            for artifact in result.artifacts
        )
        finished_at = self._now()
        delivery_message = (
            None
            if not result.artifacts or run.depth >= 1
            else ConversationMessageDraft(
                id=f"msg-{call.record.id}",
                session_id=run.session_id,
                owner_id=run.owner_id,
                role=ConversationRole.ASSISTANT,
                status=MessageStatus.COMPLETED,
                parts=(
                    TextPart("已生成以下交付文件。"),
                    *(
                        FileRefPart(
                            file_id=artifact.id,
                            filename=artifact.filename,
                            mime_type=artifact.mime_type,
                            size_bytes=artifact.size_bytes,
                        )
                        for artifact in result.artifacts
                    ),
                ),
                reply_to_message_id=run.trigger_message_id,
                run_id=run.id,
                client_message_id=None,
                created_at=finished_at,
                completed_at=finished_at,
                metadata=MessageMetadata(),
                context_turn_id=run.trigger_message_id,
            )
        )
        # 原子写入结果消息、事件与记录终态；返回 None 表示租约已丢失。
        try:
            finished = await self._repository.finish_tool_call(
                FinishToolCallCommand(
                    run_id=run.id,
                    worker_id=run.worker_id or "",
                    execution_epoch=run.execution_epoch,
                    record_id=call.record.id,
                    result_message=self.tool_result_draft(
                        run,
                        tool_call_id=call.record.tool_call_id,
                        outcome=outcome,
                        created_at=finished_at,
                        thread_id=thread_id,
                        artifacts=artifact_refs,
                    ),
                    outcome=outcome,
                    finished_at=finished_at,
                    artifacts=result.artifacts,
                    delivery_message=delivery_message,
                    skill_execution_completion=result.skill_execution_completion,
                )
            )
        except (ValueError, RunStateError) as error:
            if str(error) != "session_file_quota_exceeded" or not result.artifacts:
                raise
            # 会话文件数量达到配额上限导致事务回滚：先清理本次刚上传至对象存储的临时产物，
            # 再将本次工具调用与脚本审计收敛为配额错误，防止 MinIO 孤儿对象和未收口审计行。
            await self.abandon_artifacts_safely(result.artifacts)
            outcome = ToolOutcome(
                status=ToolOutcomeStatus.ERROR,
                code="session_file_quota_exceeded",
                message="当前会话的文件数量已达上限，无法保存本次脚本产物。",
                cleanup_error_code=outcome.cleanup_error_code,
            )
            quota_completion = (
                None
                if result.skill_execution_completion is None
                else result.skill_execution_completion.model_copy(
                    update={
                        "status": SkillExecutionStatus.FAILED,
                        "error_code": "session_file_quota_exceeded",
                    }
                )
            )
            finished = await self._repository.finish_tool_call(
                FinishToolCallCommand(
                    run_id=run.id,
                    worker_id=run.worker_id or "",
                    execution_epoch=run.execution_epoch,
                    record_id=call.record.id,
                    result_message=self.tool_result_draft(
                        run,
                        tool_call_id=call.record.tool_call_id,
                        outcome=outcome,
                        created_at=finished_at,
                        thread_id=thread_id,
                        artifacts=(),
                    ),
                    outcome=outcome,
                    finished_at=finished_at,
                    artifacts=(),
                    delivery_message=None,
                    skill_execution_completion=quota_completion,
                )
            )
        if finished is None:
            stop_dispatch.set()
            return None, True
        # 结果未知或工具要求终止 Run 时，通知调度器停止派发后续调用。
        stop_error = (
            result.terminate_run_error_code
            if result.terminate_run_error_code is not None
            else ("tool_outcome_unknown" if outcome.status is ToolOutcomeStatus.UNKNOWN else None)
        )
        if stop_error is not None:
            stop_dispatch.set()
        return stop_error, False

    async def abandon_artifacts_safely(
        self,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> None:
        """尽力清理落库失败的临时产物对象，清理异常不影响调用状态收敛。"""
        if not artifacts or self._tool_runtime is None:
            return
        try:
            await self._tool_runtime.abandon_artifacts(artifacts)
        except Exception as error:
            logger.warning(
                "run_tool_artifact_abandon_failed count=%d exception_type=%s",
                len(artifacts),
                type(error).__name__,
            )

    async def execute_tool(
        self,
        *,
        run: Run,
        authorized: AuthorizedRunContext | None,
        loop_deadline: float,
        definition: RuntimeToolDefinition | None,
        call: LlmToolCall,
        record_id: str,
    ) -> ToolExecutionResult:
        """Execute exactly one business tool with a bounded deadline and no retry."""
        # 定义缺失或未启用工具运行时：返回安全错误，不发起外部调用。
        if definition is None:
            return ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.ERROR,
                    code="tool_unknown",
                    message="模型请求了冻结目录中不存在的工具。",
                )
            )
        if self._tool_runtime is None:
            return ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.ERROR,
                    code="tool_unavailable",
                    message="当前 Run 未启用业务工具执行。",
                )
            )
        run_remaining_seconds = max(0.0, loop_deadline - asyncio.get_running_loop().time())
        # skill_execute 只受 Run 总截止时间约束；其他工具保留通用 tool_timeout 语义。
        skill_execution = (
            definition.source.kind == "builtin" and definition.tool_id == SKILL_EXECUTE_TOOL_ID
        )
        deadline_seconds = (
            run_remaining_seconds
            if skill_execution
            else min(self._config.tool_timeout_seconds, run_remaining_seconds)
        )
        if deadline_seconds <= 0:
            # 执行前时间已耗尽：外部操作是否发生不可知，按 UNKNOWN 处理。
            return ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.UNKNOWN,
                    code="tool_deadline_exhausted",
                    message="工具执行前 Run 的执行时间已经耗尽，结果无法确认。",
                )
            )
        context = ToolExecutionContext(
            owner_id=run.owner_id,
            chat_session_id=run.session_id,
            project_id=None if authorized is None else authorized.project_id,
            run_id=run.id,
            execution_epoch=run.execution_epoch,
            auth_session_id=run.auth_session_id or "",
            deadline_at=self._now() + timedelta(seconds=deadline_seconds),
            tool_call_record_id=record_id,
            skill_snapshot=run.skill_snapshot,
            knowledge_base_ids=run.knowledge_base_ids,
        )
        # MCP 内部按业务与清理两段超时收敛；外层守护必须完整包住该 envelope 并留调度余量，
        # 否则已确定结果会被抢先误报为 unknown。内置工具不加额外预算。
        execution_timeout_seconds = deadline_seconds
        if definition.source.kind == "mcp":
            execution_timeout_seconds += (
                self._config.tool_cleanup_seconds + MCP_TOOL_WATCHDOG_GRACE_SECONDS
            )
        try:
            async with asyncio.timeout(execution_timeout_seconds):
                return await self._tool_runtime.execute(
                    definition,
                    call.name,
                    dict(call.arguments),
                    context,
                    deadline_seconds=deadline_seconds,
                    run_remaining_seconds=run_remaining_seconds,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # 超时后结果不可确认（外部操作可能已生效），标记 UNKNOWN 交给上层收敛。
            return ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.UNKNOWN,
                    code="tool_timeout",
                    message="工具在截止时间前没有返回确定结果，可能已经生效。",
                )
            )
        except Exception as error:
            # 非预期异常同样按 UNKNOWN 收敛，不向模型暴露内部异常细节。
            logger.warning(
                "run_tool_execution_failed run_id=%s exception_type=%s",
                run.id,
                type(error).__name__,
            )
            return ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.UNKNOWN,
                    code="tool_execution_failed",
                    message="工具执行中断，结果无法确认。",
                )
            )
