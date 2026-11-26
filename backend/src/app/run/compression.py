"""在主模型调用前组织单次、连续边界的会话历史压缩。"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from app.run.budget import RunLlmBudget
from app.run.context import (
    LlmRequestSpec,
    RunContextError,
    count_conversation_group_tokens_async,
    count_plain_text_tokens_async,
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
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile
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

_MAX_TRACKED_FAILED_RUNS = 256


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
    """以用户消息开始划分连续对话轮次，避免拆开相邻的澄清问答组。"""
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
    groups: Sequence[tuple[ConversationMessage, ...]],
    profile: ModelProfileVersion,
    recent_limit: int,
    *,
    timeout_seconds: float,
) -> tuple[list[tuple[ConversationMessage, ...]], tuple[ConversationMessage, ...]]:
    """单趟按轮次分组反向累加 Token，保留不超过 recent_limit 的连续近期轮次后缀。"""
    if not groups or recent_limit <= 0:
        return list(groups), ()
    group_tokens = await count_conversation_group_tokens_async(
        groups,
        profile,
        timeout_seconds=timeout_seconds,
    )
    accumulated = 0
    split_index = len(groups)
    for index in range(len(groups) - 1, -1, -1):
        if accumulated + group_tokens[index] > recent_limit:
            break
        accumulated += group_tokens[index]
        split_index = index
    remaining = list(groups[:split_index])
    recent = tuple(message for group in groups[split_index:] for message in group)
    return remaining, recent


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
        "字符数只是保守生成指导，最终仍以主模型窗口校验为准。\n"
        "既有摘要：\n" + (summary or "（无）") + "\n新增历史：\n" + _render_messages(messages)
    )


def _compression_token_limits(
    context_window: int,
    max_output_tokens: int,
    main_summary_budget: int,
) -> tuple[int, int]:
    """为压缩请求预留协议和输出后返回可发送输入上限与输出上限。"""
    protocol_reserve = min(128, max(1, context_window // 16))
    usable_window = context_window - protocol_reserve
    if usable_window < 2 or max_output_tokens <= 0 or main_summary_budget <= 0:
        raise RunContextError("run_compression_context_window_exceeded")
    output_limit = min(max_output_tokens, max(1, usable_window // 3), main_summary_budget)
    input_limit = context_window - protocol_reserve - output_limit
    if input_limit <= 0:
        raise RunContextError("run_compression_context_window_exceeded")
    return input_limit, output_limit


def _bounded_budget(base: int, ratio: float, cap: int, feasible: int) -> int:
    """统一计算比例与绝对上限共同约束后的有效 Token 预算。"""
    return max(0, min(int(base * ratio), cap, feasible))


def _compression_failure_hint(safe_error_code: str) -> ContextStopReason:
    """按压缩失败安全码映射精确的上下文停止原因。"""
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
        # 仅在进程内存中记录同一 Run 失败过的候选边界及原因，避免同轮次循环反复重试压缩。
        self._failed_covered_through: dict[tuple[str, int], tuple[int, ContextStopReason]] = {}

    async def prepare(
        self,
        lease: RunLease,
        spec: LlmRequestSpec,
        main_runtime: RuntimeModelProfile,
        policy: CompressionPolicyVersion,
        *,
        compression_prompt: str,
        timeout_seconds: float,
        budget: RunLlmBudget | None = None,
        allow_compression: bool = True,
    ) -> ContextPreparation:
        """压缩一次新增旧历史，并在失败时优雅回退全部历史而不崩溃。"""
        run = lease.run
        summaries = await self._repository.list_context_summaries(
            run.session_id, run.owner_id, run.id
        )
        if summaries is None:
            raise RunContextError("run_context_unavailable")
        messages = tuple(spec.messages)
        conversations = tuple(item for item in messages if isinstance(item, ConversationMessage))
        trigger = next((item for item in conversations if item.id == run.trigger_message_id), None)
        if trigger is None:
            raise RunContextError("run_context_trigger_missing")

        # 复用最新兼容历史摘要作为唯一权威边界。
        eligible_summaries = tuple(
            item for item in summaries if item.covered_through_sequence < trigger.sequence
        )
        summary = max(
            eligible_summaries,
            key=lambda item: (item.covered_through_sequence, item.created_at, item.id),
            default=None,
        )
        active_messages = self._messages_after_summary(messages, summary)
        summary_text = None if summary is None else summary.text

        trigger_prefix = tuple(
            item
            for item in conversations
            if item.sequence < trigger.sequence
            and (summary is None or item.sequence > summary.covered_through_sequence)
        )
        groups = _turn_groups(trigger_prefix)

        # 先执行 1 次完整主上下文测量；未触及压缩线、90% 安全线与硬窗时直接短路径返回。
        active_spec = replace(spec, messages=active_messages, summary_text=summary_text)
        llm_messages, selected_tools, input_tokens, output_tokens = await measure_llm_request(
            active_spec,
            main_runtime.version,
            timeout_seconds=timeout_seconds,
        )
        current = ContextPreparation(
            messages=llm_messages,
            tools=selected_tools,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if not groups:
            return replace(
                current,
                had_new_history=False,
                stop_reason_hint=ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )

        safety_threshold = (main_runtime.version.context_window_tokens * 9 + 9) // 10
        if (
            current.input_tokens < policy.trigger_tokens
            and current.input_tokens < safety_threshold
            and current.input_tokens + current.output_tokens
            <= main_runtime.version.context_window_tokens
        ):
            return current

        # 主请求固定基础预算必须计入输入文件与视觉图片，准确扣除可用历史窗口。
        _, _, base_tokens, base_output_tokens = await measure_llm_request(
            replace(spec, messages=(), summary_text=None),
            main_runtime.version,
            timeout_seconds=timeout_seconds,
        )
        feasible_history = max(
            0,
            main_runtime.version.context_window_tokens - base_output_tokens - base_tokens,
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

        remaining_groups, _ = await _split_recent_suffix(
            groups,
            main_runtime.version,
            recent_limit,
            timeout_seconds=timeout_seconds,
        )
        if not remaining_groups:
            return replace(
                current,
                had_new_history=True,
                stop_reason_hint=ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )

        # 仅因 I+O 超硬窗但输入未达触发线和 90% 安全线时，不触发压缩，交由执行器统一裁决硬窗停止。
        if current.input_tokens < policy.trigger_tokens and current.input_tokens < safety_threshold:
            return current

        candidate_messages = tuple(message for group in remaining_groups for message in group)
        covered_through = candidate_messages[-1].sequence
        base_summary_id = None if summary is None else summary.id

        # 同一 Run 内存去重：相同或更窄的已失败候选范围不再重试压缩，保留原始停止原因直接回退。
        failed_record = self._failed_covered_through.get((run.id, run.execution_epoch))
        if failed_record is not None and covered_through <= failed_record[0]:
            return replace(
                current,
                had_new_history=True,
                stop_reason_hint=failed_record[1],
            )
        if not allow_compression:
            return replace(
                current,
                had_new_history=True,
                stop_reason_hint=ContextStopReason.COMPRESSION_BUDGET_EXHAUSTED,
            )

        runtime = await self._profiles.get_runtime(policy.compression_model_profile_id)
        operation_id = new_id("cmpop")
        started = await self._repository.append_compression_event(
            run.id,
            run.worker_id or "",
            run.execution_epoch,
            RunCompressionStartedPayload(operation_id=operation_id),
        )
        if started is None:
            raise RunContextError("run_execution_lease_lost")
        await self._publish(started, run)

        if runtime is None or summary_limit <= 0:
            code = (
                "run_compression_model_unavailable"
                if runtime is None
                else "run_context_budget_unavailable"
            )
            stop_reason = _compression_failure_hint(code)
            self._remember_failure(run, covered_through, stop_reason)
            failed = await self._append_failure(
                lease, operation_id, 0, code, base_summary_id, covered_through
            )
            return replace(
                current,
                event=failed,
                had_new_history=True,
                stop_reason_hint=stop_reason,
            )

        final_summary, error_code, dispatched = await self._compress_single(
            run,
            runtime,
            summary_text,
            candidate_messages,
            summary_limit,
            operation_id,
            timeout_seconds,
            compression_prompt,
            budget,
        )
        if error_code is not None:
            stop_reason = _compression_failure_hint(error_code)
            self._remember_failure(run, covered_through, stop_reason)
            failed = await self._append_failure(
                lease,
                operation_id,
                1 if dispatched else 0,
                error_code,
                base_summary_id,
                covered_through,
            )
            return replace(
                current,
                event=failed,
                had_new_history=True,
                compression_called=dispatched,
                stop_reason_hint=stop_reason,
            )

        # 用主模型完整测量压缩后的上下文，作为是否满足主模型硬窗的唯一权威裁决。
        retained = self._retain_after_compression(active_messages, candidate_messages)
        final_spec = replace(spec, messages=retained, summary_text=final_summary)
        try:
            final_msgs, final_tools, final_in, final_out = await measure_llm_request(
                final_spec,
                main_runtime.version,
                timeout_seconds=timeout_seconds,
            )
        except RunContextError as error:
            stop_reason = _compression_failure_hint(str(error))
            self._remember_failure(run, covered_through, stop_reason)
            failed = await self._append_failure(
                lease, operation_id, 1, str(error), base_summary_id, covered_through
            )
            return replace(
                current,
                event=failed,
                had_new_history=True,
                compression_called=True,
                stop_reason_hint=stop_reason,
            )
        if final_in + final_out > main_runtime.version.context_window_tokens:
            stop_reason = _compression_failure_hint("run_context_window_exceeded")
            self._remember_failure(run, covered_through, stop_reason)
            failed = await self._append_failure(
                lease,
                operation_id,
                1,
                "run_context_window_exceeded",
                base_summary_id,
                covered_through,
            )
            return replace(
                current,
                event=failed,
                had_new_history=True,
                compression_called=True,
                stop_reason_hint=stop_reason,
            )

        saved = await self._repository.save_context_summary(
            SaveContextSummaryCommand(
                run_id=run.id,
                worker_id=run.worker_id or "",
                execution_epoch=run.execution_epoch,
                operation_id=operation_id,
                summary_id=new_id("csum"),
                covered_through_sequence=covered_through,
                parent_summary_id=base_summary_id,
                text=final_summary,
                policy_version_id=policy.id,
                compression_model_version_id=runtime.version.id,
                prompt_version=policy.prompt_template_version,
                created_at=datetime.now(UTC),
                batch_count=1,
            )
        )
        if saved is None:
            raise RunContextError("run_execution_lease_lost")
        self._failed_covered_through.pop((run.id, run.execution_epoch), None)
        completed = saved[1]
        await self._publish(completed, run)
        return ContextPreparation(
            messages=final_msgs,
            tools=final_tools,
            input_tokens=final_in,
            output_tokens=final_out,
            event=completed,
            had_new_history=True,
            compression_called=True,
            compression_completed=True,
        )

    async def _compress_single(
        self,
        run: Run,
        runtime: RuntimeModelProfile,
        prior_summary: str | None,
        messages: Sequence[ConversationMessage],
        summary_limit: int,
        operation_id: str,
        timeout_seconds: float,
        compression_prompt: str,
        budget: RunLlmBudget | None,
    ) -> tuple[str, str | None, bool]:
        """把旧摘要与候选历史一次装入并最多派发一次压缩调用；返回 (摘要, 错误码, 是否已派发)。"""
        try:
            input_limit, output_limit = _compression_token_limits(
                runtime.version.context_window_tokens,
                runtime.version.max_output_tokens,
                summary_limit,
            )
            target_characters = min(summary_limit, output_limit) // 4
            if target_characters <= 0:
                return "", "run_context_budget_unavailable", False
            user_content = _compression_prompt(prior_summary, messages, target_characters)
            input_tokens = await count_plain_text_tokens_async(
                f"{compression_prompt}\n{user_content}",
                runtime.version,
                timeout_seconds=timeout_seconds,
            )
            if input_tokens > input_limit:
                return "", "run_compression_context_window_exceeded", False
        except RunContextError as error:
            return "", str(error), False

        if not await self._repository.is_live_execution(run.id, run.execution_epoch):
            raise RunContextError("run_execution_lease_lost")
        if budget is not None:
            if not budget.can_compress:
                return "", "run_context_compression_budget_exhausted", False
            budget.reserve()

        # 压缩隔离铁律：发往压缩模型的请求严格为纯文本，绝不附带任何 LlmImagePart。
        request = LlmRequest(
            model_profile=runtime.version.id,
            messages=(
                LlmMessage(LlmRole.SYSTEM, compression_prompt),
                LlmMessage(LlmRole.USER, user_content),
            ),
            tool_choice="none",
            max_output_tokens=output_limit,
            timeout_seconds=min(timeout_seconds, runtime.version.request_timeout_seconds),
        )
        started = time.monotonic()
        response = None
        failure: Exception | None = None
        try:
            # 严格只调用一次 complete，不做应用层重试。
            response = await self._provider.complete(runtime, request)
        except Exception as error:
            failure = error

        elapsed = max(0, int((time.monotonic() - started) * 1000))
        usage = None if response is None else response.usage
        recorded = await self._repository.record_usage(
            RecordRunUsageCommand(
                run_id=run.id,
                worker_id=run.worker_id or "",
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
                llm_request_count=None if budget is None else budget.used,
            )
        )
        if recorded is None:
            raise RunContextError("run_execution_lease_lost")
        if failure is not None or response is None:
            return "", "run_context_compression_failed", True
        if response.status != "completed" or response.incomplete_reason is not None:
            return "", "run_context_compression_incomplete", True
        if len(response.output) != 1 or not isinstance(response.output[0], LlmText):
            return "", "run_context_compression_invalid", True
        text = response.output[0].text.strip()
        if not text:
            return "", "run_context_compression_empty", True
        return text, None, True

    def _remember_failure(
        self,
        run: Run,
        covered_through_sequence: int,
        stop_reason: ContextStopReason,
    ) -> None:
        """在有界内存字典中登记当前 Run 已失败的候选边界及原因，防止常驻 Worker 内存增长。"""
        key = (run.id, run.execution_epoch)
        previous = self._failed_covered_through.get(key)
        if previous is None or covered_through_sequence >= previous[0]:
            self._failed_covered_through[key] = (covered_through_sequence, stop_reason)
        while len(self._failed_covered_through) > _MAX_TRACKED_FAILED_RUNS:
            del self._failed_covered_through[next(iter(self._failed_covered_through))]

    async def _append_failure(
        self,
        lease: RunLease,
        operation_id: str,
        batch_count: int,
        safe_error_code: str,
        base_summary_id: str | None,
        covered_through_sequence: int,
    ) -> RunEvent | None:
        """提交固定的 0/1 批次压缩失败结论。"""
        failed = await self._repository.append_compression_event(
            lease.run.id,
            lease.run.worker_id or "",
            lease.run.execution_epoch,
            RunCompressionFailedPayload(
                operation_id=operation_id,
                safe_error_code=safe_error_code,
                batch_count=batch_count,
                candidate_base_summary_id=base_summary_id,
                candidate_covered_through_sequence=covered_through_sequence,
            ),
        )
        if failed is not None:
            await self._publish(failed, lease.run)
        return failed

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
