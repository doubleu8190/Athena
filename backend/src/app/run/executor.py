"""Run 的模型循环、单工具执行、澄清和原子完成。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from pydantic import ValidationError

from app.run.budget import RunLlmBudget
from app.run.compression import ContextCompressor
from app.run.config import RunWorkerConfig
from app.run.context import RunContextError, build_llm_request
from app.tool.runtime import (
    MCP_DELIVERY_AUTH_TIMEOUT_SECONDS,
    MCP_TOOL_WATCHDOG_GRACE_SECONDS,
    REQUEST_USER_INPUT_DEFINITION,
)
from common.ids import new_id, now_utc
from common.json import utf8_size
from domain.llm.model import (
    LlmError,
    LlmMessage,
    LlmProviderError,
    LlmRequest,
    LlmResponse,
    LlmText,
    LlmTool,
    LlmToolCall,
    ResponseCompleted,
    TextDelta,
    ToolArgumentsDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.llm.ports import LlmRuntimeTextProvider, LlmStreamProvider
from domain.llm.profile_ports import CompressionPolicyRepository, ModelProfileRepository
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from domain.run.model import (
    FailRunCommand,
    FinishRunCommand,
    RecordRunUsageCommand,
    RequestRunInputCommand,
    Run,
    RunEvent,
    RunLease,
    RunUsage,
)
from domain.run.ports import RunRepository
from domain.tool import (
    AdmitToolCallCommand,
    AuthorizedRunContext,
    BeginToolCallBatchCommand,
    BeginToolCallBatchItem,
    ConvergeToolBatchCommand,
    FinishToolCallCommand,
    InitializeRunToolsCommand,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    RunToolBatch,
    RunToolBatchCall,
    RunToolRuntime,
    ToolCallStatus,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRetryContext,
    bounded_outcome,
    protocol_status_for_outcome,
)

logger = logging.getLogger(__name__)
MAX_MODEL_ATTEMPTS = 3


class _UseFinalAnswerSlot(Exception):
    """A retryable normal request exhausted its safe retry window."""


class RunProgressPublisher(Protocol):
    """发布临时文本增量的可选传输端口。"""

    async def publish_text(
        self,
        run_id: str,
        execution_epoch: int,
        text: str,
        *,
        round: int = 0,
    ) -> None:
        """发送临时文本，不负责保存正式消息。"""

    async def publish_persisted(self, run_id: str, execution_epoch: int, event: RunEvent) -> None:
        """通知客户端数据库已提交的状态事件。"""


class NullRunProgressPublisher:
    """无实时传输时的空发布端口。"""

    async def publish_text(
        self,
        run_id: str,
        execution_epoch: int,
        text: str,
        *,
        round: int = 0,
    ) -> None:
        return None

    async def publish_persisted(self, run_id: str, execution_epoch: int, event: RunEvent) -> None:
        return None


@dataclass(slots=True)
class _ExecutionState:
    """One live execution's immutable and per-loop state."""

    lease: RunLease
    run: Run
    loop_deadline: float
    authorized: AuthorizedRunContext | None
    snapshot: RuntimeToolSnapshot
    budget: RunLlmBudget
    retry_context: tuple[ToolRetryContext, ...]
    event_cursor: int
    round: int = 0
    publish_disabled: bool = False


@dataclass(frozen=True, slots=True)
class _BatchCallResult:
    """One settled call and whether it requires stopping new dispatch."""

    record_id: str
    ordinal: int
    stop_error_code: str | None = None
    lease_lost: bool = False


class RunExecutor:
    """执行一个 Run，直到最终回答、澄清等待或安全失败。"""

    def __init__(
        self,
        repository: RunRepository,
        profiles: ModelProfileRepository,
        provider: LlmStreamProvider,
        *,
        tool_runtime: RunToolRuntime | None = None,
        publisher: RunProgressPublisher | None = None,
        config: RunWorkerConfig | None = None,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """保存持久化命令、模型配置、工具运行目录和有界执行参数。"""
        self._repository = repository
        self._profiles = profiles
        self._provider = provider
        self._tool_runtime = tool_runtime
        self._publisher = publisher or NullRunProgressPublisher()
        self._config = config or RunWorkerConfig()
        self._id_factory = id_factory
        self._clock = clock
        self._sleep = sleep
        self._compressor = ContextCompressor(
            repository,
            profiles,
            cast(LlmRuntimeTextProvider, provider),
            self._publisher,
        )

    async def execute(self, lease: RunLease) -> None:
        """执行一次领取；终态、等待与资源清理由原子仓储命令完成。"""
        run = lease.run
        loop_deadline = asyncio.get_running_loop().time() + max(
            0.0, (lease.execution_deadline_at - self._now()).total_seconds()
        )
        try:
            if loop_deadline <= asyncio.get_running_loop().time():
                raise TimeoutError("run_execution_deadline")
            async with asyncio.timeout_at(loop_deadline):
                state = await self._prepare(lease, loop_deadline)
                await self._run_loop(state)
        except asyncio.CancelledError:
            raise
        except RunContextError as error:
            await self._fail(run, str(error))
        except LlmError as error:
            await self._fail(run, error.code)
        except TimeoutError:
            await self._fail(run, "execution_timeout")
        except Exception as error:
            logger.error(
                "run_execution_failed run_id=%s exception_type=%s",
                run.id,
                type(error).__name__,
            )
            await self._fail(run, "execution_failed")

    async def _prepare(self, lease: RunLease, loop_deadline: float) -> _ExecutionState:
        """Re-authorize, freeze the catalog, and create the Run request budget."""
        run = lease.run
        if run.prompt_template_version not in {"run-v1", "run-v2"}:
            raise LlmError("run_prompt_template_unsupported")
        authorized: AuthorizedRunContext | None = None
        if self._tool_runtime is not None:
            if not run.auth_session_id:
                raise LlmError("run_auth_session_required")
            authorized = await self._authorize(run)
            if authorized is None:
                raise LlmError("run_auth_session_invalid")
        snapshot = run.tool_snapshot
        if snapshot is None:
            snapshot = await self._freeze_snapshot(run, authorized)
        context = ToolExecutionContext(
            owner_id=run.owner_id,
            chat_session_id=run.session_id,
            project_id=None if authorized is None else authorized.project_id,
            run_id=run.id,
            execution_epoch=run.execution_epoch,
            auth_session_id=run.auth_session_id or "",
            deadline_at=lease.execution_deadline_at,
        )
        budget = RunLlmBudget(
            self._repository,
            context,
            used=run.llm_request_count,
            maximum=self._config.llm_request_limit,
            worker_id=run.worker_id or "",
        )
        retry_context = await self._repository.list_retry_context(
            run.session_id, run.owner_id, run.id
        )
        return _ExecutionState(
            lease=lease,
            run=run,
            loop_deadline=loop_deadline,
            authorized=authorized,
            snapshot=snapshot,
            budget=budget,
            retry_context=retry_context or (),
            event_cursor=run.event_sequence,
        )

    async def _freeze_snapshot(
        self, run: Run, authorized: AuthorizedRunContext | None
    ) -> RuntimeToolSnapshot:
        """Build and atomically persist the first catalog without a partial write."""
        try:
            if self._tool_runtime is None:
                snapshot = RuntimeToolSnapshot(tools=(REQUEST_USER_INPUT_DEFINITION,))
            else:
                if authorized is None:
                    raise LlmError("run_auth_session_invalid")
                snapshot = await self._tool_runtime.build_snapshot(
                    run.prompt_template_version,
                    authorized,
                )
        except ValidationError as error:
            code = (
                "tool_catalog_too_large"
                if "tool_snapshot_too_large" in str(error)
                else "tool_snapshot_invalid"
            )
            raise LlmError(code) from error
        except ValueError as error:
            if str(error) == "run_prompt_template_unsupported":
                raise LlmError("run_prompt_template_unsupported") from error
            if str(error) == "tool_snapshot_too_large":
                raise LlmError("tool_catalog_too_large") from error
            raise LlmError("tool_snapshot_invalid") from error
        fixed = await self._repository.initialize_tools(
            InitializeRunToolsCommand(
                run_id=run.id,
                worker_id=run.worker_id or "",
                execution_epoch=run.execution_epoch,
                snapshot=snapshot,
            )
        )
        if fixed is None:
            raise LlmError("run_execution_lease_lost")
        return fixed

    async def _run_loop(self, state: _ExecutionState) -> None:
        """Resume settled batches before asking the model for any new calls."""
        run = state.run
        while True:
            if not await self._repository.is_live_execution(run.id, run.execution_epoch):
                raise LlmError("run_execution_lease_lost")
            resumed = await self._repository.get_resumable_tool_batch(run.id)
            if resumed is not None:
                state.round = max(state.round, resumed.round)
                if not await self._execute_persisted_batch(state, resumed):
                    return
                continue
            runtime = await self._profiles.get_runtime(run.model_config_version_id)
            if runtime is None:
                raise LlmError("model_profile_unavailable")
            final_only = state.budget.final_only
            messages, tools, max_output_tokens = await self._prepare_request(
                state,
                runtime,
                final_only=final_only,
            )
            if state.budget.final_only:
                final_only = True
                tools = ()
            state.round += 1
            request = LlmRequest(
                model_profile=runtime.version.id,
                messages=messages,
                tools=tools,
                tool_choice="auto" if tools else "none",
                max_output_tokens=max_output_tokens,
                temperature=runtime.version.temperature,
                timeout_seconds=runtime.version.request_timeout_seconds,
            )
            try:
                response = await self._complete_with_retry(
                    state,
                    request,
                    allow_retry=not final_only,
                )
            except _UseFinalAnswerSlot:
                continue
            text, calls = self._response_parts(response)
            if calls:
                if final_only:
                    raise LlmError("llm_tool_call_in_final_answer")
                if len(calls) > 1 and not runtime.version.supports_parallel_tool_calls:
                    raise LlmError("llm_parallel_tool_calls_unsupported")
                if not 1 <= len(calls) <= self._config.tool_batch_limit:
                    raise LlmError("llm_tool_batch_too_large")
                call_ids = [call.call_id for call in calls]
                if len(set(call_ids)) != len(call_ids):
                    raise LlmError("llm_tool_call_id_duplicate")
                if any(
                    utf8_size(dict(call.arguments)) > self._config.tool_argument_bytes
                    for call in calls
                ):
                    raise LlmError("llm_tool_arguments_too_large")
                request_inputs = [
                    call for call in calls if call.name == "request_user_input"
                ]
                if request_inputs:
                    if len(calls) != 1:
                        raise LlmError("llm_tool_call_mix_unsupported")
                    await self._save_clarification(state, calls[0], text, response)
                    return
                if not await self._submit_and_execute_batch(state, calls, text, response):
                    return
                continue
            if not text.strip():
                raise LlmError("llm_empty_output")
            await self._save_final(state, text, response)
            return

    async def _prepare_request(
        self,
        state: _ExecutionState,
        runtime: RuntimeModelProfile,
        *,
        final_only: bool,
    ) -> tuple[tuple[LlmMessage, ...], tuple[LlmTool, ...], int]:
        """Rebuild context each round and keep the final slot tool-free."""
        run = state.run
        messages = await self._repository.list_context_messages(
            run.session_id, run.owner_id, run.id
        )
        if messages is None:
            raise LlmError("run_context_unavailable")
        tools = () if final_only else self._available_tools(state)
        remaining = self._remaining_seconds(state)
        if remaining <= 0:
            raise TimeoutError("run_execution_deadline")
        policy_id = run.compression_policy_version_id
        if policy_id is None or final_only or not state.budget.can_compress:
            return await build_llm_request(
                run,
                messages,
                runtime.version,
                timeout_seconds=remaining,
                tools=tools,
                retry_context=state.retry_context,
            )
        policy = await cast(CompressionPolicyRepository, self._profiles).get_compression_policy(
            policy_id
        )
        if policy is None:
            raise LlmError("run_compression_policy_unavailable")
        try:
            built, selected, output, _ = await self._compressor.prepare(
                state.lease,
                messages,
                runtime,
                policy,
                timeout_seconds=remaining,
                tools=tools,
                retry_context=state.retry_context,
                budget=state.budget,
            )
        except RunContextError as error:
            if error.args and error.args[0] == "run_execution_lease_lost":
                raise
            built, selected, output = await build_llm_request(
                run,
                messages,
                runtime.version,
                timeout_seconds=remaining,
                tools=tools,
                retry_context=state.retry_context,
            )
        if state.budget.final_only:
            selected = ()
        return built, selected, output

    def _available_tools(self, state: _ExecutionState) -> tuple[LlmTool, ...]:
        """Expose only definitions in the frozen snapshot for this prompt version."""
        snapshot = state.snapshot
        definitions: tuple[RuntimeToolDefinition, ...]
        if self._tool_runtime is None or state.run.prompt_template_version == "run-v1":
            request_input = next(
                (item for item in snapshot.tools if item.model_name == "request_user_input"),
                REQUEST_USER_INPUT_DEFINITION,
            )
            definitions = (request_input,)
        else:
            definitions = snapshot.tools
        return tuple(
            LlmTool(
                name=item.model_name,
                description=item.description,
                input_schema=dict(item.input_schema),
            )
            for item in definitions
        )

    async def _complete_with_retry(
        self,
        state: _ExecutionState,
        request: LlmRequest,
        *,
        allow_retry: bool,
    ) -> LlmResponse:
        """Reserve every provider attempt and retry only output-free transient errors."""
        run = state.run
        operation_id = new_id("llmop")
        for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
            if not await self._repository.is_live_execution(run.id, run.execution_epoch):
                raise LlmError("run_execution_lease_lost")
            attempt_runtime = await self._profiles.get_runtime(run.model_config_version_id)
            if attempt_runtime is None:
                raise LlmError("model_profile_unavailable")
            await state.budget.reserve()
            response: LlmResponse | None = None
            emitted_output = False
            provider_error: LlmProviderError | None = None
            fatal_error: Exception | None = None
            cancelled = False
            usage = None
            started = time.monotonic()
            try:
                async with self._provider.stream(attempt_runtime, request) as events:
                    async for event in events:
                        if isinstance(event, TextDelta):
                            emitted_output = True
                            try:
                                await self._publisher.publish_text(
                                    run.id,
                                    run.execution_epoch,
                                    event.text,
                                    round=state.round,
                                )
                            except Exception:
                                logger.warning("run_progress_publish_failed run_id=%s", run.id)
                        elif isinstance(
                            event, (ToolCallStarted, ToolArgumentsDelta, ToolCallCompleted)
                        ):
                            emitted_output = True
                        elif isinstance(event, ResponseCompleted):
                            response = event.response
                            usage = response.usage
                if response is None:
                    raise LlmError("llm_incomplete")
            except asyncio.CancelledError:
                cancelled = True
                raise
            except LlmProviderError as error:
                provider_error = error
            except Exception as error:
                fatal_error = error
            finally:
                elapsed = max(0, int((time.monotonic() - started) * 1000))
                try:
                    usage_recorded = await self._repository.record_usage(
                        RecordRunUsageCommand(
                            run_id=run.id,
                            worker_id=run.worker_id or "",
                            execution_epoch=run.execution_epoch,
                            attempt=attempt,
                            input_tokens=None if usage is None else usage.input_tokens,
                            output_tokens=None if usage is None else usage.output_tokens,
                            total_tokens=None if usage is None else usage.total_tokens,
                            complete=bool(
                                usage is not None
                                and usage.input_tokens is not None
                                and usage.output_tokens is not None
                                and usage.total_tokens is not None
                            ),
                            recorded_at=self._now(),
                            purpose="answer",
                            operation_id=operation_id,
                            model_profile_id=attempt_runtime.version.id,
                            duration_ms=elapsed,
                        )
                    )
                except Exception as error:
                    if not cancelled:
                        raise
                    logger.error(
                        "run_usage_record_failed run_id=%s exception_type=%s",
                        run.id,
                        type(error).__name__,
                    )
                except asyncio.CancelledError:
                    if not cancelled:
                        raise
                    logger.error("run_usage_record_cancelled run_id=%s", run.id)
                else:
                    if usage_recorded is None and not cancelled:
                        raise LlmError("run_execution_lease_lost")
            if fatal_error is not None:
                raise fatal_error
            if provider_error is not None:
                if (
                    not emitted_output
                    and allow_retry
                    and self._is_retryable(provider_error)
                    and attempt < MAX_MODEL_ATTEMPTS
                    and state.budget.remaining == 1
                ):
                    # The failed normal request consumed the penultimate slot.
                    # Re-enter the loop so the last provider call is the
                    # mandatory tool-free final answer rather than another
                    # tool-enabled retry.
                    raise _UseFinalAnswerSlot() from provider_error
                if (
                    emitted_output
                    or not allow_retry
                    or not self._is_retryable(provider_error)
                    or attempt == MAX_MODEL_ATTEMPTS
                    or state.budget.remaining <= 0
                ):
                    raise provider_error
                delay = max(
                    provider_error.retry_after_seconds or 0.0,
                    0.25 * (2 ** (attempt - 1)),
                )
                if self._remaining_seconds(state) <= delay:
                    raise TimeoutError("run_execution_deadline") from provider_error
                await self._sleep(delay)
                continue
            if response is not None:
                return response
            raise LlmError("llm_provider_error")
        raise LlmError("llm_provider_error")

    @staticmethod
    def _response_parts(response: LlmResponse) -> tuple[str, tuple[LlmToolCall, ...]]:
        if response.status != "completed" or response.incomplete_reason is not None:
            raise LlmError("llm_incomplete")
        text = "".join(part.text for part in response.output if isinstance(part, LlmText))
        calls = tuple(part for part in response.output if isinstance(part, LlmToolCall))
        return text, calls

    def _definition_for_call(
        self,
        state: _ExecutionState,
        model_name: str,
    ) -> RuntimeToolDefinition | None:
        if state.run.prompt_template_version == "run-v1":
            return None
        return state.snapshot.by_model_name(model_name)

    def _validate_business_call(
        self,
        definition: RuntimeToolDefinition | None,
        call: LlmToolCall,
    ) -> ToolOutcome | None:
        """Validate locally before deciding whether a call needs approval."""
        if self._tool_runtime is None:
            return ToolOutcome(
                status=ToolOutcomeStatus.ERROR,
                code="tool_unavailable",
                message="当前 Run 未启用业务工具执行。",
            )
        validator = getattr(self._tool_runtime, "validate_call", None)
        if validator is None:
            if definition is None:
                return ToolOutcome(
                    status=ToolOutcomeStatus.ERROR,
                    code="tool_unknown",
                    message="模型请求了冻结目录中不存在的工具。",
                )
            return None
        return cast(
            ToolOutcome | None,
            validator(definition, call.name, dict(call.arguments)),
        )

    def _tool_result_draft(
        self,
        state: _ExecutionState,
        *,
        tool_call_id: str,
        outcome: ToolOutcome,
        created_at: datetime,
        thread_id: str | None = None,
    ) -> ExecutionMessageDraft:
        return ExecutionMessageDraft(
            id=self._id_factory("exec"),
            session_id=state.run.session_id,
            owner_id=state.run.owner_id,
            run_id=state.run.id,
            thread_id=thread_id or state.run.id,
            role=ExecutionRole.TOOL,
            parts=(
                ToolResultPart(
                    tool_call_id=tool_call_id,
                    status=protocol_status_for_outcome(outcome),
                    output=outcome.canonical_output(),
                ),
            ),
            tool_call_id=tool_call_id,
            source_message_id=None,
            created_at=created_at,
            metadata=MessageMetadata(),
        )

    async def _submit_and_execute_batch(
        self,
        state: _ExecutionState,
        calls: tuple[LlmToolCall, ...],
        explanation: str,
        response: LlmResponse,
    ) -> bool:
        """Persist all calls together, then execute only if no approval barrier exists."""
        run = state.run
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
                *(
                    ToolCallPart(call.call_id, call.name, dict(call.arguments))
                    for call in calls
                ),
            ),
            tool_call_id=None,
            source_message_id=None,
            created_at=created_at,
            metadata=MessageMetadata(provider="chat_completions", model=response.model),
        )
        items: list[BeginToolCallBatchItem] = []
        for call in calls:
            definition = self._definition_for_call(state, call.name)
            outcome = self._validate_business_call(definition, call)
            items.append(
                BeginToolCallBatchItem(
                    record_id=self._id_factory("toolcall"),
                    tool_call_id=call.call_id,
                    model_name=call.name,
                    tool_id=None if definition is None else definition.tool_id,
                    tool_kind=(
                        None if definition is None else ToolKind(definition.source.kind)
                    ),
                    requires_approval=bool(
                        outcome is None
                        and definition is not None
                        and definition.requires_approval
                    ),
                    immediate_outcome=outcome,
                    result_message=(
                        None
                        if outcome is None
                        else self._tool_result_draft(
                            state,
                            tool_call_id=call.call_id,
                            outcome=bounded_outcome(
                                outcome,
                                maximum_bytes=self._config.tool_result_bytes,
                            ),
                            created_at=created_at,
                        )
                    ),
                )
            )
        committed = await self._repository.begin_tool_call_batch(
            BeginToolCallBatchCommand(
                run_id=run.id,
                worker_id=run.worker_id or "",
                execution_epoch=run.execution_epoch,
                call_message=call_message,
                calls=tuple(items),
                requested_at=created_at,
                round=state.round,
            )
        )
        await self._publish_pending_events(state)
        if committed.waiting_for_approval:
            return False
        return await self._execute_persisted_batch(state, committed.batch)

    async def _execute_persisted_batch(
        self,
        state: _ExecutionState,
        batch: RunToolBatch,
    ) -> bool:
        """Execute approved calls with stable admission order and bounded concurrency."""
        if any(
            call.record.status is ToolCallStatus.UNKNOWN for call in batch.calls
        ):
            converged = await self._repository.converge_tool_batch(
                ConvergeToolBatchCommand(
                    run_id=state.run.id,
                    worker_id=state.run.worker_id or "",
                    execution_epoch=state.run.execution_epoch,
                    call_message_id=batch.call_message_id,
                    safe_error_code="tool_outcome_unknown",
                    failed_at=self._now(),
                )
            )
            if converged is None:
                raise LlmError("run_execution_lease_lost")
            await self._publish_pending_events(state)
            return False
        approved = [
            call
            for call in batch.calls
            if call.record.status is ToolCallStatus.APPROVED
        ]
        if not approved:
            return True
        stop_dispatch = asyncio.Event()
        active: dict[asyncio.Task[_BatchCallResult], RunToolBatchCall] = {}
        next_index = 0
        stop_error_code: str | None = None
        lease_lost = False

        async def collect(done: set[asyncio.Task[_BatchCallResult]]) -> None:
            nonlocal stop_error_code, lease_lost
            for task in done:
                result = await task
                active.pop(task)
                if result.lease_lost:
                    lease_lost = True
                    stop_dispatch.set()
                if result.stop_error_code is not None:
                    stop_error_code = stop_error_code or result.stop_error_code
                    stop_dispatch.set()

        try:
            while next_index < len(approved) or active:
                while (
                    next_index < len(approved)
                    and len(active) < self._config.tool_concurrency
                    and not stop_dispatch.is_set()
                ):
                    call = approved[next_index]
                    admitted = await self._repository.admit_tool_call(
                        AdmitToolCallCommand(
                            run_id=state.run.id,
                            worker_id=state.run.worker_id or "",
                            execution_epoch=state.run.execution_epoch,
                            record_id=call.record.id,
                            admitted_at=self._now(),
                        )
                    )
                    if admitted is None:
                        lease_lost = True
                        stop_dispatch.set()
                        break
                    if stop_dispatch.is_set():
                        if not await self._finish_cancelled_admitted_call(
                            state,
                            call,
                            batch.thread_id,
                        ):
                            lease_lost = True
                        break
                    started = asyncio.Event()
                    task = asyncio.create_task(
                        self._execute_admitted_call(
                            state,
                            call,
                            batch.thread_id,
                            stop_dispatch,
                            started,
                        )
                    )
                    active[task] = call
                    next_index += 1
                    await started.wait()
                if not active:
                    break
                done, _ = await asyncio.wait(
                    active,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                await collect(done)
        except asyncio.CancelledError:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            raise
        except Exception:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            raise
        finally:
            if active:
                await asyncio.gather(*active, return_exceptions=True)

        if lease_lost:
            raise LlmError("run_execution_lease_lost")
        if stop_error_code is not None:
            converged = await self._repository.converge_tool_batch(
                ConvergeToolBatchCommand(
                    run_id=state.run.id,
                    worker_id=state.run.worker_id or "",
                    execution_epoch=state.run.execution_epoch,
                    call_message_id=batch.call_message_id,
                    safe_error_code=stop_error_code,
                    failed_at=self._now(),
                )
            )
            if converged is None:
                raise LlmError("run_execution_lease_lost")
            await self._publish_pending_events(state)
            return False
        await self._publish_pending_events(state)
        return True

    async def _finish_cancelled_admitted_call(
        self,
        state: _ExecutionState,
        call: RunToolBatchCall,
        thread_id: str,
    ) -> bool:
        """Close an admitted call that never reached an external request."""
        outcome = ToolOutcome(
            status=ToolOutcomeStatus.CANCELLED,
            code="tool_not_executed",
            message="该工具调用未执行。",
        )
        finished_at = self._now()
        finished = await self._repository.finish_tool_call(
            FinishToolCallCommand(
                run_id=state.run.id,
                worker_id=state.run.worker_id or "",
                execution_epoch=state.run.execution_epoch,
                record_id=call.record.id,
                result_message=self._tool_result_draft(
                    state,
                    tool_call_id=call.record.tool_call_id,
                    outcome=outcome,
                    created_at=finished_at,
                    thread_id=thread_id,
                ),
                outcome=outcome,
                finished_at=finished_at,
                defer_terminal_convergence=True,
            )
        )
        return finished is not None

    async def _execute_admitted_call(
        self,
        state: _ExecutionState,
        call: RunToolBatchCall,
        thread_id: str,
        stop_dispatch: asyncio.Event,
        started: asyncio.Event,
    ) -> _BatchCallResult:
        """Run one already-admitted call and persist exactly one matching result."""
        if stop_dispatch.is_set():
            started.set()
            if not await self._finish_cancelled_admitted_call(
                state,
                call,
                thread_id,
            ):
                return _BatchCallResult(
                    call.record.id,
                    call.record.ordinal,
                    lease_lost=True,
                )
            return _BatchCallResult(call.record.id, call.record.ordinal)
        started.set()
        definition = self._definition_for_call(state, call.record.model_name)
        result = await self._execute_tool(
            state,
            definition,
            LlmToolCall(
                call.record.tool_call_id,
                call.record.model_name,
                call.arguments,
            ),
        )
        outcome = bounded_outcome(
            result.outcome,
            maximum_bytes=self._config.tool_result_bytes,
        )
        finished_at = self._now()
        finished = await self._repository.finish_tool_call(
            FinishToolCallCommand(
                run_id=state.run.id,
                worker_id=state.run.worker_id or "",
                execution_epoch=state.run.execution_epoch,
                record_id=call.record.id,
                result_message=self._tool_result_draft(
                    state,
                    tool_call_id=call.record.tool_call_id,
                    outcome=outcome,
                    created_at=finished_at,
                    thread_id=thread_id,
                ),
                outcome=outcome,
                finished_at=finished_at,
                terminate_error_code=result.terminate_run_error_code,
                defer_terminal_convergence=True,
            )
        )
        if finished is None:
            stop_dispatch.set()
            return _BatchCallResult(call.record.id, call.record.ordinal, lease_lost=True)
        if (
            outcome.status is ToolOutcomeStatus.UNKNOWN
            or result.terminate_run_error_code is not None
        ):
            stop_dispatch.set()
        return _BatchCallResult(
            record_id=call.record.id,
            ordinal=call.record.ordinal,
            stop_error_code=(
                result.terminate_run_error_code
                if result.terminate_run_error_code is not None
                else (
                    "tool_outcome_unknown"
                    if outcome.status is ToolOutcomeStatus.UNKNOWN
                    else None
                )
            ),
        )

    async def _execute_tool(
        self,
        state: _ExecutionState,
        definition: RuntimeToolDefinition | None,
        call: LlmToolCall,
    ) -> ToolExecutionResult:
        """Execute exactly one business tool with a bounded deadline and no retry."""
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
        run_remaining_seconds = self._remaining_seconds(state)
        deadline_seconds = min(self._config.tool_timeout_seconds, run_remaining_seconds)
        if deadline_seconds <= 0:
            return ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.UNKNOWN,
                    code="tool_deadline_exhausted",
                    message="工具执行前 Run 的执行时间已经耗尽，结果无法确认。",
                )
            )
        context = ToolExecutionContext(
            owner_id=state.run.owner_id,
            chat_session_id=state.run.session_id,
            project_id=None if state.authorized is None else state.authorized.project_id,
            run_id=state.run.id,
            execution_epoch=state.run.execution_epoch,
            auth_session_id=state.run.auth_session_id or "",
            deadline_at=self._now() + timedelta(seconds=deadline_seconds),
        )
        # MCP 内部按业务、清理、交付认证三段收敛；外层守护必须完整包住该 envelope
        # 并留调度余量，否则已确定结果会被抢先误报为 unknown。内置工具不加额外预算。
        execution_timeout_seconds = deadline_seconds
        if definition.source.kind == "mcp":
            execution_timeout_seconds += (
                self._config.tool_cleanup_seconds
                + MCP_DELIVERY_AUTH_TIMEOUT_SECONDS
                + MCP_TOOL_WATCHDOG_GRACE_SECONDS
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
            return ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.UNKNOWN,
                    code="tool_timeout",
                    message="工具在截止时间前没有返回确定结果，可能已经生效。",
                )
            )
        except Exception as error:
            logger.warning(
                "run_tool_execution_failed run_id=%s exception_type=%s",
                state.run.id,
                type(error).__name__,
            )
            return ToolExecutionResult(
                ToolOutcome(
                    status=ToolOutcomeStatus.UNKNOWN,
                    code="tool_execution_failed",
                    message="工具执行中断，结果无法确认。",
                )
            )

    async def _save_clarification(
        self,
        state: _ExecutionState,
        call: LlmToolCall,
        explanation: str,
        response: LlmResponse,
    ) -> None:
        """Persist the single request_user_input interaction and enter waiting."""
        run = state.run
        if self._tool_runtime is not None and await self._authorize(run) is None:
            raise LlmError("run_auth_session_invalid")
        if set(call.arguments) != {"question"}:
            raise LlmError("llm_tool_arguments_invalid")
        question = call.arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise LlmError("llm_tool_arguments_invalid")
        created_at = self._now()
        question_message_id = self._id_factory("msg")
        question_message = ConversationMessageDraft(
            id=question_message_id,
            session_id=run.session_id,
            owner_id=run.owner_id,
            role=ConversationRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            parts=(TextPart(question),),
            reply_to_message_id=run.trigger_message_id,
            run_id=run.id,
            client_message_id=None,
            created_at=created_at,
            completed_at=created_at,
            metadata=MessageMetadata(),
            context_turn_id=run.trigger_message_id,
        )
        execution_message = ExecutionMessageDraft(
            id=self._id_factory("exec"),
            session_id=run.session_id,
            owner_id=run.owner_id,
            run_id=run.id,
            thread_id=run.id,
            role=ExecutionRole.ASSISTANT,
            parts=(
                *((TextPart(explanation),) if explanation.strip() else ()),
                ToolCallPart(call.call_id, call.name, dict(call.arguments)),
            ),
            tool_call_id=None,
            source_message_id=question_message_id,
            created_at=created_at,
            metadata=MessageMetadata(provider="chat_completions", model=response.model),
        )
        request = await self._repository.request_input(
            RequestRunInputCommand(
                run_id=run.id,
                worker_id=run.worker_id or "",
                execution_epoch=run.execution_epoch,
                input_request_id=self._id_factory("input"),
                question_message=question_message,
                execution_messages=(execution_message,),
                usage=RunUsage(),
                created_at=created_at,
            )
        )
        if request.run_id != run.id:
            raise LlmError("run_input_request_invalid")
        await self._publish_pending_events(state)

    async def _save_final(
        self,
        state: _ExecutionState,
        text: str,
        response: LlmResponse,
    ) -> None:
        """Re-check delivery permission and atomically save the final answer."""
        run = state.run
        if self._tool_runtime is not None and await self._authorize(run) is None:
            raise LlmError("run_auth_session_invalid")
        finished_at = self._now()
        reply = ConversationMessageDraft(
            id=self._id_factory("msg"),
            session_id=run.session_id,
            owner_id=run.owner_id,
            role=ConversationRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            parts=(TextPart(text),),
            reply_to_message_id=run.trigger_message_id,
            run_id=run.id,
            client_message_id=None,
            created_at=finished_at,
            completed_at=finished_at,
            metadata=MessageMetadata(provider="chat_completions", model=response.model),
            context_turn_id=run.trigger_message_id,
        )
        finished = await self._repository.finish(
            FinishRunCommand(
                run_id=run.id,
                worker_id=run.worker_id or "",
                execution_epoch=run.execution_epoch,
                reply_message=reply,
                usage=RunUsage(),
                finished_at=finished_at,
            )
        )
        if finished is None:
            raise LlmError("run_execution_lease_lost")
        await self._publish_pending_events(state)

    async def _authorize(self, run: Run) -> AuthorizedRunContext | None:
        if self._tool_runtime is None:
            return None
        return await self._tool_runtime.authorize(
            run.owner_id,
            run.session_id,
            run.id,
            run.auth_session_id,
        )

    async def _publish_pending_events(self, state: _ExecutionState) -> None:
        """Best-effort publish committed events; SSE reconciles from PostgreSQL."""
        if state.publish_disabled or isinstance(self._publisher, NullRunProgressPublisher):
            return
        try:
            while True:
                current = await self._repository.get(
                    state.run.session_id, state.run.owner_id, state.run.id
                )
                if current is None or current.event_sequence <= state.event_cursor:
                    return
                events = await self._repository.list_events(
                    state.run.session_id,
                    state.run.owner_id,
                    state.run.id,
                    after_sequence=state.event_cursor,
                    limit=min(100, current.event_sequence - state.event_cursor),
                )
                if not events:
                    return
                for event in events:
                    try:
                        await self._publisher.publish_persisted(
                            state.run.id, state.run.execution_epoch, event
                        )
                    except Exception as error:
                        logger.warning(
                            "run_event_publish_failed run_id=%s exception_type=%s",
                            state.run.id,
                            type(error).__name__,
                        )
                    state.event_cursor = event.sequence
        except Exception as error:
            state.publish_disabled = True
            logger.warning(
                "run_event_lookup_failed run_id=%s exception_type=%s",
                state.run.id,
                type(error).__name__,
            )

    async def _fail(self, run: Run, safe_error_code: str) -> None:
        """用当前租约记录固定安全错误码。"""
        if run.worker_id is None:
            return
        try:
            await self._repository.fail(
                FailRunCommand(
                    run_id=run.id,
                    worker_id=run.worker_id,
                    execution_epoch=run.execution_epoch,
                    safe_error_code=safe_error_code,
                    failed_at=self._now(),
                )
            )
        except Exception as error:
            logger.error(
                "run_failure_settle_failed run_id=%s exception_type=%s",
                run.id,
                type(error).__name__,
            )

    def _remaining_seconds(self, state: _ExecutionState) -> float:
        return max(0.0, state.loop_deadline - asyncio.get_running_loop().time())

    def _now(self) -> datetime:
        """读取并规范化执行时钟。"""
        value = self._clock()
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _is_retryable(error: LlmProviderError) -> bool:
        """只重试可恢复网络、超时、限流及服务端错误。"""
        return error.code in {
            "llm_connection_failed",
            "llm_rate_limited",
            "llm_temporary_upstream_error",
            "llm_timeout",
        }


__all__ = ["NullRunProgressPublisher", "RunExecutor", "RunProgressPublisher"]
