"""在主模型调用前组织单次、连续边界的会话历史压缩。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.run.budget import RunLlmBudget
from app.run.context import (
    RunContextError,
    RunInputFileContext,
    count_plain_text_tokens_async,
    estimate_llm_input_tokens,
    measure_llm_request,
)
from app.run.events import publish_persisted_event
from common.ids import new_id
from domain.llm.model import (
    LlmMessage,
    LlmRequest,
    LlmRole,
    LlmText,
    LlmTool,
)
from domain.llm.model.compression_policy import CompressionPolicyVersion
from domain.llm.model.profile import RuntimeModelProfile
from domain.llm.ports import LlmRuntimeTextProvider
from domain.llm.profile_ports import ModelProfileRepository
from domain.message import (
    ConversationMessage,
    ConversationRole,
    ExecutionMessage,
    FileRefPart,
    TextPart,
)
from domain.run.model import (
    ContextStopReason,
    ContextSummary,
    RecordRunUsageCommand,
    Run,
    RunCompressionFailedPayload,
    RunCompressionStartedPayload,
    RunEvent,
    RunLease,
    SaveContextSummaryCommand,
)
from domain.run.ports import RunEventTransport, RunRepository
from domain.skill import RunSkillPromptContext
from domain.tool import ToolRetryContext


class _CompressionCallError(RunContextError):
    """区分供应商调用前失败与其唯一一次调用后的失败。"""

    def __init__(self, code: str, *, dispatched: bool) -> None:
        super().__init__(code)
        self.dispatched = dispatched


@dataclass(frozen=True, slots=True)
class _CompressionCandidate:
    """标识一次 prepare 中实际可送入压缩模型的旧历史范围。"""

    base_summary_id: str | None
    covered_through_sequence: int


@dataclass(frozen=True, slots=True)
class ContextPreparation:
    """一次主调用前的完整请求、计数和压缩结论。"""

    messages: tuple[LlmMessage, ...]
    tools: tuple[LlmTool, ...]
    input_tokens: int
    output_tokens: int
    event: RunEvent | None = None
    had_new_history: bool = False
    compression_called: bool = False
    compression_completed: bool = False
    stop_reason_hint: ContextStopReason | None = None


def _turn_groups(
    messages: Sequence[ConversationMessage],
) -> tuple[tuple[ConversationMessage, ...], ...]:
    """以用户消息开始划分连续对话轮次，避免拆开相邻的澄清。"""
    groups: list[list[ConversationMessage]] = []
    active_turn_id: str | None = None
    for message in messages:
        turn_id = message.context_turn_id or message.run_id
        starts_new_run = (
            message.role is ConversationRole.USER
            and groups
            and (turn_id is None or turn_id != active_turn_id)
        )
        if not groups or starts_new_run:
            groups.append([])
            active_turn_id = turn_id
        groups[-1].append(message)
    return tuple(tuple(group) for group in groups)


async def _split_recent_suffix(
    run: Run,
    groups: list[tuple[ConversationMessage, ...]],
    profile: RuntimeModelProfile,
    base_tokens: int,
    recent_limit: int,
    timeout_seconds: float,
    main_prompt: str,
    skill_context: RunSkillPromptContext | None = None,
    tools: Sequence[LlmTool] | None = None,
    retry_context: Sequence[ToolRetryContext] = (),
    input_files: Sequence[RunInputFileContext] = (),
    preference_context: str | None = None,
    summary_text: str | None = None,
    *,
    prefix_tokens: int | None = None,
) -> tuple[list[tuple[ConversationMessage, ...]], tuple[ConversationMessage, ...]]:
    """按完整主请求计数保留不超过近期预算的连续轮次后缀。"""
    # 增量累加每组消息的 token 增量，避免每轮重拼接并重算固定前缀与已选后缀。
    resolved_prefix = base_tokens if prefix_tokens is None else prefix_tokens
    remaining_groups = list(groups)
    recent_groups: list[tuple[ConversationMessage, ...]] = []
    accumulated_recent_tokens = resolved_prefix
    while remaining_groups:
        group = remaining_groups[-1]
        group_with_prefix_tokens, _ = await estimate_llm_input_tokens(
            run,
            group,
            profile.version,
            main_prompt=main_prompt,
            timeout_seconds=timeout_seconds,
            summary_text=summary_text,
            tools=tools,
            retry_context=retry_context,
            skill_context=skill_context,
            input_files=input_files,
            preference_context=preference_context,
        )
        candidate_tokens = accumulated_recent_tokens + max(
            0, group_with_prefix_tokens - resolved_prefix
        )
        if max(0, candidate_tokens - base_tokens) > recent_limit:
            break
        accumulated_recent_tokens = candidate_tokens
        recent_groups.append(group)
        remaining_groups.pop()
    return remaining_groups, tuple(
        message for group in reversed(recent_groups) for message in group
    )


def _render_messages(messages: Sequence[ConversationMessage]) -> str:
    """把旧历史转换为带明确角色和顺序边界的 JSON 文本。"""
    return json.dumps(
        [
            {
                "role": item.role.value,
                "sequence": item.sequence,
                "text": "".join(
                    part.text if isinstance(part, TextPart) else f"[附件：{part.filename}]"
                    for part in item.parts
                    if isinstance(part, (TextPart, FileRefPart))
                ),
            }
            for item in messages
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _compression_prompt(
    summary: str | None,
    messages: Sequence[ConversationMessage],
    target_characters: int,
) -> str:
    """构造既有摘要与全部新增历史的单次摘要输入。"""
    return (
        f"目标摘要不超过 {target_characters} 个 Unicode 字符。按 UTF-8 最坏情况每字符 4 字节估算；"
        "字符数只是保守生成指导，最终仍以主模型 tokenizer 的实际 Token 校验为准。\n"
        "既有摘要：\n" + (summary or "（无）") + "\n新增历史：\n" + _render_messages(messages)
    )


def _render_compression_request(
    system_prompt: str,
    summary: str | None,
    messages: Sequence[ConversationMessage],
    target_characters: int,
) -> str:
    """序列化实际发送的 system/user 文本，计数包含角色与提示封装。"""
    return json.dumps(
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": _compression_prompt(summary, messages, target_characters),
            },
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _compression_token_limits(
    context_window: int,
    max_output_tokens: int,
    main_summary_budget: int,
) -> tuple[int, int]:
    """为压缩请求预留协议和输出后返回可发送输入上限。"""
    protocol_reserve = min(128, max(1, context_window // 16))
    usable_window = context_window - protocol_reserve
    summary_output_limit = main_summary_budget
    if usable_window < 2 or max_output_tokens <= 0 or summary_output_limit <= 0:
        raise RunContextError("run_compression_context_window_exceeded")
    output_limit = min(max_output_tokens, max(1, usable_window // 3), summary_output_limit)
    input_limit = context_window - protocol_reserve - output_limit
    if input_limit <= 0:
        raise RunContextError("run_compression_context_window_exceeded")
    return input_limit, output_limit


def _bounded_budget(base: int, ratio: float, cap: int, feasible: int) -> int:
    """统一计算比例与绝对上限共同约束后的有效 Token 预算。"""
    return max(0, min(int(base * ratio), cap, feasible))


class ContextCompressor:
    """按 Run 固定策略生成单次摘要版本，不拆批且不做应用层重试。"""

    def __init__(
        self,
        repository: RunRepository,
        profiles: ModelProfileRepository,
        provider: LlmRuntimeTextProvider,
        publisher: RunEventTransport | None = None,
    ) -> None:
        self._repository = repository
        self._profiles = profiles
        self._provider = provider
        self._publisher = publisher

    async def prepare(
        self,
        lease: RunLease,
        messages: tuple[ConversationMessage | ExecutionMessage, ...],
        main_runtime: RuntimeModelProfile,
        policy: CompressionPolicyVersion,
        *,
        main_prompt: str,
        compression_prompt: str,
        timeout_seconds: float,
        tools: Sequence[LlmTool] | None = None,
        retry_context: Sequence[ToolRetryContext] = (),
        skill_context: RunSkillPromptContext | None = None,
        input_files: Sequence[RunInputFileContext] = (),
        preference_context: str | None = None,
        budget: RunLlmBudget | None = None,
        allow_compression: bool = True,
    ) -> ContextPreparation:
        """压缩一次新增旧历史，并在失败时只使用兼容摘要快照回退。"""
        run = lease.run
        summaries = await self._repository.list_context_summaries(
            run.session_id, run.owner_id, run.id
        )
        if summaries is None:
            raise RunContextError("run_context_unavailable")
        conversations = tuple(item for item in messages if isinstance(item, ConversationMessage))
        trigger = next((item for item in conversations if item.id == run.trigger_message_id), None)
        if trigger is None:
            raise RunContextError("run_context_trigger_missing")

        # 旧摘要即使超过当前 cap 仍是唯一权威历史边界，必须原样复用且不得自动重整。
        eligible_summaries = tuple(
            item for item in summaries if item.covered_through_sequence < trigger.sequence
        )
        summary = max(
            eligible_summaries,
            key=lambda item: (
                item.covered_through_sequence,
                item.created_at,
                item.id,
            ),
            default=None,
        )
        active_messages = self._messages_after_summary(messages, summary)
        summary_text = None if summary is None else summary.text

        base_tokens, output_tokens = await estimate_llm_input_tokens(
            run,
            (),
            main_runtime.version,
            main_prompt=main_prompt,
            timeout_seconds=timeout_seconds,
            tools=tools,
            retry_context=retry_context,
            skill_context=skill_context,
            input_files=input_files,
            preference_context=preference_context,
        )
        feasible_history = max(
            0,
            main_runtime.version.context_window_tokens - output_tokens - base_tokens,
        )
        recent_limit = _bounded_budget(
            policy.trigger_tokens,
            policy.recent_ratio,
            policy.recent_token_cap,
            feasible_history,
        )
        summary_limit = _bounded_budget(
            policy.trigger_tokens,
            policy.summary_ratio,
            policy.summary_token_cap,
            feasible_history,
        )

        trigger_prefix = tuple(
            item
            for item in conversations
            if item.sequence < trigger.sequence
            and (summary is None or item.sequence > summary.covered_through_sequence)
        )
        groups = list(_turn_groups(trigger_prefix))
        if not groups:
            current = await self._measure_context(
                run,
                active_messages,
                main_runtime,
                main_prompt=main_prompt,
                timeout_seconds=timeout_seconds,
                summary_text=summary_text,
                tools=tools,
                retry_context=retry_context,
                skill_context=skill_context,
                input_files=input_files,
                preference_context=preference_context,
            )
            return ContextPreparation(
                messages=current.messages,
                tools=current.tools,
                input_tokens=current.input_tokens,
                output_tokens=current.output_tokens,
                had_new_history=False,
                stop_reason_hint=ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )

        group_message_ids = {message.id for group in groups for message in group}
        protected_messages = tuple(
            item for item in active_messages if item.id not in group_message_ids
        )
        # 缓存固定前缀 token 数，供 _split_recent_suffix 增量计算每组预算，避免重复测量前缀。
        prefix_tokens, _ = await estimate_llm_input_tokens(
            run,
            (),
            main_runtime.version,
            main_prompt=main_prompt,
            timeout_seconds=timeout_seconds,
            summary_text=summary_text,
            tools=tools,
            retry_context=retry_context,
            skill_context=skill_context,
            input_files=input_files,
            preference_context=preference_context,
        )
        protected_tokens = (
            prefix_tokens
            if not protected_messages
            else (
                await estimate_llm_input_tokens(
                    run,
                    protected_messages,
                    main_runtime.version,
                    main_prompt=main_prompt,
                    timeout_seconds=timeout_seconds,
                    summary_text=summary_text,
                    tools=tools,
                    retry_context=retry_context,
                    skill_context=skill_context,
                    input_files=input_files,
                    preference_context=preference_context,
                )
            )[0]
        )
        remaining_groups, _ = await _split_recent_suffix(
            run,
            groups,
            main_runtime,
            protected_tokens,
            recent_limit,
            timeout_seconds,
            main_prompt,
            skill_context,
            tools,
            retry_context,
            input_files,
            preference_context,
            summary_text,
            prefix_tokens=prefix_tokens,
        )
        current = await self._measure_context(
            run,
            active_messages,
            main_runtime,
            main_prompt=main_prompt,
            timeout_seconds=timeout_seconds,
            summary_text=summary_text,
            tools=tools,
            retry_context=retry_context,
            skill_context=skill_context,
            input_files=input_files,
            preference_context=preference_context,
        )
        if not remaining_groups:
            return ContextPreparation(
                messages=current.messages,
                tools=current.tools,
                input_tokens=current.input_tokens,
                output_tokens=current.output_tokens,
                had_new_history=True,
                stop_reason_hint=ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )

        # 常规触发使用绝对阈值；达到硬窗口 90% 时也允许一次提前救援，
        # 最终是否停止仍由执行器按原业务 tools 的完整输入统一裁决。
        safety_threshold = (main_runtime.version.context_window_tokens * 9 + 9) // 10
        if current.input_tokens < policy.trigger_tokens and current.input_tokens < safety_threshold:
            return current
        candidate_messages = tuple(message for group in remaining_groups for message in group)
        candidate = _CompressionCandidate(
            base_summary_id=None if summary is None else summary.id,
            covered_through_sequence=candidate_messages[-1].sequence,
        )
        previous_failure = await self._latest_compression_failure(run)
        if (
            previous_failure is not None
            and previous_failure.candidate_covered_through_sequence is not None
            and candidate.covered_through_sequence
            <= previous_failure.candidate_covered_through_sequence
        ):
            # 同一或收缩后的失败范围不能借下一次 prepare 隐式重试；直接复用已测量的 current，
            # 由 executor 继续执行 90% 与硬窗口裁决。
            return ContextPreparation(
                messages=current.messages,
                tools=current.tools,
                input_tokens=current.input_tokens,
                output_tokens=current.output_tokens,
                had_new_history=True,
                stop_reason_hint=self._compression_failure_hint(previous_failure.safe_error_code),
            )
        if not allow_compression:
            return ContextPreparation(
                messages=current.messages,
                tools=current.tools,
                input_tokens=current.input_tokens,
                output_tokens=current.output_tokens,
                had_new_history=True,
                stop_reason_hint=ContextStopReason.COMPRESSION_BUDGET_EXHAUSTED,
            )

        runtime = await self._profiles.get_runtime(policy.compression_model_profile_id)
        operation_id = new_id("cmpop")
        started = await self._repository.append_compression_event(
            run.id,
            lease.run.worker_id or "",
            run.execution_epoch,
            RunCompressionStartedPayload(operation_id=operation_id),
        )
        if started is None:
            raise RunContextError("run_execution_lease_lost")
        await self._publish(started, run)

        if runtime is None or summary_limit <= 0:
            failed = await self._append_failure(
                lease,
                operation_id,
                0,
                "run_compression_model_unavailable"
                if runtime is None
                else "run_context_budget_unavailable",
                candidate,
            )
            return ContextPreparation(
                messages=current.messages,
                tools=current.tools,
                input_tokens=current.input_tokens,
                output_tokens=current.output_tokens,
                event=failed,
                had_new_history=True,
                stop_reason_hint=(
                    ContextStopReason.COMPRESSION_MODEL_UNAVAILABLE
                    if runtime is None
                    else ContextStopReason.COMPRESSION_INPUT_TOO_LARGE
                ),
            )

        retained = self._retain_after_compression(
            active_messages,
            candidate_messages,
        )
        provider_dispatched = False
        try:
            final_summary, compression_runtime = await self._compress_single(
                run,
                lease,
                runtime,
                main_runtime,
                summary_text,
                candidate_messages,
                summary_limit,
                operation_id,
                timeout_seconds,
                compression_prompt,
                budget,
            )
            # _compress_single 成功返回即表示摘要供应商调用已经发生。
            provider_dispatched = True
            final_context = await self._measure_context(
                run,
                retained,
                main_runtime,
                main_prompt=main_prompt,
                timeout_seconds=timeout_seconds,
                summary_text=final_summary,
                tools=tools,
                retry_context=retry_context,
                skill_context=skill_context,
                input_files=input_files,
                preference_context=preference_context,
            )
            if (
                final_context.input_tokens + final_context.output_tokens
                > main_runtime.version.context_window_tokens
            ):
                raise _CompressionCallError(
                    "run_context_window_exceeded",
                    dispatched=True,
                )
        except RunContextError as error:
            if error.args and error.args[0] == "run_execution_lease_lost":
                raise
            dispatched = provider_dispatched or (
                isinstance(error, _CompressionCallError) and error.dispatched
            )
            failed = await self._append_failure(
                lease,
                operation_id,
                1 if dispatched else 0,
                str(error),
                candidate,
            )
            return ContextPreparation(
                messages=current.messages,
                tools=current.tools,
                input_tokens=current.input_tokens,
                output_tokens=current.output_tokens,
                event=failed,
                had_new_history=True,
                compression_called=dispatched,
                stop_reason_hint=self._compression_failure_hint(str(error)),
            )

        saved = await self._repository.save_context_summary(
            SaveContextSummaryCommand(
                run_id=run.id,
                worker_id=lease.run.worker_id or "",
                execution_epoch=run.execution_epoch,
                operation_id=operation_id,
                summary_id=new_id("csum"),
                covered_through_sequence=candidate.covered_through_sequence,
                parent_summary_id=None if summary is None else summary.id,
                text=final_summary,
                policy_version_id=policy.id,
                compression_model_version_id=compression_runtime.version.id,
                prompt_version=policy.prompt_template_version,
                created_at=datetime.now(UTC),
                batch_count=1,
            )
        )
        if saved is None:
            raise RunContextError("run_execution_lease_lost")
        completed = saved[1]
        await self._publish(completed, run)
        return ContextPreparation(
            messages=final_context.messages,
            tools=final_context.tools,
            input_tokens=final_context.input_tokens,
            output_tokens=final_context.output_tokens,
            event=completed,
            had_new_history=True,
            compression_called=True,
            compression_completed=True,
        )

    async def _compress_single(
        self,
        run: Run,
        lease: RunLease,
        runtime: RuntimeModelProfile,
        main_runtime: RuntimeModelProfile,
        prior_summary: str | None,
        messages: Sequence[ConversationMessage],
        summary_limit: int,
        operation_id: str,
        timeout_seconds: float,
        compression_prompt: str,
        budget: RunLlmBudget | None,
    ) -> tuple[str, RuntimeModelProfile]:
        """把旧摘要与全部候选历史一次装入并最多发起一次供应商调用。"""
        input_limit, output_limit = _compression_token_limits(
            runtime.version.context_window_tokens,
            runtime.version.max_output_tokens,
            summary_limit,
        )
        target_characters = min(summary_limit, output_limit) // 4
        if target_characters <= 0:
            raise _CompressionCallError(
                "run_context_budget_unavailable",
                dispatched=False,
            )
        rendered = _render_compression_request(
            compression_prompt,
            prior_summary,
            messages,
            target_characters,
        )
        input_tokens = await count_plain_text_tokens_async(
            rendered,
            runtime.version,
            timeout_seconds=timeout_seconds,
        )
        if input_tokens > input_limit:
            # 预检失败发生在预算预占之前，不能用伪 usage=0 记录供应商调用。
            raise _CompressionCallError(
                "run_compression_context_window_exceeded",
                dispatched=False,
            )
        if not await self._repository.is_live_execution(run.id, run.execution_epoch):
            raise RunContextError("run_execution_lease_lost")
        if budget is not None:
            if not budget.can_compress:
                raise _CompressionCallError(
                    "run_context_compression_budget_exhausted",
                    dispatched=False,
                )
            await budget.reserve()

        request = LlmRequest(
            model_profile=runtime.version.id,
            messages=(
                LlmMessage(LlmRole.SYSTEM, compression_prompt),
                LlmMessage(
                    LlmRole.USER,
                    _compression_prompt(
                        prior_summary,
                        messages,
                        target_characters,
                    ),
                ),
            ),
            tool_choice="none",
            max_output_tokens=output_limit,
            timeout_seconds=min(timeout_seconds, runtime.version.request_timeout_seconds),
        )
        started = time.monotonic()
        response = None
        failure: Exception | None = None
        provider_dispatched = False
        try:
            # 从这里开始供应商调用可能已经发生，之后任何非终止性校验异常都必须按
            # “已派发一次”记录，不能因后置 tokenizer 错误退化成零调用。
            provider_dispatched = True
            # SDK 客户端已固定 max_retries=0；这里也只调用 complete 一次。
            response = await self._provider.complete(runtime, request)
        except Exception as error:
            failure = error
        try:
            elapsed = max(0, int((time.monotonic() - started) * 1000))
            usage = None if response is None else response.usage
            recorded = await self._repository.record_usage(
                RecordRunUsageCommand(
                    run_id=run.id,
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=run.execution_epoch,
                    attempt=1,
                    input_tokens=None if usage is None else usage.input_tokens,
                    output_tokens=None if usage is None else usage.output_tokens,
                    total_tokens=None if usage is None else usage.total_tokens,
                    complete=bool(
                        usage is not None
                        and usage.input_tokens is not None
                        and usage.output_tokens is not None
                        and usage.total_tokens is not None
                    ),
                    recorded_at=datetime.now(UTC),
                    purpose="compression",
                    operation_id=operation_id,
                    batch_index=0,
                    model_profile_id=runtime.version.id,
                    duration_ms=elapsed,
                )
            )
            if recorded is None:
                raise RunContextError("run_execution_lease_lost")
            if failure is not None or response is None:
                raise _CompressionCallError(
                    "run_context_compression_failed",
                    dispatched=True,
                )
            if response.status != "completed" or response.incomplete_reason is not None:
                raise _CompressionCallError(
                    "run_context_compression_incomplete",
                    dispatched=True,
                )
            if len(response.output) != 1 or not isinstance(response.output[0], LlmText):
                raise _CompressionCallError(
                    "run_context_compression_invalid",
                    dispatched=True,
                )
            text = response.output[0].text.strip()
            if not text:
                raise _CompressionCallError(
                    "run_context_compression_empty",
                    dispatched=True,
                )
            if (
                await count_plain_text_tokens_async(
                    text,
                    runtime.version,
                    timeout_seconds=timeout_seconds,
                )
                > output_limit
            ):
                raise _CompressionCallError(
                    "run_context_compression_output_too_large",
                    dispatched=True,
                )
            if (
                await count_plain_text_tokens_async(
                    text,
                    main_runtime.version,
                    timeout_seconds=timeout_seconds,
                )
                > summary_limit
            ):
                raise _CompressionCallError(
                    "run_context_summary_too_large",
                    dispatched=True,
                )
            return text, runtime
        except asyncio.CancelledError:
            raise
        except RunContextError as error:
            if error.args and error.args[0] == "run_execution_lease_lost":
                raise
            if isinstance(error, _CompressionCallError):
                raise
            raise _CompressionCallError(
                str(error),
                dispatched=provider_dispatched,
            ) from error

    async def _measure_context(
        self,
        run: Run,
        messages: Sequence[ConversationMessage | ExecutionMessage],
        runtime: RuntimeModelProfile,
        *,
        main_prompt: str,
        timeout_seconds: float,
        summary_text: str | None,
        tools: Sequence[LlmTool] | None,
        retry_context: Sequence[ToolRetryContext],
        skill_context: RunSkillPromptContext | None,
        input_files: Sequence[RunInputFileContext],
        preference_context: str | None,
    ) -> ContextPreparation:
        """按完整主请求口径测量当前候选上下文。"""
        llm_messages, selected_tools, input_tokens, output_tokens = await measure_llm_request(
            run,
            messages,
            runtime.version,
            main_prompt=main_prompt,
            timeout_seconds=timeout_seconds,
            summary_text=summary_text,
            tools=tools,
            retry_context=retry_context,
            skill_context=skill_context,
            input_files=input_files,
            preference_context=preference_context,
        )
        return ContextPreparation(
            messages=llm_messages,
            tools=selected_tools,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def _append_failure(
        self,
        lease: RunLease,
        operation_id: str,
        batch_count: int,
        safe_error_code: str,
        candidate: _CompressionCandidate,
    ) -> RunEvent | None:
        """提交固定的 0/1 批次结论并区分预检失败与真实调用。"""
        failed = await self._repository.append_compression_event(
            lease.run.id,
            lease.run.worker_id or "",
            lease.run.execution_epoch,
            RunCompressionFailedPayload(
                operation_id=operation_id,
                safe_error_code=safe_error_code,
                batch_count=batch_count,
                candidate_base_summary_id=candidate.base_summary_id,
                candidate_covered_through_sequence=candidate.covered_through_sequence,
            ),
        )
        if failed is not None:
            await self._publish(failed, lease.run)
        return failed

    async def _latest_compression_failure(
        self,
        run: Run,
    ) -> RunCompressionFailedPayload | None:
        """分页读取本 Run 最近一次压缩失败，供 Worker 恢复后去重。"""
        after_sequence = 0
        latest: RunCompressionFailedPayload | None = None
        while True:
            events = await self._repository.list_events(
                run.session_id,
                run.owner_id,
                run.id,
                after_sequence=after_sequence,
                limit=100,
            )
            if not events:
                break
            for event in events:
                if isinstance(event.payload, RunCompressionFailedPayload):
                    latest = event.payload
            next_sequence = events[-1].sequence
            if next_sequence <= after_sequence:
                break
            after_sequence = next_sequence
            if len(events) < 100:
                break
        return latest

    @staticmethod
    def _compression_failure_hint(safe_error_code: str) -> ContextStopReason:
        """把最近一次真实失败类别映射为安全停止原因。"""
        if safe_error_code in {
            "run_compression_context_window_exceeded",
            "run_context_budget_unavailable",
        }:
            return ContextStopReason.COMPRESSION_INPUT_TOO_LARGE
        if safe_error_code == "run_context_compression_budget_exhausted":
            return ContextStopReason.COMPRESSION_BUDGET_EXHAUSTED
        if safe_error_code == "run_compression_model_unavailable":
            return ContextStopReason.COMPRESSION_MODEL_UNAVAILABLE
        return ContextStopReason.COMPRESSION_FAILED

    async def _publish(self, event: RunEvent, run: Run) -> None:
        await publish_persisted_event(self._publisher, run.id, run.execution_epoch, event)

    @staticmethod
    def _messages_after_summary(
        messages: tuple[ConversationMessage | ExecutionMessage, ...],
        summary: ContextSummary | None,
    ) -> tuple[ConversationMessage | ExecutionMessage, ...]:
        """保留全部当前 Run 执行协议和摘要边界之后的正式历史。"""
        if summary is None:
            return messages
        return tuple(
            item
            for item in messages
            if isinstance(item, ExecutionMessage)
            or item.sequence > summary.covered_through_sequence
        )

    @staticmethod
    def _retain_after_compression(
        active_messages: tuple[ConversationMessage | ExecutionMessage, ...],
        compressed_messages: Sequence[ConversationMessage],
    ) -> tuple[ConversationMessage | ExecutionMessage, ...]:
        """按原顺序移除本次一次性摘要覆盖的正式历史。"""
        compressed_ids = {message.id for message in compressed_messages}
        return tuple(
            item
            for item in active_messages
            if isinstance(item, ExecutionMessage) or item.id not in compressed_ids
        )


__all__ = [
    "ContextCompressor",
    "ContextPreparation",
    "_compression_token_limits",
    "_render_messages",
    "_split_recent_suffix",
    "_turn_groups",
]
