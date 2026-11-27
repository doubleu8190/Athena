"""Run 的模型循环、上下文装配、澄清和原子完成主状态机。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.access import AccessPolicyService
from app.preference import PreferenceService, render_preference_context
from app.run.auto_title import AUTO_TITLE_TIMEOUT_SECONDS
from app.run.auto_title_runner import AutoTitleRunner
from app.run.budget import RunLlmBudget
from app.run.compression import ContextCompressor, ContextPreparation
from app.run.config import RunWorkerConfig
from app.run.context import (
    LlmRequestSpec,
    RunContextError,
    RunInputFileContext,
    RunInputImageAttachment,
    measure_llm_request,
)
from app.run.event_publisher import publish_run_events
from app.run.prompt import render_project_instructions_context
from app.run.safe_stop import SafeStopReporter
from app.run.subagent_dispatcher import SubagentDispatcher
from app.run.tool_batch_executor import ToolBatchExecutor
from app.tool.ports import RunToolRuntime
from app.tool.runtime import REQUEST_USER_INPUT_DEFINITION
from common.date import as_utc, utc_now
from common.ids import new_id
from common.json import utf8_size
from domain.file import VISION_IMAGE_MIME_TYPES
from domain.llm.model import (
    LlmError,
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
from domain.llm.ports import LlmStreamProvider
from domain.llm.profile_ports import ModelProfileRepository
from domain.message import (
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
)
from domain.preference import DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET
from domain.run.model import (
    DISPATCH_SUBAGENTS_TOOL_ID,
    ContextStopReason,
    FailRunCommand,
    FinishRunCommand,
    RecordRunUsageCommand,
    RequestRunInputCommand,
    Run,
    RunLease,
    RunStatus,
    RunUsage,
)
from domain.run.ports import RunEventTransport, RunRepository
from domain.session import SessionScope
from domain.tool import (
    AuthorizedRunContext,
    InitializeRunToolsCommand,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    RunToolBatch,
)

if TYPE_CHECKING:
    from adapter.project.repository import SqlAlchemyProjectRepository
    from adapter.session.repository import SqlAlchemySessionRepository
    from adapter.subagent.repository import SqlAlchemySubagentRepository
    from adapter.system_prompt.repository import SqlAlchemySystemPromptRepository

logger = logging.getLogger(__name__)
MAX_MODEL_ATTEMPTS = 3
CONTEXT_SAFETY_NUMERATOR = 9
CONTEXT_SAFETY_DENOMINATOR = 10
_MAX_TRACKED_ACTIVE_BUDGETS = 1024


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
    input_files: tuple[RunInputFileContext, ...]
    event_cursor: int
    round: int = 0
    publish_disabled: bool = False
    trigger_text: str | None = None
    input_images: tuple[RunInputImageAttachment, ...] = ()
    vision_resolved: bool = False
    vision_resolved_supports_vision: bool | None = None


class RunExecutor:
    """执行一个 Run，直到最终回答、澄清等待或安全失败。"""

    def __init__(
        self,
        repository: RunRepository,
        profiles: ModelProfileRepository,
        provider: LlmStreamProvider,
        *,
        system_prompts: SqlAlchemySystemPromptRepository,
        tool_runtime: RunToolRuntime | None = None,
        publisher: RunEventTransport | None = None,
        config: RunWorkerConfig | None = None,
        preferences: PreferenceService | None = None,
        preference_memory_token_budget: int = DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET,
        projects: SqlAlchemyProjectRepository | None = None,
        sessions: SqlAlchemySessionRepository | None = None,
        subagents: SqlAlchemySubagentRepository | None = None,
        access_policy: AccessPolicyService | None = None,
        auto_title_timeout_seconds: float = AUTO_TITLE_TIMEOUT_SECONDS,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = utc_now,
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
        # 有界保存当前执行代次的在途预算引用，供 RunWorker 在取消或超时收敛时回收已消耗计数。
        self._active_budgets: dict[tuple[str, int], RunLlmBudget] = {}
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
            id_factory=id_factory,
            now=self._now,
        )
        self._auto_title_runner = AutoTitleRunner(
            profiles=profiles,
            provider=provider,
            sessions=sessions,
            timeout_seconds=self._auto_title_timeout_seconds,
        )
        self._subagent_dispatcher = SubagentDispatcher(
            repository=repository,
            subagents=subagents,
            tool_runtime=tool_runtime,
            access_policy=access_policy,
            id_factory=id_factory,
            now=self._now,
        )
        self._tool_batch_executor = ToolBatchExecutor(
            repository=repository,
            tool_runtime=tool_runtime,
            config=self._config,
            id_factory=id_factory,
            now=self._now,
        )

    async def wait_background_tasks(self) -> None:
        """等待受控后台任务（如首轮自动标题生成）全部安全退出，供测试与优雅停机使用。"""
        await self._auto_title_runner.wait_background_tasks()

    def _register_active_budget(self, run: Run, budget: RunLlmBudget) -> None:
        """登记本代次在途预算并按插入序限容，避免直接单测调用 execute 时无限累积。"""
        key = (run.id, run.execution_epoch)
        self._active_budgets.pop(key, None)
        self._active_budgets[key] = budget
        while len(self._active_budgets) > _MAX_TRACKED_ACTIVE_BUDGETS:
            del self._active_budgets[next(iter(self._active_budgets))]

    def pop_llm_request_count(self, run_id: str, execution_epoch: int) -> int | None:
        """弹出指定执行代次当前已消耗的 LLM 请求计数，供 Worker 取消或超时收敛落库。"""
        budget = self._active_budgets.pop((run_id, execution_epoch), None)
        return None if budget is None else budget.used

    async def execute(self, lease: RunLease) -> None:
        """执行一次领取；终态、等待与资源清理由原子仓储命令完成。"""
        run = lease.run
        # 把数据库的墙上时钟截止时间换算成事件循环的单调时钟，统一用于超时判断。
        loop_deadline = asyncio.get_running_loop().time() + max(
            0.0, (lease.execution_deadline_at - self._now()).total_seconds()
        )
        state: _ExecutionState | None = None
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
        except RunContextError as error:
            # 上下文类错误携带稳定错误码，直接作为安全失败原因落库。
            await self._fail(
                run,
                str(error),
                llm_request_count=None if state is None else state.budget.used,
            )
        except LlmError as error:
            # 模型/工具运行时的领域错误同样只暴露固定安全错误码。
            await self._fail(
                run,
                error.code,
                llm_request_count=None if state is None else state.budget.used,
            )
        except TimeoutError:
            # 总截止时间耗尽。
            await self._fail(
                run,
                "execution_timeout",
                llm_request_count=None if state is None else state.budget.used,
            )
        except Exception as error:
            # 未预期异常记录类型后收敛为固定错误码，避免泄露堆栈与内部信息。
            logger.error(
                "run_execution_failed run_id=%s exception_type=%s",
                run.id,
                type(error).__name__,
            )
            await self._fail(
                run,
                "execution_failed",
                llm_request_count=None if state is None else state.budget.used,
            )

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
        # 进程内预算计数器从数据库已用次数续接，随 usage 与状态流转事务原子落库。
        budget = RunLlmBudget(
            used=run.llm_request_count,
            maximum=(
                run.max_llm_requests
                if run.max_llm_requests is not None
                else self._config.llm_request_limit
            ),
        )
        self._register_active_budget(run, budget)
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
                    runtime=runtime,
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
        spec = LlmRequestSpec(
            run=run,
            messages=messages,
            main_prompt=state.main_prompt,
            tools=business_tools,
            skill_context=skill_context,
            input_files=state.input_files,
            input_images=state.input_images,
            preference_context=preference_content,
            project_context=project_content,
        )
        # 子 Run 无正式会话历史，跳过历史压缩准备流程，直接走完整上下文度量。
        policy_id = None if run.depth >= 1 else run.compression_policy_version_id
        policy = (
            None if policy_id is None else await self._profiles.get_compression_policy(policy_id)
        )
        prepared: ContextPreparation
        if policy is not None:
            prepared = await self._compressor.prepare(
                state.lease,
                spec,
                runtime,
                policy,
                compression_prompt=state.compression_prompt,
                timeout_seconds=remaining,
                budget=state.budget,
                allow_compression=state.budget.can_compress,
            )
        else:
            llm_messages, selected_tools, input_tokens, output_tokens = await measure_llm_request(
                spec,
                runtime.version,
                timeout_seconds=remaining,
            )
            prepared = ContextPreparation(
                messages=llm_messages,
                tools=selected_tools,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason_hint=ContextStopReason.COMPRESSION_NOT_CONFIGURED,
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
            await self._stop_for_context_safety(state, messages, reason)
            return None

        # 只有安全裁决通过后才允许 final-only 移除工具；压缩消耗最后倒数槽位也一样。
        selected_tools = () if final_only or state.budget.final_only else prepared.tools
        return replace(prepared, tools=selected_tools)

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

    async def _publish_text(self, state: _ExecutionState, text: str) -> None:
        """尽力直推文本增量；推送失败只记告警，不中断模型调用。"""
        if self._publisher is None or not text:
            return
        try:
            await self._publisher.publish_text(
                state.run.id,
                state.run.execution_epoch,
                text,
                round=state.round,
            )
        except Exception:
            logger.warning("run_progress_publish_failed run_id=%s", state.run.id)

    async def _complete_with_retry(
        self,
        state: _ExecutionState,
        request: LlmRequest,
        *,
        allow_retry: bool,
        runtime: RuntimeModelProfile | None = None,
    ) -> LlmResponse:
        """Reserve every provider attempt and retry only output-free transient errors."""
        run = state.run
        # 同一次执行内的所有尝试共享 operation_id，便于用量审计关联。
        operation_id = new_id("llmop")
        for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
            # 首调直接复用外层已校验的 runtime 与租约；仅在退避重试前复核一次租约和配置状态。
            if attempt > 1:
                if not await self._repository.is_live_execution(run.id, run.execution_epoch):
                    raise LlmError("run_execution_lease_lost")
            attempt_runtime = (
                runtime
                if attempt == 1 and runtime is not None
                else await self._profiles.get_runtime(run.model_config_version_id)
            )
            if attempt_runtime is None:
                raise LlmError("model_profile_unavailable")
            # 先在进程内预占预算槽位；失败不退还，随后随 usage 或状态命令原子落库。
            state.budget.reserve()
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
                        # 收到首片文本或工具调用分片即置位 emitted_output，严守已输出禁重试契约。
                        if isinstance(event, TextDelta):
                            emitted_output = True
                            await self._publish_text(state, event.text)
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
                # 无论成功、失败还是取消，都记录本次尝试的用量、耗时及当前预算计数。
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
                            llm_request_count=state.budget.used,
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

    async def _submit_and_execute_batch(
        self,
        state: _ExecutionState,
        calls: tuple[LlmToolCall, ...],
        explanation: str,
        response: LlmResponse,
    ) -> bool:
        """委托 ToolBatchExecutor 落库工具批次并在无审批屏障时执行。"""
        return await self._tool_batch_executor.submit_and_execute_batch(
            run=state.run,
            authorized=state.authorized,
            snapshot=state.snapshot,
            loop_deadline=state.loop_deadline,
            round_number=state.round,
            llm_request_count=state.budget.used,
            calls=calls,
            explanation=explanation,
            response=response,
            publish_events=lambda: self._publish_pending_events(state),
            execute_persisted_batch=lambda batch: self._execute_persisted_batch(state, batch),
        )

    async def _execute_persisted_batch(
        self,
        state: _ExecutionState,
        batch: RunToolBatch,
    ) -> bool:
        """委托 ToolBatchExecutor 按信号量并发执行已落库且已批准的工具批次。"""
        return await self._tool_batch_executor.execute_persisted_batch(
            run=state.run,
            authorized=state.authorized,
            snapshot=state.snapshot,
            loop_deadline=state.loop_deadline,
            llm_request_count=state.budget.used,
            batch=batch,
            publish_events=lambda: self._publish_pending_events(state),
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
                llm_request_count=state.budget.used,
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
        """委托 SubagentDispatcher 校验参数与实时权限并原子派发子 Run 批次。"""
        await self._subagent_dispatcher.dispatch(
            run=state.run,
            authorized=state.authorized,
            snapshot=state.snapshot,
            round_number=state.round,
            llm_request_count=lambda: state.budget.used,
            call=call,
            explanation=explanation,
            response=response,
            authorize=self._authorize,
            publish_events=lambda: self._publish_pending_events(state),
        )

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
                llm_request_count=state.budget.used,
            )
        )
        if finished is None:
            raise LlmError("run_execution_lease_lost")
        await self._publish_pending_events(state)
        # 仅在主 Run 成功落库 completed 并推送事件后异步触发轻量标题生成，
        # 全程与主 Run 终态隔离，超时或失败绝不回滚主 Run。
        if finished.status is RunStatus.COMPLETED and run.depth == 0:
            self._auto_title_runner.schedule(
                owner_id=run.owner_id,
                session_id=run.session_id,
                run_model_profile_id=run.model_config_version_id,
                trigger_text=state.trigger_text or "",
            )

    async def _stop_for_context_safety(
        self,
        state: _ExecutionState,
        messages: tuple[ConversationMessage | ExecutionMessage, ...],
        reason: ContextStopReason,
    ) -> None:
        """委托 SafeStopReporter 生成持久化系统提示并一次性原子提交 FAILED 终态。"""
        await self._safe_stop.stop_for_context_safety(
            state.run,
            messages,
            reason,
            authorize=self._authorize if self._tool_runtime is not None else None,
            publish_events=lambda: self._publish_pending_events(state),
            llm_request_count=state.budget.used,
        )

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
        state.event_cursor, lookup_failed = await publish_run_events(
            self._repository,
            self._publisher,
            session_id=state.run.session_id,
            owner_id=state.run.owner_id,
            run_id=state.run.id,
            execution_epoch=state.run.execution_epoch,
            after_sequence=state.event_cursor,
            parent_run_id=state.run.parent_run_id,
        )
        if lookup_failed:
            state.publish_disabled = True

    async def _fail(
        self,
        run: Run,
        safe_error_code: str,
        *,
        llm_request_count: int | None = None,
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
                    usage=RunUsage(),
                    llm_request_count=llm_request_count,
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
        return as_utc(self._clock())

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
