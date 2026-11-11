"""上下文压缩切分与调用用量聚合回归。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock

import pytest
from system_prompt_support import (
    TEST_COMPRESSION_PROMPT,
    TEST_COMPRESSION_PROMPT_ID,
    TEST_MAIN_PROMPT,
    TEST_MAIN_PROMPT_ID,
)

from adapter.run.repository.run import SqlAlchemyRunRepository
from app.run import compression as compression_module
from app.run.budget import RunLlmBudget
from app.run.compression import (
    ContextCompressor,
    _compression_token_limits,
    _render_messages,
    _split_recent_suffix,
    _turn_groups,
)
from app.run.context import (
    RunContextError,
    count_plain_text_tokens_async,
    estimate_llm_input_tokens,
)
from domain.llm.model import LlmProviderError, LlmRequest, LlmResponse, LlmText, LlmUsage
from domain.llm.model.compression_policy import CompressionPolicyVersion
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile
from domain.llm.ports import LlmRuntimeTextProvider
from domain.message import (
    ConversationMessage,
    ConversationRole,
    ExecutionMessage,
    FileRefPart,
    MessageMetadata,
    MessageStatus,
    TextPart,
)
from domain.run.model import (
    ContextSummary,
    Run,
    RunAttemptUsage,
    RunCompressionFailedPayload,
    RunCompressionStartedPayload,
    RunEvent,
    RunEventPayload,
    RunLease,
    RunStatus,
    RunUsage,
)
from domain.run.ports import RunRepository
from domain.tool import ToolExecutionContext


def _message(
    sequence: int,
    role: ConversationRole,
    *,
    run_id: str | None = None,
    context_turn_id: str | None = None,
) -> ConversationMessage:
    """创建仅用于轮次边界测试的正式消息。"""
    return ConversationMessage(
        id=f"msg_{sequence}",
        session_id="ses_test",
        role=role,
        status=MessageStatus.COMPLETED,
        sequence=sequence,
        parts=(TextPart(f"message {sequence}"),),
        reply_to_message_id=None,
        run_id=run_id,
        client_message_id=None,
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        metadata=MessageMetadata(),
        context_turn_id=context_turn_id,
    )


def test_turn_groups_keep_assistant_followups_with_their_user_turn() -> None:
    """同一用户轮次中的连续助手消息不能被拆分。"""
    groups = _turn_groups(
        (
            _message(1, ConversationRole.USER),
            _message(2, ConversationRole.ASSISTANT),
            _message(3, ConversationRole.ASSISTANT),
            _message(4, ConversationRole.USER),
            _message(5, ConversationRole.ASSISTANT),
        )
    )
    assert [[item.sequence for item in group] for group in groups] == [[1, 2, 3], [4, 5]]


def test_turn_groups_keep_same_run_clarification_messages_together() -> None:
    """同一 Run 的多轮澄清问答不被拆成可独立压缩的轮次。"""
    groups = _turn_groups(
        (
            _message(1, ConversationRole.USER, run_id="run_a"),
            _message(2, ConversationRole.ASSISTANT, run_id="run_a"),
            _message(3, ConversationRole.USER, run_id="run_a"),
            _message(4, ConversationRole.ASSISTANT, run_id="run_a"),
            _message(5, ConversationRole.USER, run_id="run_b"),
        )
    )
    assert [[item.sequence for item in group] for group in groups] == [[1, 2, 3, 4], [5]]


def test_turn_groups_join_retry_trigger_to_answer_using_stable_context_turn_id() -> None:
    """失败 Run 的原触发消息与重试 Run 的澄清答案仍属于同一逻辑轮次。"""
    groups = _turn_groups(
        (
            _message(1, ConversationRole.USER, run_id="failed_run", context_turn_id="trigger_1"),
            _message(
                2,
                ConversationRole.ASSISTANT,
                run_id="failed_run",
                context_turn_id="trigger_1",
            ),
            _message(3, ConversationRole.USER, run_id="retry_run", context_turn_id="trigger_1"),
            _message(
                4,
                ConversationRole.ASSISTANT,
                run_id="retry_run",
                context_turn_id="trigger_1",
            ),
            _message(5, ConversationRole.USER, run_id="next_run", context_turn_id="trigger_5"),
        )
    )
    assert [[item.sequence for item in group] for group in groups] == [[1, 2, 3, 4], [5]]


def test_compression_render_keeps_attachment_names() -> None:
    """摘要输入保留附件文件名占位，避免压缩丢失文件线索。"""
    message = _message(1, ConversationRole.USER)
    message = replace(message, parts=(FileRefPart("file_1", "report.pdf"),))
    assert "[附件：report.pdf]" in _render_messages((message,))


@pytest.mark.parametrize("context_window", [128, 600, 4096])
def test_compression_budget_reserves_input_even_when_output_limit_fills_window(
    context_window: int,
) -> None:
    """max output 达到窗口上限时仍留有正输入空间，避免合法配置预算归零。"""
    input_limit, output_limit = _compression_token_limits(
        context_window, context_window, main_summary_budget=100
    )
    assert input_limit > 0
    assert output_limit > 0
    assert output_limit <= 100
    assert input_limit + output_limit < context_window
    assert output_limit <= context_window


def test_recent_suffix_budget_uses_complete_main_request_count_for_short_messages() -> None:
    """大量短消息按主请求 tokenizer 和 framing 预算保留连续后缀。"""
    now = datetime.now(UTC)
    run = Run(
        id="run_recent_budget",
        session_id="ses_recent_budget",
        owner_id="usr_recent_budget",
        status=RunStatus.RUNNING,
        trigger_message_id="msg_101",
        context_cutoff_sequence=101,
        retry_of_run_id=None,
        model_config_version_id="profile_recent_budget",
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_recent_budget",
        lease_expires_at=now + timedelta(minutes=1),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(minutes=1),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )
    profile_version = ModelProfileVersion(
        id="profile_recent_budget",
        name="main",
        protocol="chat_completions",
        base_url="https://llm.example.test/v1",
        model="main-model",
        has_api_key=True,
        context_window_tokens=8192,
        tokenizer_encoding="o200k_base",
        supports_stream=True,
        supports_tools=True,
        max_output_tokens=512,
        request_timeout_seconds=10,
        execution_timeout_seconds=30,
        max_concurrency=1,
        temperature=None,
        enabled=True,
        created_at=now,
    )
    profile = RuntimeModelProfile(version=profile_version, api_key="test")
    messages = tuple(
        _message(
            sequence,
            ConversationRole.USER if sequence % 2 else ConversationRole.ASSISTANT,
        )
        for sequence in range(1, 102)
    )
    groups = list(_turn_groups(messages[:-1]))

    async def scenario() -> None:
        base_tokens, output_tokens = await estimate_llm_input_tokens(
            run,
            (),
            profile_version,
            main_prompt=TEST_MAIN_PROMPT,
            timeout_seconds=10,
        )
        input_budget = profile_version.context_window_tokens - output_tokens - base_tokens
        recent_limit = int(input_budget * 0.2)
        remaining, recent = await _split_recent_suffix(
            run,
            groups,
            profile,
            base_tokens,
            recent_limit,
            timeout_seconds=10,
            main_prompt=TEST_MAIN_PROMPT,
        )
        actual_tokens, _ = await estimate_llm_input_tokens(
            run,
            recent,
            profile_version,
            main_prompt=TEST_MAIN_PROMPT,
            timeout_seconds=10,
        )
        assert recent
        assert max(0, actual_tokens - base_tokens) <= recent_limit
        assert [item.sequence for item in (*recent, *messages[-1:])] == list(
            range(len(remaining) * 2 + 1, 102)
        )

    asyncio.run(scenario())


def test_compression_retry_consumes_the_same_atomic_llm_budget() -> None:
    """压缩的每次应用层重试都先占用同一 Run 的原子 LLM 额度。"""
    now = datetime.now(UTC)
    profile_version = ModelProfileVersion(
        id="profile_compression_budget",
        name="compression",
        protocol="chat_completions",
        base_url="https://llm.example.test/v1",
        model="compression-model",
        has_api_key=True,
        context_window_tokens=8192,
        tokenizer_encoding="o200k_base",
        supports_stream=False,
        supports_tools=False,
        max_output_tokens=512,
        request_timeout_seconds=10,
        execution_timeout_seconds=30,
        max_concurrency=1,
        temperature=None,
        enabled=True,
        created_at=now,
    )
    runtime = RuntimeModelProfile(version=profile_version, api_key="test")
    run = Run(
        id="run_compression_budget",
        session_id="ses_compression_budget",
        owner_id="usr_compression_budget",
        status=RunStatus.RUNNING,
        trigger_message_id="msg_2",
        context_cutoff_sequence=2,
        retry_of_run_id=None,
        model_config_version_id="profile_main_budget",
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_compression_budget",
        lease_expires_at=now + timedelta(minutes=1),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(minutes=1),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )
    lease = RunLease(run, run.lease_expires_at or now, run.execution_deadline_at or now)
    reserve_calls: list[int] = []
    usage_calls: list[object] = []

    class Repository:
        async def reserve_llm_request(self, command: object) -> int:
            reserve_calls.append(1)
            return len(reserve_calls) + 36

        async def is_live_execution(self, *_: object) -> bool:
            return True

        async def record_usage(self, command: object) -> Run:
            usage_calls.append(command)
            return run

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, *_: object) -> LlmResponse:
            self.calls += 1
            if self.calls == 1:
                raise LlmProviderError(
                    "llm_temporary_upstream_error",
                    retry_after_seconds=0,
                    retry_safe=True,
                )
            return LlmResponse(
                output=(LlmText("compressed summary"),),
                status="completed",
                incomplete_reason=None,
                model="compression-model",
                response_id="response-compression",
                request_id=None,
                usage=LlmUsage(10, 2, 12),
            )

    async def scenario() -> None:
        repository = Repository()
        budget = RunLlmBudget(
            cast(RunRepository, repository),
            ToolExecutionContext(
                owner_id=run.owner_id,
                chat_session_id=run.session_id,
                project_id=None,
                run_id=run.id,
                execution_epoch=run.execution_epoch,
                auth_session_id="auth_compression_budget",
                deadline_at=run.execution_deadline_at or now,
            ),
            used=36,
            maximum=40,
            worker_id=run.worker_id or "",
        )
        compressor = ContextCompressor(
            cast(RunRepository, repository),
            SimpleNamespace(get_runtime=AsyncMock(return_value=runtime)),
            cast(LlmRuntimeTextProvider, Provider()),
            SimpleNamespace(publish_persisted=AsyncMock()),
        )
        summary, covered = await compressor._summarize_batch(
            compressor._provider,
            runtime,
            None,
            (_message(1, ConversationRole.USER),),
            128,
            run,
            lease,
            "cmpop_budget",
            0,
            128,
            10,
            TEST_COMPRESSION_PROMPT,
            budget,
        )
        assert summary == "compressed summary"
        assert covered == 1
        assert budget.used == 38
        assert len(reserve_calls) == 2
        assert [getattr(item, "purpose", None) for item in usage_calls] == [
            "compression",
            "compression",
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("main_encoding", "compression_encoding"),
    [("cl100k_base", "o200k_base"), ("o200k_base", "cl100k_base")],
)
def test_prepare_bounds_summary_output_across_tokenizers(
    main_encoding: Literal["cl100k_base", "o200k_base"],
    compression_encoding: Literal["cl100k_base", "o200k_base"],
) -> None:
    """完整 prepare 流程按摘要预算限制输出，并以主 tokenizer 实际验证结果。"""
    now = datetime.now(UTC)
    run = Run(
        id="run_cross_tokenizer",
        session_id="ses_cross_tokenizer",
        owner_id="usr_cross_tokenizer",
        status=RunStatus.RUNNING,
        trigger_message_id="msg_9",
        context_cutoff_sequence=9,
        retry_of_run_id=None,
        model_config_version_id="profile_main_cross",
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_cross_tokenizer",
        lease_expires_at=now + timedelta(minutes=1),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(minutes=1),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )

    def _runtime(
        profile_id: str,
        encoding: Literal["cl100k_base", "o200k_base"],
        window: int,
        output: int,
    ) -> RuntimeModelProfile:
        return RuntimeModelProfile(
            version=ModelProfileVersion(
                id=profile_id,
                name=profile_id,
                protocol="chat_completions",
                base_url="https://llm.example.test/v1",
                model=profile_id,
                has_api_key=True,
                context_window_tokens=window,
                tokenizer_encoding=encoding,
                supports_stream=profile_id == "profile_main_cross",
                supports_tools=profile_id == "profile_main_cross",
                max_output_tokens=output,
                request_timeout_seconds=10,
                execution_timeout_seconds=30,
                max_concurrency=1,
                temperature=None,
                enabled=True,
                created_at=now,
            ),
            api_key="test",
        )

    main = _runtime("profile_main_cross", main_encoding, 2048, 128)
    compression = _runtime("profile_compression_cross", compression_encoding, 4096, 4096)
    policy = CompressionPolicyVersion(
        id="policy_cross_tokenizer",
        compression_model_profile_id=compression.version.id,
        trigger_ratio=0.8,
        recent_ratio=0.2,
        summary_ratio=0.1,
        prompt_template_version="context-summary-v1",
        created_at=now,
    )
    messages = tuple(
        replace(
            _message(
                sequence,
                ConversationRole.USER if sequence % 2 else ConversationRole.ASSISTANT,
            ),
            parts=(TextPart(("中英文 Mixed context; code: value_123 = '测试'; " * 24)[:700]),),
        )
        for sequence in range(1, 9)
    ) + (_message(9, ConversationRole.USER),)
    summary_text = "目标已确认；约束保留；代码：x=1"
    captured: dict[str, object] = {}
    completed_event = RunEvent(
        id="event_summary_completed",
        run_id=run.id,
        sequence=2,
        type="compression_completed",
        created_at=now,
        payload=RunCompressionStartedPayload(operation_id="cmpop_cross_tokenizer"),
    )

    class Repository:
        async def list_context_summaries(self, *_: str) -> tuple[ContextSummary, ...]:
            return ()

        async def append_compression_event(self, *_: object, **__: object) -> RunEvent | None:
            return completed_event

        async def is_live_execution(self, *_: object) -> bool:
            return True

        async def record_usage(self, *_: object) -> Run:
            return run

        async def save_context_summary(self, command: object) -> tuple[ContextSummary, RunEvent]:
            captured["command"] = command
            return (
                ContextSummary(
                    id="summary_cross_tokenizer",
                    session_id=run.session_id,
                    owner_id=run.owner_id,
                    covered_through_sequence=8,
                    parent_summary_id=None,
                    text=summary_text,
                    run_id=run.id,
                    execution_epoch=run.execution_epoch,
                    operation_id="cmpop_cross_tokenizer",
                    policy_version_id=policy.id,
                    compression_model_version_id=compression.version.id,
                    prompt_version=policy.prompt_template_version,
                    compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
                    created_at=now,
                ),
                completed_event,
            )

    class Provider:
        async def complete(self, _runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
            captured["request"] = request
            return LlmResponse(
                output=(LlmText(summary_text),),
                status="completed",
                incomplete_reason=None,
                model="summary-model",
                response_id="response_cross_tokenizer",
                request_id=None,
                usage=LlmUsage(input_tokens=100, output_tokens=20, total_tokens=120),
            )

    async def scenario() -> None:
        base_tokens, output_tokens = await estimate_llm_input_tokens(
            run,
            (),
            main.version,
            main_prompt=TEST_MAIN_PROMPT,
            timeout_seconds=10,
        )
        summary_budget = int(
            (main.version.context_window_tokens - output_tokens - base_tokens)
            * policy.summary_ratio
        )
        assert run.lease_expires_at is not None and run.execution_deadline_at is not None
        result = await ContextCompressor(
            cast(RunRepository, Repository()),
            SimpleNamespace(get_runtime=AsyncMock(return_value=compression)),
            cast(LlmRuntimeTextProvider, Provider()),
            SimpleNamespace(publish_persisted=AsyncMock()),
        ).prepare(
            RunLease(run, run.lease_expires_at, run.execution_deadline_at),
            messages,
            main,
            policy,
            main_prompt=TEST_MAIN_PROMPT,
            compression_prompt=TEST_COMPRESSION_PROMPT,
            timeout_seconds=10,
        )
        request = cast(LlmRequest, captured["request"])
        assert request.messages[0].content == TEST_COMPRESSION_PROMPT
        assert request.max_output_tokens is not None
        target_characters = min(summary_budget, request.max_output_tokens) // 4
        assert request.max_output_tokens <= summary_budget
        assert len(summary_text) <= target_characters
        assert f"不超过 {target_characters} 个 Unicode 字符" in request.messages[1].content
        compression_tokens = await count_plain_text_tokens_async(
            summary_text, compression.version, timeout_seconds=10
        )
        assert compression_tokens <= request.max_output_tokens
        actual_summary_tokens = await count_plain_text_tokens_async(
            summary_text, main.version, timeout_seconds=10
        )
        assert actual_summary_tokens <= summary_budget
        assert result[3] is not None

    asyncio.run(scenario())


def test_usage_merge_keys_compression_batches_and_preserves_legacy_totals() -> None:
    """不同压缩操作不冲突，只有旧聚合值的历史用量仍被计入。"""
    current = RunUsage.model_validate(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "complete": False,
            "attempts": [],
        }
    )
    first = RunAttemptUsage(
        execution_epoch=1,
        attempt=1,
        purpose="compression",
        operation_id="cmpop_a",
        batch_index=0,
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        complete=True,
    )
    second = first.model_copy(update={"operation_id": "cmpop_b"})
    merged = SqlAlchemyRunRepository._merge_usage(
        current.model_dump(mode="json"), RunUsage(attempts=(first, second))
    )
    assert merged.input_tokens == 120
    assert merged.output_tokens == 24
    assert merged.total_tokens == 144
    assert merged.compression_tokens == 24
    assert merged.answer_tokens == 120
    assert not merged.complete
    no_op = SqlAlchemyRunRepository._merge_usage(merged.model_dump(mode="json"), RunUsage())
    assert no_op == merged


def test_usage_merge_assigns_legacy_total_only_to_answer_tokens() -> None:
    """旧聚合仅有 total 时将其归入答案用量，未知字段继续未知。"""
    current = RunUsage.model_validate({"total_tokens": 120, "complete": False, "attempts": []})
    compression = RunAttemptUsage(
        execution_epoch=1,
        attempt=1,
        purpose="compression",
        operation_id="cmpop_legacy_total",
        batch_index=0,
        input_tokens=8,
        output_tokens=2,
        total_tokens=10,
        complete=True,
    )
    merged = SqlAlchemyRunRepository._merge_usage(
        current.model_dump(mode="json"), RunUsage(attempts=(compression,))
    )
    assert merged.total_tokens == 130
    assert merged.answer_tokens == 120
    assert merged.input_tokens == 8
    assert merged.output_tokens == 2
    assert merged.compression_tokens == 10


def test_compression_retries_only_provider_errors_and_records_each_attempt() -> None:
    """可重试的纯传输错误最多尝试三次，且每次单独记录用量。"""
    now = datetime.now(UTC)
    run = Run(
        id="run_retry_test",
        session_id="ses_retry_test",
        owner_id="usr_retry_test",
        status=RunStatus.RUNNING,
        trigger_message_id="msg_2",
        context_cutoff_sequence=2,
        retry_of_run_id=None,
        model_config_version_id="profile_main",
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_retry_test",
        lease_expires_at=now + timedelta(minutes=1),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(minutes=1),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )
    lease = RunLease(run, now + timedelta(minutes=1), now + timedelta(minutes=1))
    profile = ModelProfileVersion(
        id="profile_compression",
        name="compression",
        protocol="chat_completions",
        base_url="https://llm.example.test/v1",
        model="summary-model",
        has_api_key=True,
        context_window_tokens=4096,
        tokenizer_encoding="o200k_base",
        supports_stream=False,
        supports_tools=False,
        max_output_tokens=256,
        request_timeout_seconds=10,
        execution_timeout_seconds=20,
        max_concurrency=1,
        temperature=None,
        enabled=True,
        created_at=now,
    )
    runtime = RuntimeModelProfile(version=profile, api_key="test")
    provider = SimpleNamespace(
        complete=AsyncMock(side_effect=LlmProviderError("llm_rate_limited", retry_safe=True))
    )
    repository = SimpleNamespace(
        is_live_execution=AsyncMock(return_value=True),
        record_usage=AsyncMock(return_value=run),
    )
    compressor = ContextCompressor(
        repository,
        SimpleNamespace(get_runtime=AsyncMock(return_value=runtime)),
        provider,
        SimpleNamespace(),
    )
    with pytest.raises(RunContextError, match="run_context_compression_failed"):
        asyncio.run(
            compressor._summarize_batch(
                provider,
                runtime,
                None,
                (_message(1, ConversationRole.USER),),
                64,
                run,
                lease,
                "cmpop_retry_test",
                0,
                30,
                30,
                TEST_COMPRESSION_PROMPT,
            )
        )
    assert provider.complete.await_count == 3
    assert repository.record_usage.await_count == 3


def test_compression_does_not_retry_uncertain_provider_outcome() -> None:
    """超时或连接中断等无法确认未执行的调用只尝试一次。"""
    now = datetime.now(UTC)
    run = Run(
        id="run_uncertain_test",
        session_id="ses_uncertain_test",
        owner_id="usr_uncertain_test",
        status=RunStatus.RUNNING,
        trigger_message_id="msg_2",
        context_cutoff_sequence=2,
        retry_of_run_id=None,
        model_config_version_id="profile_main",
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_uncertain_test",
        lease_expires_at=now + timedelta(minutes=1),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(minutes=1),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )
    lease = RunLease(run, now + timedelta(minutes=1), now + timedelta(minutes=1))
    profile = ModelProfileVersion(
        id="profile_uncertain",
        name="compression",
        protocol="chat_completions",
        base_url="https://llm.example.test/v1",
        model="summary-model",
        has_api_key=True,
        context_window_tokens=2048,
        tokenizer_encoding="o200k_base",
        supports_stream=False,
        supports_tools=False,
        max_output_tokens=512,
        request_timeout_seconds=10,
        execution_timeout_seconds=20,
        max_concurrency=1,
        temperature=None,
        enabled=True,
        created_at=now,
    )
    runtime = RuntimeModelProfile(version=profile, api_key="test")
    provider = SimpleNamespace(
        complete=AsyncMock(side_effect=LlmProviderError("llm_timeout", retry_safe=False))
    )
    repository = SimpleNamespace(
        is_live_execution=AsyncMock(return_value=True),
        record_usage=AsyncMock(return_value=run),
    )
    compressor = ContextCompressor(
        repository,
        SimpleNamespace(get_runtime=AsyncMock(return_value=runtime)),
        provider,
        SimpleNamespace(),
    )
    with pytest.raises(RunContextError, match="run_context_compression_failed"):
        asyncio.run(
            compressor._summarize_batch(
                provider,
                runtime,
                None,
                (_message(1, ConversationRole.USER),),
                64,
                run,
                lease,
                "cmpop_uncertain_test",
                0,
                30,
                30,
                TEST_COMPRESSION_PROMPT,
            )
        )
    assert provider.complete.await_count == 1
    assert repository.record_usage.await_count == 1


@pytest.mark.parametrize("mode", ["initial", "failed_fallback", "oversized_refresh"])
def test_prepare_commits_summary_before_trigger_and_preserves_trigger(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """首次压缩只覆盖触发消息之前的历史，并以同一操作保存最终摘要。"""
    now = datetime.now(UTC)
    base = Run(
        id="run_prepare_test",
        session_id="ses_prepare_test",
        owner_id="usr_prepare_test",
        status=RunStatus.RUNNING,
        trigger_message_id="msg_4",
        context_cutoff_sequence=4,
        retry_of_run_id=None,
        model_config_version_id="profile_main",
        main_prompt_version_id=TEST_MAIN_PROMPT_ID,
        compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_prepare_test",
        lease_expires_at=now + timedelta(minutes=1),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(minutes=1),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )
    lease = RunLease(base, now + timedelta(minutes=1), now + timedelta(minutes=1))
    main = RuntimeModelProfile(
        version=ModelProfileVersion(
            id="profile_main",
            name="main",
            protocol="chat_completions",
            base_url="https://llm.example.test/v1",
            model="main-model",
            has_api_key=True,
            context_window_tokens=1000,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            max_output_tokens=100,
            request_timeout_seconds=10,
            execution_timeout_seconds=30,
            max_concurrency=1,
            temperature=None,
            enabled=True,
            created_at=now,
        ),
        api_key="test",
    )
    compression_runtime = RuntimeModelProfile(
        version=ModelProfileVersion(
            id="profile_compression",
            name="compression",
            protocol="chat_completions",
            base_url="https://llm.example.test/v1",
            model="summary-model",
            has_api_key=True,
            context_window_tokens=600,
            tokenizer_encoding="cl100k_base",
            supports_stream=False,
            supports_tools=False,
            max_output_tokens=80,
            request_timeout_seconds=10,
            execution_timeout_seconds=30,
            max_concurrency=1,
            temperature=None,
            enabled=True,
            created_at=now,
        ),
        api_key="test",
    )
    policy = CompressionPolicyVersion(
        id="policy_prepare_test",
        compression_model_profile_id="profile_compression",
        trigger_ratio=0.8,
        recent_ratio=0.2,
        summary_ratio=0.1,
        prompt_template_version="context-summary-v1",
        created_at=now,
    )
    messages = tuple(
        _message(i, ConversationRole.USER if i % 2 else ConversationRole.ASSISTANT)
        for i in range(1, 5)
    )
    saved: dict[str, object] = {}
    events: list[RunEventPayload] = []
    built_contexts: list[tuple[tuple[str, ...], object]] = []
    compression_requests: list[LlmRequest] = []
    provider_fails = mode == "failed_fallback"

    class Repository:
        async def list_context_summaries(self, *_: str) -> tuple[ContextSummary, ...]:
            if mode == "initial":
                return ()
            return (
                ContextSummary(
                    id="summary_previous",
                    session_id=base.session_id,
                    owner_id=base.owner_id,
                    covered_through_sequence=(3 if mode == "oversized_refresh" else 2),
                    parent_summary_id=None,
                    text=(
                        "oversized previous summary"
                        if mode == "oversized_refresh"
                        else "previous compatible summary"
                    ),
                    run_id="run_previous",
                    execution_epoch=1,
                    operation_id="cmpop_previous",
                    policy_version_id=policy.id,
                    compression_model_version_id=compression_runtime.version.id,
                    prompt_version=policy.prompt_template_version,
                    compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
                    created_at=now,
                ),
            )

        async def append_compression_event(
            self, run_id: str, worker_id: str, execution_epoch: int, payload: RunEventPayload
        ) -> object:
            events.append(payload)
            return SimpleNamespace(type=payload.type, payload=payload)

        async def is_live_execution(self, *_: object) -> bool:
            return True

        async def record_usage(self, *_: object) -> object:
            return base

        async def save_context_summary(self, command: object) -> tuple[ContextSummary, object]:
            saved["command"] = command
            return (
                ContextSummary(
                    id="summary_prepare_test",
                    session_id=base.session_id,
                    owner_id=base.owner_id,
                    covered_through_sequence=3,
                    parent_summary_id=None,
                    text="summary text",
                    run_id=base.id,
                    execution_epoch=base.execution_epoch,
                    operation_id="cmpop_prepare_test",
                    policy_version_id=policy.id,
                    compression_model_version_id=compression_runtime.version.id,
                    prompt_version=policy.prompt_template_version,
                    compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
                    created_at=now,
                ),
                SimpleNamespace(type="compression_completed"),
            )

    class Provider:
        async def complete(
            self,
            _runtime: RuntimeModelProfile,
            request: LlmRequest,
        ) -> LlmResponse:
            compression_requests.append(request)
            if provider_fails:
                raise LlmProviderError("llm_authentication_failed")
            return LlmResponse(
                output=(LlmText("summary text"),),
                status="completed",
                incomplete_reason=None,
                model="summary-model",
                response_id="response-1",
                request_id=None,
                usage=LlmUsage(input_tokens=10, output_tokens=2, total_tokens=12),
            )

    async def fake_estimate(*args: object, **__: object) -> tuple[int, int]:
        messages = cast(Sequence[object], args[1])
        return 850 + 500 * len(messages), 100

    async def fake_count(text: str, *_: object, **__: object) -> int:
        if text in {"summary text", "previous compatible summary"}:
            return 2
        if text == "oversized previous summary":
            return 200
        if text.count("sequence") >= 3:
            return 600
        return 200 if "sequence" in text else 10

    async def fake_build(
        run: Run,
        active: Sequence[ConversationMessage | ExecutionMessage],
        profile: ModelProfileVersion,
        **kwargs: object,
    ) -> tuple[tuple[()], tuple[()], int]:
        assert any(item.id == base.trigger_message_id for item in active)
        built_contexts.append((tuple(item.id for item in active), kwargs.get("summary_text")))
        return (), (), 100

    async def scenario() -> None:
        monkeypatch.setattr(compression_module, "estimate_llm_input_tokens", fake_estimate)
        monkeypatch.setattr(compression_module, "count_plain_text_tokens_async", fake_count)
        monkeypatch.setattr(compression_module, "build_llm_request", fake_build)
        compressor = ContextCompressor(
            cast(RunRepository, Repository()),
            SimpleNamespace(get_runtime=AsyncMock(return_value=compression_runtime)),
            Provider(),
            SimpleNamespace(publish_persisted=AsyncMock()),
        )
        result = await compressor.prepare(
            lease,
            messages,
            main,
            policy,
            main_prompt=TEST_MAIN_PROMPT,
            compression_prompt=TEST_COMPRESSION_PROMPT,
            timeout_seconds=30,
        )
        assert result[3] is not None

    asyncio.run(scenario())
    assert compression_requests
    assert {
        request.messages[0].content for request in compression_requests
    } == {TEST_COMPRESSION_PROMPT}
    if provider_fails:
        assert "command" not in saved
        assert len(events) == 2
        started, failed = events
        assert isinstance(started, RunCompressionStartedPayload)
        assert isinstance(failed, RunCompressionFailedPayload)
        assert started.operation_id == failed.operation_id
        assert failed.batch_count == 1
        assert built_contexts == [(("msg_3", "msg_4"), "previous compatible summary")]
    elif mode == "oversized_refresh":
        command = saved["command"]
        assert command.covered_through_sequence == 3  # type: ignore[attr-defined]
        assert command.parent_summary_id == "summary_previous"  # type: ignore[attr-defined]
        assert command.batch_count == 1  # type: ignore[attr-defined]
        assert built_contexts == [(("msg_4",), "summary text")]
    else:
        command = saved["command"]
        assert command.covered_through_sequence == 3  # type: ignore[attr-defined]
        assert command.parent_summary_id is None  # type: ignore[attr-defined]
        assert command.batch_count == 2  # type: ignore[attr-defined]
