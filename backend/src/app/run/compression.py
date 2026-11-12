"""在主模型调用前组织会话历史压缩。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from app.run.budget import RunLlmBudget
from app.run.context import (
    RunContextError,
    RunInputFileContext,
    build_llm_request,
    count_plain_text_tokens_async,
    estimate_llm_input_tokens,
)
from common.ids import new_id
from domain.llm.model import (
    LlmError,
    LlmMessage,
    LlmProviderError,
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
    ContextSummary,
    RecordRunUsageCommand,
    Run,
    RunCompressionFailedPayload,
    RunCompressionStartedPayload,
    RunEvent,
    RunLease,
    SaveContextSummaryCommand,
)
from domain.run.ports import RunRepository
from domain.skill import RunSkillPromptContext
from domain.tool import ToolRetryContext


class CompressionBatchError(RunContextError):
    """保存失败操作已经处理过的分批数量。"""

    def __init__(self, code: str, batch_count: int) -> None:
        super().__init__(code)
        self.batch_count = batch_count


class CompressionPublisher(Protocol):
    """已提交压缩事件的实时发布端口。"""

    async def publish_persisted(self, run_id: str, execution_epoch: int, event: RunEvent) -> None:
        """通知订阅者读取已提交的状态事件。"""


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
) -> tuple[list[tuple[ConversationMessage, ...]], tuple[ConversationMessage, ...]]:
    """按主模型完整请求的计数口径保留不超预算的连续近期轮次。"""
    remaining_groups = list(groups)
    recent_groups: list[tuple[ConversationMessage, ...]] = []
    while remaining_groups:
        group = remaining_groups[-1]
        candidate_groups = [group, *reversed(recent_groups)]
        candidate = tuple(message for item in candidate_groups for message in item)
        candidate_tokens, _ = await estimate_llm_input_tokens(
            run,
            candidate,
            profile.version,
            main_prompt=main_prompt,
            timeout_seconds=timeout_seconds,
            tools=tools,
            retry_context=retry_context,
            skill_context=skill_context,
            input_files=input_files,
        )
        if max(0, candidate_tokens - base_tokens) > recent_limit:
            break
        recent_groups.append(group)
        remaining_groups.pop()
    return remaining_groups, tuple(
        message for group in reversed(recent_groups) for message in group
    )


def _render_messages(messages: Sequence[ConversationMessage]) -> str:
    """把消息转换为带明确角色边界的 JSON 历史文本。"""
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
    """构造与供应商请求相同的摘要和新增历史封装。"""
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
    """为压缩请求预留窗口，并按主模型摘要预算保守限制输出额度。"""
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


class ContextCompressor:
    """按 Run 固定策略压缩较早会话历史并提交摘要版本。"""

    def __init__(
        self,
        repository: RunRepository,
        profiles: ModelProfileRepository,
        provider: LlmRuntimeTextProvider,
        publisher: CompressionPublisher,
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
        budget: RunLlmBudget | None = None,
    ) -> tuple[tuple[LlmMessage, ...], tuple[LlmTool, ...], int, RunEvent | None]:
        """压缩需要的历史并返回可直接调用主模型的完整上下文。"""
        run = lease.run
        # 读取当前可见的摘要版本；同时定位触发消息，压缩范围不能覆盖当前问题。
        summaries = await self._repository.list_context_summaries(
            run.session_id, run.owner_id, run.id
        )
        if summaries is None:
            raise RunContextError("run_context_unavailable")
        conversations = tuple(item for item in messages if isinstance(item, ConversationMessage))
        trigger = next((item for item in conversations if item.id == run.trigger_message_id), None)
        if trigger is None:
            raise RunContextError("run_context_trigger_missing")
        # 先估算 system/tools/重试上下文占用的基础 token，剩余部分才是历史预算。
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
        )
        input_budget = main_runtime.version.context_window_tokens - output_tokens - base_tokens
        if input_budget <= 0:
            raise RunContextError("run_context_budget_unavailable")
        summary_limit = int(input_budget * policy.summary_ratio)
        if summary_limit <= 0:
            raise RunContextError("run_context_budget_unavailable")
        # 只考虑覆盖到触发消息之前的摘要；在预算内选择覆盖范围最长的版本。
        candidates: list[ContextSummary] = []
        eligible_summaries: list[tuple[ContextSummary, int]] = []
        for item in summaries:
            if item.covered_through_sequence >= trigger.sequence:
                continue
            summary_tokens = await count_plain_text_tokens_async(
                item.text, main_runtime.version, timeout_seconds=timeout_seconds
            )
            eligible_summaries.append((item, summary_tokens))
            if summary_tokens <= summary_limit:
                candidates.append(item)
        summary = max(candidates, key=lambda item: item.covered_through_sequence, default=None)
        latest_summary = max(
            (item for item, _ in eligible_summaries),
            key=lambda item: item.covered_through_sequence,
            default=None,
        )
        # 最新摘要超出预算时触发一次重压缩（不直接使用其正文）。
        refresh_oversized_summary = summary is None and latest_summary is not None
        if refresh_oversized_summary:
            summary = latest_summary
        # 有可用摘要时只保留摘要覆盖点之后的消息与本 Run 执行消息。
        if summary is None:
            active_messages = messages
            summary_text = None
        else:
            active_messages = tuple(
                item
                for item in messages
                if isinstance(item, ExecutionMessage)
                or item.sequence > summary.covered_through_sequence
            )
            summary_text = None if refresh_oversized_summary else summary.text
        fallback_messages = (
            messages if refresh_oversized_summary or summary is None else active_messages
        )
        fallback_summary_text = None if refresh_oversized_summary else summary_text
        used_tokens, output_tokens = await estimate_llm_input_tokens(
            run,
            active_messages,
            main_runtime.version,
            timeout_seconds=timeout_seconds,
            main_prompt=main_prompt,
            summary_text=summary_text,
            tools=tools,
            retry_context=retry_context,
            skill_context=skill_context,
            input_files=input_files,
        )
        # 指南与目录已计入 base_tokens，只对扣除后历史用量应用触发比例。
        history_tokens = max(0, used_tokens - base_tokens)
        threshold = int(input_budget * policy.trigger_ratio)
        if history_tokens < threshold and not refresh_oversized_summary:
            built, tools, output = await build_llm_request(
                run,
                active_messages,
                main_runtime.version,
                timeout_seconds=timeout_seconds,
                main_prompt=main_prompt,
                summary_text=summary_text,
                tools=tools,
                retry_context=retry_context,
                skill_context=skill_context,
                input_files=input_files,
            )
            return built, tools, output, None

        # 待压缩范围：触发消息之前、摘要覆盖点之后的完整对话轮次。
        trigger_prefix = tuple(
            item
            for item in conversations
            if item.sequence < trigger.sequence
            and (summary is None or item.sequence > summary.covered_through_sequence)
        )
        groups = list(_turn_groups(trigger_prefix))
        if not groups and not refresh_oversized_summary:
            built, tools, output = await build_llm_request(
                run,
                active_messages,
                main_runtime.version,
                timeout_seconds=timeout_seconds,
                main_prompt=main_prompt,
                summary_text=summary_text,
                tools=tools,
                retry_context=retry_context,
                skill_context=skill_context,
                input_files=input_files,
            )
            return built, tools, output, None
        # 从尾部保留完整轮次，并将超出 20% 预算的连续前缀送入摘要。
        recent_limit = int(input_budget * policy.recent_ratio)
        groups, recent = await _split_recent_suffix(
            run,
            groups,
            main_runtime,
            base_tokens,
            recent_limit,
            timeout_seconds,
            main_prompt,
            skill_context,
            tools,
            retry_context,
            input_files,
        )
        if not groups and not refresh_oversized_summary:
            built, tools, output = await build_llm_request(
                run,
                active_messages,
                main_runtime.version,
                timeout_seconds=timeout_seconds,
                main_prompt=main_prompt,
                summary_text=summary_text,
                tools=tools,
                retry_context=retry_context,
                skill_context=skill_context,
                input_files=input_files,
            )
            return built, tools, output, None

        # 记录压缩开始事件；事件写入失败说明失租，必须终止。
        operation_id = new_id("cmpop")
        started = await self._repository.append_compression_event(
            run.id,
            lease.run.worker_id or "",
            run.execution_epoch,
            RunCompressionStartedPayload(operation_id=operation_id),
        )
        if started is None:
            raise RunContextError("run_execution_lease_lost")
        try:
            await self._publisher.publish_persisted(run.id, run.execution_epoch, started)
        except Exception:
            pass
        try:
            # 分批顺序压缩；中间摘要只驻内存，最终结果统一提交。
            chunks = await self._compress_groups(
                run,
                lease,
                groups,
                None if summary is None else summary.text,
                0 if summary is None else summary.covered_through_sequence,
                policy,
                main_runtime,
                operation_id,
                timeout_seconds,
                main_prompt,
                compression_prompt,
                skill_context,
                tools,
                retry_context,
                input_files,
                budget,
            )
        except RunContextError as error:
            if error.args and error.args[0] == "run_execution_lease_lost":
                raise
            failed = await self._repository.append_compression_event(
                run.id,
                lease.run.worker_id or "",
                run.execution_epoch,
                RunCompressionFailedPayload(
                    operation_id=operation_id,
                    safe_error_code=str(error),
                    batch_count=(
                        error.batch_count if isinstance(error, CompressionBatchError) else 0
                    ),
                ),
            )
            if failed is not None:
                try:
                    await self._publisher.publish_persisted(run.id, run.execution_epoch, failed)
                except Exception:
                    pass
            fallback = await build_llm_request(
                run,
                fallback_messages,
                main_runtime.version,
                timeout_seconds=timeout_seconds,
                main_prompt=main_prompt,
                summary_text=fallback_summary_text,
                tools=tools,
                retry_context=retry_context,
                skill_context=skill_context,
                input_files=input_files,
            )
            return *fallback, failed
        if not chunks:
            raise RunContextError("run_context_compression_failed")
        # 压缩成功：用最终摘要 + 覆盖点之后的消息重建完整主请求。
        final_summary, covered_sequence, batch_count, compression_runtime = chunks
        retained = tuple(
            item
            for item in active_messages
            if isinstance(item, ExecutionMessage) or item.sequence > covered_sequence
        )
        try:
            built, tools, output = await build_llm_request(
                run,
                retained,
                main_runtime.version,
                timeout_seconds=timeout_seconds,
                main_prompt=main_prompt,
                summary_text=final_summary,
                tools=tools,
                retry_context=retry_context,
                skill_context=skill_context,
                input_files=input_files,
            )
        except RunContextError as error:
            failed = await self._repository.append_compression_event(
                run.id,
                lease.run.worker_id or "",
                run.execution_epoch,
                RunCompressionFailedPayload(
                    operation_id=operation_id,
                    safe_error_code=str(error),
                    batch_count=batch_count,
                ),
            )
            if failed is not None:
                try:
                    await self._publisher.publish_persisted(run.id, run.execution_epoch, failed)
                except Exception:
                    pass
            fallback = await build_llm_request(
                run,
                fallback_messages,
                main_runtime.version,
                timeout_seconds=timeout_seconds,
                main_prompt=main_prompt,
                summary_text=fallback_summary_text,
                tools=tools,
                retry_context=retry_context,
                skill_context=skill_context,
                input_files=input_files,
            )
            return *fallback, failed
        # 完整请求校验失败时仍保留完整原文供回退判断，不保存摘要。
        saved = await self._repository.save_context_summary(
            SaveContextSummaryCommand(
                run_id=run.id,
                worker_id=lease.run.worker_id or "",
                execution_epoch=run.execution_epoch,
                operation_id=operation_id,
                summary_id=new_id("csum"),
                covered_through_sequence=covered_sequence,
                parent_summary_id=None if summary is None else summary.id,
                text=final_summary,
                policy_version_id=policy.id,
                compression_model_version_id=compression_runtime.version.id,
                prompt_version=policy.prompt_template_version,
                created_at=datetime.now(UTC),
                batch_count=batch_count,
            )
        )
        # 保存摘要版本失败（失租/冲突）时终止，避免使用未提交的摘要。
        if saved is None:
            raise RunContextError("run_execution_lease_lost")
        try:
            await self._publisher.publish_persisted(run.id, run.execution_epoch, saved[1])
        except Exception:
            pass
        return built, tools, output, saved[1]

    async def _compress_groups(
        self,
        run: Run,
        lease: RunLease,
        groups: list[tuple[ConversationMessage, ...]],
        prior_summary: str | None,
        prior_covered_sequence: int,
        policy: CompressionPolicyVersion,
        main_runtime: RuntimeModelProfile,
        operation_id: str,
        timeout_seconds: float,
        main_prompt: str,
        compression_prompt: str,
        skill_context: RunSkillPromptContext | None,
        tools: Sequence[LlmTool] | None,
        retry_context: Sequence[ToolRetryContext],
        input_files: Sequence[RunInputFileContext],
        budget: RunLlmBudget | None = None,
    ) -> tuple[str, int, int, RuntimeModelProfile]:
        """顺序压缩完整轮次；中间摘要仅驻内存。"""
        # 压缩模型按策略固定；配置不可用则无法压缩。
        runtime = await self._profiles.get_runtime(policy.compression_model_profile_id)
        if runtime is None:
            raise RunContextError("run_compression_model_unavailable")
        main_base_tokens, main_output_tokens = await estimate_llm_input_tokens(
            run,
            (),
            main_runtime.version,
            main_prompt=main_prompt,
            timeout_seconds=timeout_seconds,
            tools=tools,
            retry_context=retry_context,
            skill_context=skill_context,
            input_files=input_files,
        )
        # 摘要可占用的 token 预算 = 主模型剩余窗口 × 摘要比例。
        summary_budget = int(
            (main_runtime.version.context_window_tokens - main_output_tokens - main_base_tokens)
            * policy.summary_ratio
        )
        if summary_budget <= 0:
            raise RunContextError("run_context_budget_unavailable")
        compression_window = runtime.version.context_window_tokens
        available, output_limit = _compression_token_limits(
            compression_window, runtime.version.max_output_tokens, summary_budget
        )
        target_characters = min(summary_budget, output_limit) // 4
        if target_characters <= 0:
            raise RunContextError("run_context_budget_unavailable")
        summary = prior_summary
        batch_count = 0
        covered = 0
        pending: list[ConversationMessage] = []
        provider = self._provider
        # 逐轮拼接：超出压缩模型窗口时先摘要已累积的轮次，再继续。
        for group in groups:
            candidate = [*pending, *group]
            input_text = _render_compression_request(
                compression_prompt,
                summary,
                candidate,
                target_characters,
            )
            input_tokens = await count_plain_text_tokens_async(
                input_text,
                runtime.version,
                timeout_seconds=timeout_seconds,
            )
            if input_tokens > available and pending:
                try:
                    summary, covered = await self._summarize_batch(
                        provider,
                        runtime,
                        summary,
                        pending,
                        output_limit,
                        run,
                        lease,
                        operation_id,
                        batch_count,
                        target_characters,
                        timeout_seconds,
                        compression_prompt,
                        budget,
                    )
                except RunContextError as error:
                    raise CompressionBatchError(str(error), batch_count + 1) from error
                batch_count += 1
                if (
                    await count_plain_text_tokens_async(
                        summary, main_runtime.version, timeout_seconds=timeout_seconds
                    )
                    > summary_budget
                ):
                    raise CompressionBatchError("run_context_summary_too_large", batch_count)
                pending = []
                input_text = _render_compression_request(
                    compression_prompt,
                    summary,
                    group,
                    target_characters,
                )
                input_tokens = await count_plain_text_tokens_async(
                    input_text,
                    runtime.version,
                    timeout_seconds=timeout_seconds,
                )
            if input_tokens > available:
                raise RunContextError("run_compression_turn_too_large")
            pending.extend(group)
        # 收尾：把最后一批待压缩轮次提交给压缩模型。
        if pending:
            try:
                summary, covered = await self._summarize_batch(
                    provider,
                    runtime,
                    summary,
                    pending,
                    output_limit,
                    run,
                    lease,
                    operation_id,
                    batch_count,
                    target_characters,
                    timeout_seconds,
                    compression_prompt,
                    budget,
                )
            except RunContextError as error:
                raise CompressionBatchError(str(error), batch_count + 1) from error
            batch_count += 1
            if (
                await count_plain_text_tokens_async(
                    summary, main_runtime.version, timeout_seconds=timeout_seconds
                )
                > summary_budget
            ):
                raise CompressionBatchError("run_context_summary_too_large", batch_count)
        # 没有新增轮次但已有摘要（重压缩场景）：对原摘要再压缩一次。
        elif prior_summary is not None:
            encoded_input = _render_compression_request(
                compression_prompt,
                prior_summary,
                (),
                target_characters,
            )
            input_tokens = await count_plain_text_tokens_async(
                encoded_input, runtime.version, timeout_seconds=timeout_seconds
            )
            if input_tokens > available:
                raise RunContextError("run_compression_context_window_exceeded")
            try:
                summary, _ = await self._summarize_batch(
                    provider,
                    runtime,
                    prior_summary,
                    (),
                    output_limit,
                    run,
                    lease,
                    operation_id,
                    batch_count,
                    target_characters,
                    timeout_seconds,
                    compression_prompt,
                    budget,
                )
            except RunContextError as error:
                raise CompressionBatchError(str(error), batch_count + 1) from error
            batch_count += 1
            covered = prior_covered_sequence
            if (
                await count_plain_text_tokens_async(
                    summary, main_runtime.version, timeout_seconds=timeout_seconds
                )
                > summary_budget
            ):
                raise CompressionBatchError("run_context_summary_too_large", batch_count)
        # 最终摘要必须非空且不超过主模型的摘要预算。
        if summary is None or not summary.strip():
            raise CompressionBatchError("run_context_compression_failed", batch_count)
        if (
            await count_plain_text_tokens_async(
                summary, main_runtime.version, timeout_seconds=timeout_seconds
            )
            > summary_budget
        ):
            raise CompressionBatchError("run_context_summary_too_large", batch_count)
        return summary, covered, batch_count, runtime

    async def _summarize_batch(
        self,
        provider: LlmRuntimeTextProvider,
        runtime: RuntimeModelProfile,
        summary: str | None,
        messages: Sequence[ConversationMessage],
        output_limit: int,
        run: Run,
        lease: RunLease,
        operation_id: str,
        batch_index: int,
        target_characters: int,
        timeout_seconds: float,
        compression_prompt: str,
        budget: RunLlmBudget | None = None,
    ) -> tuple[str, int]:
        """调用压缩模型并确认输出完整的纯文本摘要。"""
        # 每次调用前重新读取压缩模型配置，配置可能已被禁用。
        current_runtime = await self._profiles.get_runtime(runtime.version.id)
        if current_runtime is None:
            raise RunContextError("run_compression_model_unavailable")
        runtime = current_runtime
        prompt = _compression_prompt(summary, messages, target_characters)
        request = LlmRequest(
            model_profile=runtime.version.id,
            messages=(
                LlmMessage(LlmRole.SYSTEM, compression_prompt),
                LlmMessage(LlmRole.USER, prompt),
            ),
            tool_choice="none",
            max_output_tokens=output_limit,
            timeout_seconds=min(timeout_seconds, runtime.version.request_timeout_seconds),
        )
        # 压缩调用最多重试 3 次；每次都必须先确认租约并预占预算。
        for attempt in range(1, 4):
            if not await self._repository.is_live_execution(run.id, run.execution_epoch):
                raise RunContextError("run_execution_lease_lost")
            if budget is not None:
                if not budget.can_compress:
                    raise RunContextError("run_context_compression_budget_exhausted")
                await budget.reserve()
            started = time.monotonic()
            response = None
            provider_error: LlmProviderError | None = None
            fatal_llm_error: LlmError | None = None
            try:
                response = await provider.complete(runtime, request)
            except LlmProviderError as error:
                provider_error = error
            except LlmError as error:
                fatal_llm_error = error
            # 与主回答一致：无论成败都记录用量，用于审计与预算核对。
            elapsed = max(0, int((time.monotonic() - started) * 1000))
            usage = None if response is None else response.usage
            usage_recorded = await self._repository.record_usage(
                RecordRunUsageCommand(
                    run_id=run.id,
                    worker_id=lease.run.worker_id or "",
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
                    recorded_at=datetime.now(UTC),
                    purpose="compression",
                    operation_id=operation_id,
                    batch_index=batch_index,
                    model_profile_id=runtime.version.id,
                    duration_ms=elapsed,
                )
            )
            if usage_recorded is None:
                raise RunContextError("run_execution_lease_lost")
            if fatal_llm_error is not None:
                raise RunContextError("run_context_compression_failed") from fatal_llm_error
            if provider_error is None:
                break
            # 只有标记为可安全重试的供应商错误才退避重试。
            if not provider_error.retry_safe or attempt == 3:
                raise RunContextError("run_context_compression_failed") from provider_error
            delay = max(provider_error.retry_after_seconds or 0.0, 0.25 * 2 ** (attempt - 1))
            await asyncio.sleep(delay)
        # 压缩输出必须是完整结束的纯文本，否则视为压缩失败。
        if response is None:
            raise RunContextError("run_context_compression_failed")
        if response.status != "completed" or response.incomplete_reason is not None:
            raise RunContextError("run_context_compression_incomplete")
        if len(response.output) != 1 or not isinstance(response.output[0], LlmText):
            raise RunContextError("run_context_compression_invalid")
        text = response.output[0].text.strip()
        if not text:
            raise RunContextError("run_context_compression_empty")
        return text, messages[-1].sequence if messages else 0
