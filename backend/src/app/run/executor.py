"""Run 的模型循环、单工具执行、澄清和原子完成。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from app.access import AccessPolicyService
from app.preference import PreferenceService, render_preference_context
from app.run.auto_title import (
    AUTO_TITLE_PROMPT_INPUT_MAX_CHARS,
    AUTO_TITLE_SYSTEM_PROMPT,
    AUTO_TITLE_TIMEOUT_SECONDS,
    fallback_title_from_message,
    sanitize_generated_title,
)
from app.run.budget import RunLlmBudget
from app.run.compression import ContextCompressor, ContextPreparation
from app.run.config import RunWorkerConfig
from app.run.context import (
    RunContextError,
    RunInputFileContext,
    RunInputImageAttachment,
    measure_llm_request,
)
from app.run.events import publish_pending_events
from app.run.prompt import render_project_instructions_context
from app.run.safe_stop import (
    SafeStopReporter,
    is_context_report_text_valid,
    system_context_report,
)
from app.tool.ports import RunToolRuntime
from app.tool.runtime import (
    MCP_DELIVERY_AUTH_TIMEOUT_SECONDS,
    MCP_TOOL_WATCHDOG_GRACE_SECONDS,
    REQUEST_USER_INPUT_DEFINITION,
)
from common.ids import new_id, now_utc
from common.json import utf8_size
from domain.access import ResourceType
from domain.file import VISION_IMAGE_MIME_TYPES, PendingSessionFile
from domain.llm.model import (
    LlmError,
    LlmMessage,
    LlmProviderError,
    LlmRequest,
    LlmResponse,
    LlmRole,
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
from domain.llm.ports import LlmStreamProvider
from domain.llm.profile_ports import ModelProfileRepository
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
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from domain.preference import DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET
from domain.project import ProjectReader
from domain.run.model import (
    DISPATCH_SUBAGENTS_TOOL_ID,
    BeginSubagentBatchCommand,
    BeginSubagentBatchItem,
    ContextStopReason,
    FailRunCommand,
    FinishRunCommand,
    RecordRunUsageCommand,
    RequestRunInputCommand,
    Run,
    RunContextSafetyCommitError,
    RunLease,
    RunStatus,
    RunUsage,
)
from domain.run.model.errors import RunConflictError, RunInputResourceError, RunStateError
from domain.run.ports import RunEventTransport, RunRepository
from domain.session import SessionRepository, SessionScope
from domain.skill import SKILL_EXECUTE_TOOL_ID, RunSkillPromptContext, SkillExecutionStatus
from domain.subagent import SubagentRepository
from domain.system_prompt import SystemPromptRepository
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
    ToolCallPreparation,
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
CONTEXT_SAFETY_NUMERATOR = 9
CONTEXT_SAFETY_DENOMINATOR = 10


class _UseFinalAnswerSlot(Exception):
    """A retryable normal request exhausted its safe retry window."""


@dataclass(slots=True)
class _ExecutionState:
    """One live execution's immutable and per-loop state."""

    lease: RunLease
    run: Run
    loop_deadline: float
    authorized: AuthorizedRunContext | None
    snapshot: RuntimeToolSnapshot
    main_prompt: str
    compression_prompt: str
    budget: RunLlmBudget
    retry_context: tuple[ToolRetryContext, ...]
    input_files: tuple[RunInputFileContext, ...]
    event_cursor: int
    round: int = 0
    publish_disabled: bool = False
    trigger_text: str | None = None
    input_images: tuple[RunInputImageAttachment, ...] = ()
    vision_resolved: bool = False
    vision_resolved_supports_vision: bool | None = None


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
        system_prompts: SystemPromptRepository,
        tool_runtime: RunToolRuntime | None = None,
        publisher: RunEventTransport | None = None,
        config: RunWorkerConfig | None = None,
        preferences: PreferenceService | None = None,
        preference_memory_token_budget: int = DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET,
        projects: ProjectReader | None = None,
        sessions: SessionRepository | None = None,
        subagents: SubagentRepository | None = None,
        access_policy: AccessPolicyService | None = None,
        auto_title_timeout_seconds: float = AUTO_TITLE_TIMEOUT_SECONDS,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """保存持久化命令、模型配置、工具运行目录、项目/会话端口、权限引擎和有界执行参数。"""
        self._repository = repository
        self._profiles = profiles
        self._provider = provider
        self._system_prompts = system_prompts
        self._tool_runtime = tool_runtime
        self._publisher = publisher
        self._config = config or RunWorkerConfig()
        self._projects = projects
        self._sessions = sessions
        self._subagents = subagents
        self._access_policy = access_policy
        self._preferences = preferences
        self._preference_memory_token_budget = preference_memory_token_budget
        self._auto_title_timeout_seconds = max(0.01, float(auto_title_timeout_seconds))
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._id_factory = id_factory
        self._clock = clock
        self._sleep = sleep
        self._compressor = ContextCompressor(
            repository,
            profiles,
            provider,
            self._publisher,
        )
        self._safe_stop = SafeStopReporter(
            repository,
            provider,
            id_factory=id_factory,
            now=self._now,
        )

    async def wait_background_tasks(self) -> None:
        """等待受控后台任务（如首轮自动标题生成）全部安全退出，供测试与优雅停机使用。"""
        while self._background_tasks:
            pending = tuple(self._background_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def execute(self, lease: RunLease) -> None:
        """执行一次领取；终态、等待与资源清理由原子仓储命令完成。"""
        run = lease.run
        # 把数据库的墙上时钟截止时间换算成事件循环的单调时钟，统一用于超时判断。
        loop_deadline = asyncio.get_running_loop().time() + max(
            0.0, (lease.execution_deadline_at - self._now()).total_seconds()
        )
        try:
            # 领取时截止时间已过：不再执行，直接走超时失败路径。
            if loop_deadline <= asyncio.get_running_loop().time():
                raise TimeoutError("run_execution_deadline")
            # 模型循环与全部工具执行共享同一个总截止时间。
            async with asyncio.timeout_at(loop_deadline):
                state = await self._prepare(lease, loop_deadline)
                await self._run_loop(state)
        except asyncio.CancelledError:
            # 取消必须原样传播，由 Worker 监督循环取消子任务并清理上游资源。
            raise
        except RunContextSafetyCommitError:
            # 安全收尾已有稳定报告命令；绝不能再降级为无报告普通失败。
            raise
        except RunContextError as error:
            # 上下文类错误携带稳定错误码，直接作为安全失败原因落库。
            await self._fail(run, str(error))
        except LlmError as error:
            # 模型/工具运行时的领域错误同样只暴露固定安全错误码。
            await self._fail(run, error.code)
        except TimeoutError:
            # 总截止时间耗尽。
            await self._fail(run, "execution_timeout")
        except Exception as error:
            # 未预期异常记录类型后收敛为固定错误码，避免泄露堆栈与内部信息。
            logger.error(
                "run_execution_failed run_id=%s exception_type=%s",
                run.id,
                type(error).__name__,
            )
            await self._fail(run, "execution_failed")

    async def _prepare(self, lease: RunLease, loop_deadline: float) -> _ExecutionState:
        """Re-authorize, freeze the catalog, and create the Run request budget."""
        run = lease.run
        # 仅执行当前支持的模板版本；历史 v1 Run 安全失败，不再兼容。
        if run.prompt_template_version != "run-v2":
            raise LlmError("run_prompt_template_unsupported")
        # 内容版本绑定在执行准备阶段解析一次；后续所有轮次都复用同一对不可变正文。
        prompts = await self._system_prompts.get_pair_by_ids(
            run.main_prompt_version_id,
            run.compression_prompt_version_id,
        )
        if prompts is None:
            raise LlmError("system_prompt_unavailable")
        main_prompt, compression_prompt = (item.content for item in prompts)
        if run.depth >= 1 and run.subagent_system_prompt is not None:
            # 子 Run 使用派发时冻结的 Subagent 专属系统提示词，不复用主 Run 提示词。
            main_prompt = run.subagent_system_prompt
        authorized: AuthorizedRunContext | None = None
        if self._tool_runtime is not None:
            # 启用业务工具时必须绑定有效登录会话，否则无法做 owner/项目级鉴权。
            if not run.auth_session_id:
                raise LlmError("run_auth_session_required")
            authorized = await self._authorize(run)
            if authorized is None:
                raise LlmError("run_auth_session_invalid")
        # 工具目录在首次执行时冻结并持久化；同一 Run 的后续轮次、
        # 澄清回答或审批恢复后重新执行时都复用同一快照。
        snapshot = run.tool_snapshot
        if snapshot is None:
            snapshot = await self._freeze_snapshot(run, authorized)
        # 工具执行上下文固定 owner/session/project/epoch 与总截止时间，
        # 供工具运行时在每次外部调用前后做权限与超时约束。
        context = ToolExecutionContext(
            owner_id=run.owner_id,
            chat_session_id=run.session_id,
            project_id=None if authorized is None else authorized.project_id,
            run_id=run.id,
            execution_epoch=run.execution_epoch,
            auth_session_id=run.auth_session_id or "",
            deadline_at=lease.execution_deadline_at,
            skill_snapshot=run.skill_snapshot,
            knowledge_base_ids=run.knowledge_base_ids,
        )
        # 预算从数据库已用次数继续，跨进程重试也不会超发模型请求。
        budget = RunLlmBudget(
            self._repository,
            context,
            used=run.llm_request_count,
            maximum=(
                run.max_llm_requests
                if run.max_llm_requests is not None
                else self._config.llm_request_limit
            ),
            worker_id=run.worker_id or "",
        )
        # 用户主动重试时带上的安全摘要，只作为历史资料注入，不是当前执行结果。
        retry_context = await self._repository.list_retry_context(
            run.session_id, run.owner_id, run.id
        )
        input_files: tuple[RunInputFileContext, ...] = ()
        if run.input_files.file_ids:
            # 解析必须发生在授权成功之后；解析结果只保留安全元数据。
            if self._tool_runtime is None:
                raise LlmError("run_input_file_context_unavailable")
            input_files = await self._tool_runtime.build_run_input_file_context(run)
        # event_cursor 记录已推送事件位置，执行期间只增量发布新事件。
        return _ExecutionState(
            lease=lease,
            run=run,
            loop_deadline=loop_deadline,
            authorized=authorized,
            snapshot=snapshot,
            main_prompt=main_prompt,
            compression_prompt=compression_prompt,
            budget=budget,
            retry_context=retry_context or (),
            input_files=input_files,
            event_cursor=run.event_sequence,
        )

    async def _freeze_snapshot(
        self, run: Run, authorized: AuthorizedRunContext | None
    ) -> RuntimeToolSnapshot:
        """Build and atomically persist the first catalog without a partial write."""
        try:
            # 没有工具运行时的 Run 只暴露澄清工具。
            if self._tool_runtime is None:
                snapshot = RuntimeToolSnapshot(tools=(REQUEST_USER_INPUT_DEFINITION,))
            else:
                if authorized is None:
                    raise LlmError("run_auth_session_invalid")
                # 由运行时构建 v2 工具目录；非 run-v2 已在 _prepare 阶段拒绝，不再兼容 v1。
                if run.depth >= 1:
                    snapshot = await self._tool_runtime.build_snapshot(
                        run.prompt_template_version,
                        authorized,
                        depth=run.depth,
                        allowed_tool_ids=(),
                        knowledge_base_ids=run.knowledge_base_ids,
                    )
                else:
                    snapshot = await self._tool_runtime.build_snapshot(
                        run.prompt_template_version,
                        authorized,
                        knowledge_base_ids=run.knowledge_base_ids,
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
        # 原子写入冻结快照；返回 None 表示租约已丢失或已被其他执行抢先初始化。
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
            # 每轮先确认 Run 仍由当前执行轮次持有，失租立即停止，避免重复副作用。
            if not await self._repository.is_live_execution(run.id, run.execution_epoch):
                raise LlmError("run_execution_lease_lost")
            # 审批恢复或进程重启后的待执行批次也必须先通过同一上下文安全闸门。
            resumed = await self._repository.get_resumable_tool_batch(run.id)
            # 每轮重新读取 Run 绑定的模型配置版本，配置可能已被禁用或修改。
            runtime = await self._profiles.get_runtime(run.model_config_version_id)
            if runtime is None:
                raise LlmError("model_profile_unavailable")
            # 预算只剩最后一个槽位时进入 final-only：本轮必须产出最终回答。
            requested_final_only = state.budget.final_only
            prepared = await self._prepare_request(
                state,
                runtime,
                final_only=requested_final_only,
            )
            if prepared is None:
                return
            if resumed is not None:
                state.round = max(state.round, resumed.round)
                if not await self._execute_persisted_batch(state, resumed):
                    return
                continue
            final_only = state.budget.final_only
            messages = prepared.messages
            tools = prepared.tools
            max_output_tokens = prepared.output_tokens
            state.round += 1
            # 组装本轮模型请求；有工具时才允许 auto tool choice。
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
                # 最后一个预算槽位留给无工具的最终回答，回到循环重新构建请求。
                continue
            text, calls = self._response_parts(response)
            if calls:
                # final-only 轮次出现工具调用属于协议违约；并行能力与批次大小也必须匹配配置。
                if final_only:
                    raise LlmError("llm_tool_call_in_final_answer")
                if len(calls) > 1 and not runtime.version.supports_parallel_tool_calls:
                    raise LlmError("llm_parallel_tool_calls_unsupported")
                if not 1 <= len(calls) <= self._config.tool_batch_limit:
                    raise LlmError("llm_tool_batch_too_large")
                # 调用 id 必须唯一，参数体积受配置上限约束，避免异常数据进入持久化。
                call_ids = [call.call_id for call in calls]
                if len(set(call_ids)) != len(call_ids):
                    raise LlmError("llm_tool_call_id_duplicate")
                if any(
                    utf8_size(dict(call.arguments)) > self._config.tool_argument_bytes
                    for call in calls
                ):
                    raise LlmError("llm_tool_arguments_too_large")
                # 澄清工具与子智能体派发工具不可混合调用，均需单独出现并退出本轮执行。
                request_inputs = [call for call in calls if call.name == "request_user_input"]
                dispatch_calls = [call for call in calls if call.name == DISPATCH_SUBAGENTS_TOOL_ID]
                if request_inputs and dispatch_calls:
                    raise LlmError("llm_tool_call_mix_unsupported")
                if request_inputs:
                    if len(calls) != 1:
                        raise LlmError("llm_tool_call_mix_unsupported")
                    await self._save_clarification(state, calls[0], text, response)
                    return
                if dispatch_calls and state.run.depth == 0:
                    if len(calls) != 1:
                        raise LlmError("llm_tool_call_mix_unsupported")
                    await self._dispatch_subagents(state, calls[0], text, response)
                    return
                # 其余工具调用先原子落库（可能进入审批屏障），再执行已批准部分。
                if not await self._submit_and_execute_batch(state, calls, text, response):
                    return
                continue
            # 无工具调用的纯文本即最终回答；空输出视为模型失败。
            if not text.strip():
                raise LlmError("llm_empty_output")
            await self._save_final(state, text, response)
            return

    async def _ensure_vision_context(
        self,
        state: _ExecutionState,
        runtime: RuntimeModelProfile,
    ) -> None:
        """单次 Run 周期内按需解析并缓存一次视觉图片附件，避免多轮工具循环重复下载重编码。"""
        supports_vision = runtime.version.supports_vision
        if state.vision_resolved and state.vision_resolved_supports_vision == supports_vision:
            return
        if not state.input_files or not any(
            item.mime_type in VISION_IMAGE_MIME_TYPES for item in state.input_files
        ):
            state.input_images = ()
            state.vision_resolved = True
            state.vision_resolved_supports_vision = supports_vision
            return
        if self._tool_runtime is None:
            fallback_status = "image_invalid" if supports_vision else "unsupported_by_current_model"
            state.input_files = tuple(
                replace(item, vision_status=fallback_status)
                if item.mime_type in VISION_IMAGE_MIME_TYPES
                else item
                for item in state.input_files
            )
            state.input_images = ()
            state.vision_resolved = True
            state.vision_resolved_supports_vision = supports_vision
            return
        updated_files, input_images = await self._tool_runtime.build_run_input_vision_context(
            state.run,
            state.input_files,
            supports_vision=supports_vision,
        )
        state.input_files = updated_files
        state.input_images = input_images
        state.vision_resolved = True
        state.vision_resolved_supports_vision = supports_vision

    async def _prepare_request(
        self,
        state: _ExecutionState,
        runtime: RuntimeModelProfile,
        *,
        final_only: bool,
    ) -> ContextPreparation | None:
        """先尝试合法压缩，再按原业务 tools 的完整输入裁决窗口安全。"""
        run = state.run
        # 在计算主窗口预算前按需解析一次视觉图片附件（仅存在于本次执行内存，绝不落库）。
        await self._ensure_vision_context(state, runtime)
        # 每轮重新读取 cutoff 内的正式消息与本 Run 的执行消息，确保工具结果生效。
        messages = await self._repository.list_context_messages(
            run.session_id, run.owner_id, run.id
        )
        if messages is None:
            raise LlmError("run_context_unavailable")
        # 缓存本次触发消息正文，供主 Run 完成后异步生成首轮标题（不触发额外查库）。
        if state.trigger_text is None:
            trigger_msg = next(
                (
                    item
                    for item in messages
                    if isinstance(item, ConversationMessage) and item.id == run.trigger_message_id
                ),
                None,
            )
            if trigger_msg is not None:
                state.trigger_text = "".join(
                    part.text if isinstance(part, TextPart) else f"[附件：{part.filename}]"
                    for part in trigger_msg.parts
                    if isinstance(part, (TextPart, FileRefPart))
                )
        loaded_skill_ids = await self._repository.list_successfully_loaded_skill_ids(run.id)
        skill_context = (
            None
            if self._tool_runtime is None
            else await self._tool_runtime.build_skill_context(run, loaded_skill_ids)
        )
        if run.depth >= 1 or self._projects is None:
            # 子 Run 保持严格上下文隔离，不注入项目专属背景指令。
            project_content: str | None = None
        else:
            project_id = None if state.authorized is None else state.authorized.project_id
            if project_id is None and state.authorized is None and self._sessions is not None:
                visible_session = await self._sessions.get_visible(
                    SessionScope(session_id=run.session_id, owner_id=run.owner_id)
                )
                project_id = None if visible_session is None else visible_session.project_id
            if project_id is None:
                project_content = None
            else:
                project = await self._projects.get(run.owner_id, project_id)
                project_content = (
                    None
                    if project is None or not project.instructions
                    else render_project_instructions_context(project.name, project.instructions)
                )
        if run.depth >= 1 or self._preferences is None:
            # 子 Run 保持严格上下文隔离，不注入个人工作偏好或显式备忘记忆。
            preference_content: str | None = None
        else:
            # 主 Run 每轮按当前用户偏好与显式记忆开关渲染有界背景上下文。
            preference_data = await self._preferences.get_context(run.owner_id)
            preference_content = render_preference_context(
                preference_data,
                tokenizer_encoding=runtime.version.tokenizer_encoding,
                memory_token_budget=self._preference_memory_token_budget,
            )
        # 安全裁决永远使用原业务 tools；final-only 只在裁决通过后移除工具。
        business_tools = self._available_tools(state)
        # 剩余执行时间就是本轮上下文构建与压缩的硬上限。
        remaining = self._remaining_seconds(state)
        if remaining <= 0:
            raise TimeoutError("run_execution_deadline")
        # 子 Run 无正式会话历史，跳过历史压缩准备流程，直接走完整上下文度量。
        policy_id = None if run.depth >= 1 else run.compression_policy_version_id
        prepared: ContextPreparation
        if policy_id is not None:
            policy = await self._profiles.get_compression_policy(policy_id)
            if policy is None:
                # 旧浮点策略或已不存在的引用不会被静默换算；本轮按未配置压缩继续安全判断。
                prepared = await self._measure_complete_context(
                    state,
                    runtime,
                    messages,
                    business_tools,
                    skill_context,
                    preference_content,
                    project_content,
                    remaining,
                    ContextStopReason.COMPRESSION_NOT_CONFIGURED,
                )
            else:
                prepared = await self._compressor.prepare(
                    state.lease,
                    messages,
                    runtime,
                    policy,
                    main_prompt=state.main_prompt,
                    compression_prompt=state.compression_prompt,
                    timeout_seconds=remaining,
                    tools=business_tools,
                    retry_context=state.retry_context,
                    skill_context=skill_context,
                    input_files=state.input_files,
                    input_images=state.input_images,
                    preference_context=preference_content,
                    project_context=project_content,
                    budget=state.budget,
                    allow_compression=state.budget.can_compress,
                )
        else:
            stop_hint = (
                ContextStopReason.COMPRESSION_NOT_CONFIGURED
                if policy_id is None
                else ContextStopReason.COMPRESSION_BUDGET_EXHAUSTED
            )
            prepared = await self._measure_complete_context(
                state,
                runtime,
                messages,
                business_tools,
                skill_context,
                preference_content,
                project_content,
                remaining,
                stop_hint,
            )

        # 90% 输入线和 I+O<=W 硬窗均按业务 tools 的完整计数判断，不能先删 tools 降低占用。
        input_limit = (
            runtime.version.context_window_tokens * CONTEXT_SAFETY_NUMERATOR
            + CONTEXT_SAFETY_DENOMINATOR
            - 1
        ) // CONTEXT_SAFETY_DENOMINATOR
        hard_window_exceeded = (
            prepared.input_tokens + prepared.output_tokens > runtime.version.context_window_tokens
        )
        safety_input_exceeded = prepared.input_tokens >= input_limit
        if hard_window_exceeded or safety_input_exceeded:
            if hard_window_exceeded:
                reason = ContextStopReason.HARD_WINDOW_EXCEEDED
            elif prepared.compression_completed:
                reason = ContextStopReason.COMPRESSION_INSUFFICIENT
            else:
                reason = prepared.stop_reason_hint or ContextStopReason.COMPRESSION_INSUFFICIENT
            compression_reason = (
                prepared.stop_reason_hint
                if prepared.stop_reason_hint is not None and prepared.stop_reason_hint is not reason
                else None
            )
            await self._stop_for_context_safety(
                state,
                runtime,
                messages,
                reason,
                skill_context=skill_context,
                compression_stop_reason=compression_reason,
            )
            return None

        # 只有安全裁决通过后才允许 final-only 移除工具；压缩消耗最后倒数槽位也一样。
        selected_tools = () if final_only or state.budget.final_only else prepared.tools
        return ContextPreparation(
            messages=prepared.messages,
            tools=selected_tools,
            input_tokens=prepared.input_tokens,
            output_tokens=prepared.output_tokens,
            event=prepared.event,
            had_new_history=prepared.had_new_history,
            compression_called=prepared.compression_called,
            compression_completed=prepared.compression_completed,
            stop_reason_hint=prepared.stop_reason_hint,
        )

    async def _measure_complete_context(
        self,
        state: _ExecutionState,
        runtime: RuntimeModelProfile,
        messages: tuple[ConversationMessage | ExecutionMessage, ...],
        tools: tuple[LlmTool, ...],
        skill_context: RunSkillPromptContext | None,
        preference_context: str | None,
        project_context: str | None,
        timeout_seconds: float,
        stop_hint: ContextStopReason,
    ) -> ContextPreparation:
        """测量不经过压缩的全部输入，并保留后续安全停止原因。"""
        llm_messages, selected_tools, input_tokens, output_tokens = await measure_llm_request(
            state.run,
            messages,
            runtime.version,
            main_prompt=state.main_prompt,
            timeout_seconds=timeout_seconds,
            tools=tools,
            retry_context=state.retry_context,
            skill_context=skill_context,
            input_files=state.input_files,
            input_images=state.input_images,
            preference_context=preference_context,
            project_context=project_context,
        )
        return ContextPreparation(
            messages=llm_messages,
            tools=selected_tools,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason_hint=stop_hint,
        )

    def _available_tools(self, state: _ExecutionState) -> tuple[LlmTool, ...]:
        """Expose only definitions in the frozen snapshot for this prompt version."""
        snapshot = state.snapshot
        definitions: tuple[RuntimeToolDefinition, ...]
        # 无工具运行时只暴露澄清工具；启用工具运行时暴露冻结快照中的全部工具。
        if self._tool_runtime is None:
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
        # 同一次执行内的所有尝试共享 operation_id，便于用量审计关联。
        operation_id = new_id("llmop")
        for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
            # 每次尝试前重新确认租约与模型配置，执行期间配置可能被禁用。
            if not await self._repository.is_live_execution(run.id, run.execution_epoch):
                raise LlmError("run_execution_lease_lost")
            attempt_runtime = await self._profiles.get_runtime(run.model_config_version_id)
            if attempt_runtime is None:
                raise LlmError("model_profile_unavailable")
            # 先原子预占预算；失败不退还，防止异常重试无限消耗供应商额度。
            await state.budget.reserve()
            response: LlmResponse | None = None
            # 是否已向客户端产生可见输出；一旦有输出就不能安全重试。
            emitted_output = False
            provider_error: LlmProviderError | None = None
            fatal_error: Exception | None = None
            cancelled = False
            usage = None
            started = time.monotonic()
            try:
                async with self._provider.stream(attempt_runtime, request) as events:
                    async for event in events:
                        # 文本增量尽力实时推送；推送失败只影响实时性，不中断模型调用。
                        if isinstance(event, TextDelta):
                            emitted_output = True
                            if self._publisher is not None:
                                try:
                                    await self._publisher.publish_text(
                                        run.id,
                                        run.execution_epoch,
                                        event.text,
                                        round=state.round,
                                    )
                                except Exception:
                                    logger.warning("run_progress_publish_failed run_id=%s", run.id)
                        # 工具调用分片同样视为已产生输出，禁止后续重试。
                        elif isinstance(
                            event, (ToolCallStarted, ToolArgumentsDelta, ToolCallCompleted)
                        ):
                            emitted_output = True
                        # 只有完整的 ResponseCompleted 才代表本次调用成功。
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
                # 无论成功、失败还是取消，都记录本次尝试的用量与耗时。
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
                    # 本次失败消耗了倒数第二个预算槽位：
                    # 回到主循环，让最后一个槽位用于强制的无工具最终回答。
                    raise _UseFinalAnswerSlot() from provider_error
                # 只有“未产生输出 + 允许重试 + 可恢复错误 + 预算仍够”才重试。
                if (
                    emitted_output
                    or not allow_retry
                    or not self._is_retryable(provider_error)
                    or attempt == MAX_MODEL_ATTEMPTS
                    or state.budget.remaining <= 0
                ):
                    raise provider_error
                # 退避取服务端 Retry-After 与指数退避的较大值；超过剩余时间直接超时。
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
        # 仅接受完整结束的响应；截断或未完成统一按 llm_incomplete 处理。
        if response.status != "completed" or response.incomplete_reason is not None:
            raise LlmError("llm_incomplete")
        # 拆分文本与工具调用：两者可同时存在，由主循环决定后续分支。
        text = "".join(part.text for part in response.output if isinstance(part, LlmText))
        calls = tuple(part for part in response.output if isinstance(part, LlmToolCall))
        return text, calls

    def _definition_for_call(
        self,
        state: _ExecutionState,
        model_name: str,
    ) -> RuntimeToolDefinition | None:
        # 只允许冻结快照中存在的工具，其余一律视为未知工具。
        return state.snapshot.by_model_name(model_name)

    def _validate_business_call(
        self,
        definition: RuntimeToolDefinition | None,
        call: LlmToolCall,
    ) -> ToolOutcome | None:
        """Validate locally before deciding whether a call needs approval."""
        # 未启用工具运行时：模型仍可能调用工具，直接返回可展示的失败结果。
        if self._tool_runtime is None:
            return ToolOutcome(
                status=ToolOutcomeStatus.ERROR,
                code="tool_unavailable",
                message="当前 Run 未启用业务工具执行。",
            )
        # 运行时校验器负责参数级本地校验，不发起任何网络调用。
        return self._tool_runtime.validate_call(definition, call.name, dict(call.arguments))

    async def _prepare_business_call(
        self,
        state: _ExecutionState,
        definition: RuntimeToolDefinition | None,
        call: LlmToolCall,
        *,
        record_id: str,
    ) -> ToolCallPreparation:
        """先做本地和 Run 固定 Skill 预校验，再决定是否进入审批。"""
        if self._tool_runtime is not None:
            return await self._tool_runtime.prepare_call(
                definition,
                call.name,
                dict(call.arguments),
                state.run,
                state.authorized,
                call_id=record_id,
            )
        outcome = self._validate_business_call(definition, call)
        return ToolCallPreparation(
            outcome=outcome,
            requires_approval=bool(
                outcome is None and definition is not None and definition.requires_approval
            ),
        )

    def _tool_result_draft(
        self,
        state: _ExecutionState,
        *,
        tool_call_id: str,
        outcome: ToolOutcome,
        created_at: datetime,
        thread_id: str | None = None,
        artifacts: tuple[ArtifactFileRef, ...] = (),
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
                    artifacts=artifacts,
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
        # 助手消息承载解释文本与全部工具调用，作为一个批次原子持久化。
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
            definition = self._definition_for_call(state, call.name)
            record_id = self._id_factory("toolcall")
            prepared = await self._prepare_business_call(
                state,
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
                round=state.round,
            )
        )
        await self._publish_pending_events(state)
        # 进入审批屏障时结束本次执行，等待用户决策后 Run 会被重新入队。
        if committed.waiting_for_approval:
            return False
        # 无审批屏障时立即执行已批准的调用。
        return await self._execute_persisted_batch(state, committed.batch)

    async def _execute_persisted_batch(
        self,
        state: _ExecutionState,
        batch: RunToolBatch,
    ) -> bool:
        """Execute approved calls with stable admission order and bounded concurrency."""
        # 存在结果未知的调用时必须先收敛整个批次，避免重复执行产生外部副作用。
        if any(call.record.status is ToolCallStatus.UNKNOWN for call in batch.calls):
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
        # 只调度 APPROVED 的调用；待审批或已终态的调用不再执行。
        approved = [call for call in batch.calls if call.record.status is ToolCallStatus.APPROVED]
        if not approved:
            return True
        # stop_dispatch 在失租或出现终止性错误时阻止继续派发新调用。
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
                    # 先原子 admit（APPROVED → EXECUTING）再创建任务，
                    # 保证多 Worker/重试下同一调用不会被重复执行。
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
                        # 失租或状态已变：停止派发，交由上层上报。
                        lease_lost = True
                        stop_dispatch.set()
                        break
                    if stop_dispatch.is_set():
                        # 停止信号在 admit 期间到达：补写“未执行”的取消结果。
                        if not await self._finish_cancelled_admitted_call(
                            state,
                            call,
                            batch.thread_id,
                        ):
                            lease_lost = True
                        break
                    # 启动执行任务并等待其真正开始，确保并发上限与 admit 顺序可控。
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
                # 收集已完成结果；出现失租/终止错误时停止派发后续调用。
                await collect(done)
        except asyncio.CancelledError:
            # 取消或异常时先取消并等待所有活跃工具任务，避免遗留外部调用。
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

        # 失租优先上报，由 Worker 监督循环决定是否收敛状态。
        if lease_lost:
            raise LlmError("run_execution_lease_lost")
        # 终止性错误：收敛批次并结束本轮，不再请求模型。
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
        # 批次正常结束：发布事件后返回 True，主循环继续请求模型。
        await self._publish_pending_events(state)
        return True

    async def _finish_cancelled_admitted_call(
        self,
        state: _ExecutionState,
        call: RunToolBatchCall,
        thread_id: str,
    ) -> bool:
        """Close an admitted call that never reached an external request."""
        # 已 admit 但尚未发起外部请求的调用补写“取消”终态，保证不会悬挂。
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
        # 任务开始前已被叫停：不发起外部调用，直接补写取消结果。
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
        # 定位冻结快照中的定义并执行单个工具；结果先按字节上限裁剪。
        definition = self._definition_for_call(state, call.record.model_name)
        result = await self._execute_tool(
            state,
            definition,
            LlmToolCall(
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
            if not result.artifacts or state.run.depth >= 1
            else ConversationMessageDraft(
                # 调用记录一对一决定消息 ID，完成回包不明时重放仍能核对同一条交付。
                id=f"msg-{call.record.id}",
                session_id=state.run.session_id,
                owner_id=state.run.owner_id,
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
                reply_to_message_id=state.run.trigger_message_id,
                run_id=state.run.id,
                client_message_id=None,
                created_at=finished_at,
                completed_at=finished_at,
                metadata=MessageMetadata(),
                context_turn_id=state.run.trigger_message_id,
            )
        )
        # 原子写入结果消息、事件与记录终态；返回 None 表示租约已丢失。
        try:
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
                        artifacts=artifact_refs,
                    ),
                    outcome=outcome,
                    finished_at=finished_at,
                    artifacts=result.artifacts,
                    delivery_message=delivery_message,
                    skill_execution_completion=result.skill_execution_completion,
                    terminate_error_code=result.terminate_run_error_code,
                    defer_terminal_convergence=True,
                )
            )
        except (ValueError, RunStateError) as error:
            if str(error) != "session_file_quota_exceeded" or not result.artifacts:
                raise
            # 会话文件数量达到配额上限导致事务回滚：先清理本次刚上传至对象存储的临时产物，
            # 再将本次工具调用与脚本审计收敛为配额错误，防止 MinIO 孤儿对象和未收口审计行。
            await self._abandon_artifacts_safely(result.artifacts)
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
                        artifacts=(),
                    ),
                    outcome=outcome,
                    finished_at=finished_at,
                    artifacts=(),
                    delivery_message=None,
                    skill_execution_completion=quota_completion,
                    terminate_error_code=result.terminate_run_error_code,
                    defer_terminal_convergence=True,
                )
            )
        if finished is None:
            stop_dispatch.set()
            return _BatchCallResult(call.record.id, call.record.ordinal, lease_lost=True)
        # 结果未知或工具要求终止 Run 时，通知调度器停止派发后续调用。
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
                    "tool_outcome_unknown" if outcome.status is ToolOutcomeStatus.UNKNOWN else None
                )
            ),
        )

    async def _abandon_artifacts_safely(
        self,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> None:
        """尽力清理落库失败的临时产物对象，清理异常不影响调用状态收敛。"""
        if not artifacts or self._tool_runtime is None:
            return
        abandon = getattr(self._tool_runtime, "abandon_artifacts", None)
        if not callable(abandon):
            return
        try:
            await abandon(artifacts)
        except Exception as error:
            logger.warning(
                "run_tool_artifact_abandon_failed count=%d exception_type=%s",
                len(artifacts),
                type(error).__name__,
            )

    async def _execute_tool(
        self,
        state: _ExecutionState,
        definition: RuntimeToolDefinition | None,
        call: LlmToolCall,
        *,
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
        run_remaining_seconds = self._remaining_seconds(state)
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
            owner_id=state.run.owner_id,
            chat_session_id=state.run.session_id,
            project_id=None if state.authorized is None else state.authorized.project_id,
            run_id=state.run.id,
            execution_epoch=state.run.execution_epoch,
            auth_session_id=state.run.auth_session_id or "",
            deadline_at=self._now() + timedelta(seconds=deadline_seconds),
            tool_call_record_id=record_id,
            skill_snapshot=state.run.skill_snapshot,
            knowledge_base_ids=state.run.knowledge_base_ids,
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
        # 落库前重新鉴权，防止模型调用期间登录会话失效。
        if self._tool_runtime is not None and await self._authorize(run) is None:
            raise LlmError("run_auth_session_invalid")
        # 严格校验参数形状：只允许一个非空 question 字段。
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
        # 原子写入用户可见的问题消息、内部执行消息与输入请求，并进入等待状态。
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

    async def _dispatch_subagents(
        self,
        state: _ExecutionState,
        call: LlmToolCall,
        explanation: str,
        response: LlmResponse,
    ) -> None:
        """校验 dispatch_subagents 参数并原子派发子 Run 批次，随后释放主 Run 租约。"""
        run = state.run
        if run.depth != 0:
            raise LlmError("subagent_nested_dispatch_forbidden")
        if self._tool_runtime is not None and await self._authorize(run) is None:
            raise LlmError("run_auth_session_invalid")
        if self._subagents is None:
            raise LlmError("subagent_runtime_unavailable")
        definition = self._definition_for_call(state, call.name)
        if definition is None or definition.tool_id != DISPATCH_SUBAGENTS_TOOL_ID:
            raise LlmError("tool_unknown")
        if self._tool_runtime is not None:
            validation = self._tool_runtime.validate_call(
                definition, call.name, dict(call.arguments)
            )
            if validation is not None:
                raise LlmError(validation.code)

        raw_tasks = call.arguments.get("tasks")
        if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 5:
            raise LlmError("subagent_batch_size_invalid")

        created_at = self._now()
        batch_items: list[BeginSubagentBatchItem] = []
        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict):
                raise LlmError("tool_arguments_invalid")
            slug = raw_task.get("subagent_slug")
            task_text = raw_task.get("task")
            context_text = raw_task.get("context")
            raw_files = raw_task.get("input_file_ids", [])
            if (
                not isinstance(slug, str)
                or not slug.strip()
                or not isinstance(task_text, str)
                or not task_text.strip()
            ):
                raise LlmError("tool_arguments_invalid")
            if context_text is not None and not isinstance(context_text, str):
                raise LlmError("tool_arguments_invalid")
            if (
                not isinstance(raw_files, list)
                or any(not isinstance(item, str) or not item for item in raw_files)
                or len(raw_files) != len(set(raw_files))
            ):
                raise LlmError("run_input_file_ids_invalid")

            profile = await self._subagents.get_by_slug(slug.strip())
            if profile is None or not profile.enabled:
                raise LlmError("subagent_profile_unavailable")

            # 调度执行前实时复核当前员工是否对目标子智能体拥有读权限，
            # 防止模型强行拼出他部门 subagent slug 或员工调岗后越权触发子 Run。
            if self._access_policy is not None:
                if not await self._access_policy.can_read_resource(
                    run.owner_id,
                    ResourceType.SUBAGENT,
                    profile.id,
                ):
                    raise LlmError("subagent_profile_unavailable")
            elif self._tool_runtime is not None:
                check_subagent = getattr(self._tool_runtime, "can_read_subagent", None)
                if callable(check_subagent) and not await check_subagent(
                    run.owner_id,
                    profile.id,
                ):
                    raise LlmError("subagent_profile_unavailable")

            if self._tool_runtime is not None:
                if state.authorized is None:
                    raise LlmError("run_auth_session_invalid")
                sub_snapshot = await self._tool_runtime.build_snapshot(
                    run.prompt_template_version,
                    state.authorized,
                    depth=1,
                    allowed_tool_ids=profile.allowed_tool_ids,
                    knowledge_base_ids=run.knowledge_base_ids,
                )
            else:
                sub_snapshot = RuntimeToolSnapshot(tools=())

            sub_run_id = self._id_factory("run")
            formatted_task = (
                f"任务：\n{task_text.strip()}\n\n补充上下文：\n{context_text.strip()}"
                if isinstance(context_text, str) and context_text.strip()
                else task_text.strip()
            )
            task_message = ExecutionMessageDraft(
                id=self._id_factory("exec"),
                session_id=run.session_id,
                owner_id=run.owner_id,
                run_id=sub_run_id,
                thread_id=sub_run_id,
                role=ExecutionRole.USER,
                parts=(TextPart(formatted_task),),
                tool_call_id=None,
                source_message_id=None,
                created_at=created_at,
                metadata=MessageMetadata(),
            )
            batch_items.append(
                BeginSubagentBatchItem(
                    sub_run_id=sub_run_id,
                    subagent_profile_id=profile.id,
                    subagent_slug=profile.slug,
                    subagent_name=profile.name,
                    system_prompt=profile.system_prompt,
                    model_profile_id=profile.model_profile_id or run.model_config_version_id,
                    tool_snapshot=sub_snapshot,
                    skill_snapshot=run.skill_snapshot,
                    input_file_ids=tuple(raw_files),
                    task_message=task_message,
                    max_llm_requests=profile.max_llm_requests,
                    timeout_seconds=profile.timeout_seconds,
                )
            )

        call_message = ExecutionMessageDraft(
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
            source_message_id=None,
            created_at=created_at,
            metadata=MessageMetadata(provider="chat_completions", model=response.model),
        )
        try:
            await self._repository.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id=run.id,
                    worker_id=run.worker_id or "",
                    execution_epoch=run.execution_epoch,
                    tool_call_id=call.call_id,
                    call_message=call_message,
                    record_id=self._id_factory("toolcall"),
                    items=tuple(batch_items),
                    requested_at=created_at,
                    round=state.round,
                )
            )
        except (RunStateError, RunInputResourceError, RunConflictError) as error:
            raise LlmError(str(error)) from error
        await self._publish_pending_events(state)

    async def _save_final(
        self,
        state: _ExecutionState,
        text: str,
        response: LlmResponse,
    ) -> None:
        """Re-check delivery permission and atomically save the final answer."""
        run = state.run
        # 最终回答落库前重新鉴权，防止权限在生成期间被撤销。
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
        # 原子完成 Run 并写入回复消息；返回 None 表示失租，不能覆盖他人结果。
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
        # 仅在主 Run 成功落库 completed 并推送事件后异步触发轻量标题生成，
        # 全程与主 Run 终态隔离，超时或失败绝不回滚主 Run。
        if finished.status is RunStatus.COMPLETED and run.depth == 0 and self._sessions is not None:
            self._schedule_auto_title(
                owner_id=run.owner_id,
                session_id=run.session_id,
                run_model_profile_id=run.model_config_version_id,
                trigger_text=state.trigger_text or "",
            )

    def _schedule_auto_title(
        self,
        *,
        owner_id: str,
        session_id: str,
        run_model_profile_id: str,
        trigger_text: str,
    ) -> None:
        """登记受控异步后台任务生成首轮标题，避免未回收 Task 警告。"""
        task = asyncio.create_task(
            self._run_auto_title_task(
                owner_id=owner_id,
                session_id=session_id,
                run_model_profile_id=run_model_profile_id,
                trigger_text=trigger_text,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_auto_title_task(
        self,
        *,
        owner_id: str,
        session_id: str,
        run_model_profile_id: str,
        trigger_text: str,
    ) -> None:
        """仅当未删除会话仍无标题时生成并条件写入自动标题，异常全量兜底隔离。"""
        if self._sessions is None:
            return
        try:
            visible = await self._sessions.get_visible(
                SessionScope(session_id=session_id, owner_id=owner_id)
            )
            if visible is None or visible.title is not None:
                return
            title = await self._generate_auto_title(
                run_model_profile_id=run_model_profile_id,
                trigger_text=trigger_text,
            )
            if not title:
                return
            await self._sessions.set_title_if_empty(owner_id, session_id, title)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "session_auto_title_failed session_id=%s exception_type=%s",
                session_id,
                type(error).__name__,
            )

    async def _generate_auto_title(
        self,
        *,
        run_model_profile_id: str,
        trigger_text: str,
    ) -> str:
        """优先调用压缩模型（否则复用本次 Run 模型）限时生成 <=20 字标题，
        失败或超时则降级截取前 24 字。
        """
        fallback = fallback_title_from_message(trigger_text)
        excerpt = trigger_text.strip()[:AUTO_TITLE_PROMPT_INPUT_MAX_CHARS]
        if not excerpt:
            return fallback

        runtime: RuntimeModelProfile | None = None
        try:
            policy = await self._profiles.get_current_compression_policy()
            if policy is not None:
                runtime = await self._profiles.get_runtime(policy.compression_model_profile_id)
        except Exception:
            runtime = None
        if runtime is None:
            try:
                runtime = await self._profiles.get_runtime(run_model_profile_id)
            except Exception:
                runtime = None
        if runtime is None:
            return fallback

        request = LlmRequest(
            model_profile=runtime.version.id,
            messages=(
                LlmMessage(role=LlmRole.SYSTEM, content=AUTO_TITLE_SYSTEM_PROMPT),
                LlmMessage(role=LlmRole.USER, content=excerpt),
            ),
            tool_choice="none",
            max_output_tokens=min(64, runtime.version.max_output_tokens),
            temperature=(
                0.2 if runtime.version.temperature is None else runtime.version.temperature
            ),
            timeout_seconds=min(
                float(runtime.version.request_timeout_seconds),
                self._auto_title_timeout_seconds,
            ),
        )
        try:
            async with asyncio.timeout(self._auto_title_timeout_seconds):
                response = await self._provider.complete(runtime, request)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "session_auto_title_llm_fallback model_profile_id=%s exception_type=%s",
                runtime.version.id,
                type(error).__name__,
            )
            return fallback

        if response.status != "completed":
            return fallback
        raw_text = "".join(part.text for part in response.output if isinstance(part, LlmText))
        cleaned = sanitize_generated_title(raw_text)
        return cleaned or fallback

    async def _stop_for_context_safety(
        self,
        state: _ExecutionState,
        runtime: RuntimeModelProfile,
        messages: tuple[ConversationMessage | ExecutionMessage, ...],
        reason: ContextStopReason,
        *,
        skill_context: RunSkillPromptContext | None = None,
        compression_stop_reason: ContextStopReason | None = None,
    ) -> None:
        """委托 SafeStopReporter 生成持久化报告并原子提交 FAILED 终态。"""
        await self._safe_stop.stop_for_context_safety(
            state,
            runtime,
            messages,
            reason,
            remaining_seconds=lambda: self._remaining_seconds(state),
            authorize=self._authorize if self._tool_runtime is not None else None,
            publish_events=lambda: self._publish_pending_events(state),
            skill_context=skill_context,
            compression_stop_reason=compression_stop_reason,
        )

    async def _try_model_context_report(
        self,
        state: _ExecutionState,
        runtime: RuntimeModelProfile,
        persisted_state: dict[str, object],
        system_report: str,
    ) -> tuple[str, bool, RunUsage]:
        return await self._safe_stop.try_model_context_report(
            state,
            runtime,
            persisted_state,
            system_report,
            remaining_seconds=lambda: self._remaining_seconds(state),
        )

    @staticmethod
    def _is_context_report_text_valid(text: str, system_report: str) -> bool:
        return is_context_report_text_valid(text, system_report)

    async def _context_report_state(
        self,
        state: _ExecutionState,
        messages: tuple[ConversationMessage | ExecutionMessage, ...],
        *,
        skill_context: RunSkillPromptContext | None = None,
        compression_stop_reason: ContextStopReason | None = None,
    ) -> dict[str, object]:
        return await self._safe_stop.build_context_report_state(
            state,
            messages,
            skill_context=skill_context,
            compression_stop_reason=compression_stop_reason,
        )

    @staticmethod
    def _system_context_report(
        reason: ContextStopReason,
        persisted_state: dict[str, object],
    ) -> str:
        return system_context_report(reason, persisted_state)

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
        # 无实时传输时直接跳过；客户端 SSE 会从 PostgreSQL 对账补齐。
        if state.publish_disabled or self._publisher is None:
            return
        previous_cursor = state.event_cursor
        state.event_cursor, lookup_failed = await publish_pending_events(
            self._repository,
            self._publisher,
            session_id=state.run.session_id,
            owner_id=state.run.owner_id,
            run_id=state.run.id,
            execution_epoch=state.run.execution_epoch,
            after_sequence=state.event_cursor,
        )
        if lookup_failed:
            state.publish_disabled = True
        elif state.run.parent_run_id is not None and state.event_cursor > previous_cursor:
            # 子 Run 事件同步转发至父 Run 频道，触发主 Run SSE 立即对账推送最新子任务状态。
            events = await self._repository.list_events(
                state.run.session_id,
                state.run.owner_id,
                state.run.id,
                after_sequence=previous_cursor,
                limit=100,
                through_sequence=state.event_cursor,
            )
            for event in events:
                try:
                    await self._publisher.publish_persisted(
                        state.run.parent_run_id, state.run.execution_epoch, event
                    )
                except Exception:
                    logger.warning(
                        "subagent_parent_event_forward_failed parent_run_id=%s sub_run_id=%s",
                        state.run.parent_run_id,
                        state.run.id,
                    )

    async def _fail(
        self,
        run: Run,
        safe_error_code: str,
        *,
        report_message: ConversationMessageDraft | None = None,
        usage: RunUsage | None = None,
        context_stop_reason: ContextStopReason | None = None,
    ) -> Run | None:
        """用当前租约记录固定安全错误码。"""
        # 没有 worker 归属就无法安全写入失败态，直接返回。
        if run.worker_id is None:
            return None
        try:
            return await self._repository.fail(
                FailRunCommand(
                    run_id=run.id,
                    worker_id=run.worker_id,
                    execution_epoch=run.execution_epoch,
                    safe_error_code=safe_error_code,
                    failed_at=self._now(),
                    report_message=report_message,
                    usage=RunUsage() if usage is None else usage,
                    context_stop_reason=context_stop_reason,
                )
            )
        except Exception as error:
            # 失败收敛自身出错时只记录日志，不能覆盖原始失败原因。
            logger.error(
                "run_failure_settle_failed run_id=%s exception_type=%s",
                run.id,
                type(error).__name__,
            )
            return None

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


__all__ = ["RunExecutor"]
