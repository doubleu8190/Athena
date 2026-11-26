"""企业场景多轮上下文压缩、澄清轮次组边界保护与项目/偏好/记忆三层上下文叠加测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock

import tiktoken
import yaml  # type: ignore[import-untyped]

from app.preference.context import render_preference_context
from app.run.compression import ContextCompressor, _split_recent_suffix, _turn_groups
from app.run.context import LlmRequestSpec, measure_llm_request
from app.run.prompt import render_project_instructions_context
from domain.llm.model import (
    LlmProviderError,
    LlmRequest,
    LlmResponse,
    LlmRole,
    LlmText,
    LlmUsage,
)
from domain.llm.model.compression_policy import CompressionPolicyVersion
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile
from domain.llm.ports import LlmRuntimeTextProvider
from domain.message import (
    ConversationMessage,
    ConversationRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
)
from domain.preference import PreferenceContext, UserMemory, UserMemorySource
from domain.run.model import (
    ContextStopReason,
    ContextSummary,
    Run,
    RunCompressionStartedPayload,
    RunEvent,
    RunLease,
    RunModelSnapshot,
    RunStatus,
    RunUsage,
)
from domain.run.ports import RunRepository

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_ROOT = REPO_ROOT / "datasets" / "enterprise_hroa"
DIALOGUES_PATH = DATASET_ROOT / "sources" / "compression" / "multi_turn_enterprise_dialogues.json"
MANIFEST_PATH = DATASET_ROOT / "manifest.yaml"

TEST_MAIN_PROMPT = "你是星辰智联企业办公智能助手 Athena，请严格遵循制度编号、审批与数据边界。"
TEST_COMPRESSION_PROMPT = (
    "请将历史办公对话压缩为简洁中文事实摘要，务必保留制度编号、员工工号、合同号、差旅单号、"
    "备用金单号与精确人民币金额。"
)


def _load_dialogues() -> dict[str, Any]:
    """加载企业多轮上下文压缩测试夹具。"""
    return cast(dict[str, Any], json.loads(DIALOGUES_PATH.read_text(encoding="utf-8")))


def _build_conversation_messages(
    raw_messages: list[dict[str, Any]],
    *,
    session_id: str,
) -> tuple[ConversationMessage, ...]:
    """将 JSON 夹具中的轮次消息转换为领域 ConversationMessage 序列。"""
    base_time = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
    result: list[ConversationMessage] = []
    for item in raw_messages:
        seq = int(item["sequence"])
        role = (
            ConversationRole.USER
            if str(item["role"]).lower() == "user"
            else ConversationRole.ASSISTANT
        )
        result.append(
            ConversationMessage(
                id=f"msg_{session_id}_{seq:02d}",
                session_id=session_id,
                role=role,
                status=MessageStatus.COMPLETED,
                sequence=seq,
                parts=(TextPart(str(item["text"])),),
                reply_to_message_id=None,
                run_id=str(item["run_id"]),
                client_message_id=None,
                created_at=base_time + timedelta(minutes=seq),
                completed_at=base_time + timedelta(minutes=seq),
                metadata=MessageMetadata(),
                context_turn_id=str(item["context_turn_id"]),
            )
        )
    return tuple(result)


def _make_runtime_profile(
    profile_id: str,
    *,
    window: int,
    max_output: int,
    encoding: Literal["cl100k_base", "o200k_base"] = "o200k_base",
) -> RuntimeModelProfile:
    """构造测试用运行时模型配置档案。"""
    now = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    return RuntimeModelProfile(
        version=ModelProfileVersion(
            id=profile_id,
            name=profile_id,
            protocol="chat_completions",
            base_url="https://llm.enterprise.test/v1",
            model=profile_id,
            context_window_tokens=window,
            tokenizer_encoding=encoding,
            supports_stream=True,
            supports_tools=True,
            max_output_tokens=max_output,
            request_timeout_seconds=15,
            execution_timeout_seconds=60,
            max_concurrency=2,
            temperature=None,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        api_key="test-key",
    )


def _make_run(
    *,
    run_id: str,
    session_id: str,
    owner_id: str,
    trigger_message_id: str,
    cutoff_sequence: int,
    main_profile_id: str,
) -> tuple[Run, RunLease]:
    """构造处于运行态且携带有效租约的测试 Run 与 RunLease。"""
    now = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    run = Run(
        id=run_id,
        session_id=session_id,
        owner_id=owner_id,
        status=RunStatus.RUNNING,
        trigger_message_id=trigger_message_id,
        context_cutoff_sequence=cutoff_sequence,
        retry_of_run_id=None,
        model_config_version_id=main_profile_id,
        model_snapshot=RunModelSnapshot(
            id=main_profile_id,
            name=main_profile_id,
            protocol="chat_completions",
            base_url="https://llm.enterprise.test/v1",
            model=main_profile_id,
            context_window_tokens=16384,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=False,
            max_output_tokens=512,
            request_timeout_seconds=15,
            execution_timeout_seconds=60,
            max_concurrency=2,
        ),
        main_prompt_version_id="prompt_main_v2",
        compression_prompt_version_id="prompt_cmp_v2",
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_ent_cmp",
        lease_expires_at=now + timedelta(minutes=2),
        execution_started_at=now,
        execution_deadline_at=now + timedelta(minutes=2),
        queued_at=now,
        first_started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )
    lease = RunLease(run, now + timedelta(minutes=2), now + timedelta(minutes=2))
    return run, lease


class _FakeCompressionRunRepository:
    """为企业场景压缩测试提供事件与摘要持久化的轻量内存仓储。"""

    def __init__(self, run: Run, summary_text: str) -> None:
        self._run = run
        self._summary_text = summary_text
        self.events: list[RunEvent] = []
        self.saved_commands: list[object] = []

    async def list_context_summaries(self, *_: str) -> tuple[ContextSummary, ...]:
        return ()

    async def list_events(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
        through_sequence: int | None = None,
    ) -> tuple[RunEvent, ...]:
        del session_id, owner_id, run_id, through_sequence
        return tuple(e for e in self.events if e.sequence > after_sequence)[:limit]

    async def append_compression_event(
        self,
        run_id: str,
        worker_id: str,
        execution_epoch: int,
        payload: Any,
    ) -> RunEvent:
        del run_id, worker_id, execution_epoch
        event = RunEvent(
            id=f"ev_cmp_{len(self.events) + 1}",
            run_id=self._run.id,
            sequence=len(self.events) + 2,
            type="compression_event",
            created_at=datetime(2026, 11, 24, 10, 1, tzinfo=UTC),
            payload=payload,
        )
        self.events.append(event)
        return event

    async def is_live_execution(self, *_: object) -> bool:
        return True

    async def record_usage(self, *_: object) -> Run:
        return self._run

    async def save_context_summary(self, command: Any) -> tuple[ContextSummary, RunEvent]:
        self.saved_commands.append(command)
        completed_event = RunEvent(
            id="ev_cmp_completed",
            run_id=self._run.id,
            sequence=len(self.events) + 2,
            type="compression_completed",
            created_at=datetime(2026, 11, 24, 10, 1, tzinfo=UTC),
            payload=RunCompressionStartedPayload(operation_id=command.operation_id),
        )
        self.events.append(completed_event)
        summary = ContextSummary(
            id=command.summary_id,
            session_id=self._run.session_id,
            owner_id=self._run.owner_id,
            covered_through_sequence=command.covered_through_sequence,
            parent_summary_id=command.parent_summary_id,
            text=command.text,
            run_id=self._run.id,
            execution_epoch=self._run.execution_epoch,
            operation_id=command.operation_id,
            policy_version_id=command.policy_version_id,
            compression_model_version_id=command.compression_model_version_id,
            compression_prompt_version_id="prompt_cmp_v2",
            created_at=command.created_at,
        )
        return summary, completed_event


def test_12turn_enterprise_dialogue_triggers_compression_with_layered_project_and_preference() -> (
    None
):
    """12 轮长对话 >=4500 tokens 触发压缩，保留核心事实并叠加项目/偏好/显式记忆三层上下文。"""
    dialogues = _load_dialogues()
    scenario_12 = dialogues["scenarios"]["enterprise_12turn_cross_domain"]
    messages = _build_conversation_messages(scenario_12["messages"], session_id="ses_ent_12")
    assert len(messages) == 23

    # 1. 核验长对话语料在双 tokenizer 下均 >= 4,500 tokens。
    joined_text = "\n".join(str(item["text"]) for item in scenario_12["messages"])
    for enc_name in ("o200k_base", "cl100k_base"):
        token_len = len(tiktoken.get_encoding(enc_name).encode_ordinary(joined_text))
        assert token_len >= 4500, f"12turn_fixture_too_short:{enc_name}:{token_len}"

    # 2. 基于真实 manifest.yaml 构造三层叠加：项目背景指令 + 个人工作偏好 + 显式记忆。
    manifest = cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))
    proj2 = next(p for p in manifest["projects"] if p["code"] == "PROJ-2")
    fin_pref_spec = next(
        r for r in manifest["preferences_and_memories"] if r["user_id"] == "mock_fin_admin"
    )
    base_time = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
    memories = tuple(
        UserMemory(
            id=f"umem_fin_{idx}",
            user_id="mock_fin_admin",
            content=str(text),
            source=UserMemorySource.MANUAL,
            created_at=base_time + timedelta(minutes=idx),
            updated_at=base_time + timedelta(minutes=idx),
        )
        for idx, text in enumerate(fin_pref_spec["manual_memories"])
    )
    project_context = render_project_instructions_context(
        str(proj2["name"]), str(proj2["instructions"])
    )
    preference_context = render_preference_context(
        PreferenceContext(
            custom_instructions=str(fin_pref_spec["custom_instructions"]),
            memory_enabled=True,
            memories=tuple(reversed(memories)),
        )
    )
    assert project_context is not None and preference_context is not None

    # 3. 构造压缩策略与高保真浓缩摘要（保留方案要求的全部关键锚点且严守预算）。
    main_runtime = _make_runtime_profile("main_ent_12", window=16384, max_output=512)
    cmp_runtime = _make_runtime_profile("cmp_ent_12", window=16384, max_output=1024)
    policy = CompressionPolicyVersion(
        id="policy_ent_12",
        compression_model_profile_id=cmp_runtime.version.id,
        trigger_tokens=4200,
        recent_ratio=0.25,
        recent_token_cap=1200,
        summary_ratio=0.15,
        summary_token_cap=600,
        created_at=base_time,
    )
    compact_summary = (
        "摘要：1)绩效XC-HR-2026-007：XC1008方若琳C触发60 天 PIP(申诉截至2026-12-02)，"
        "XC1004郑语桐A平调，XC1007唐逸凡S晋升；"
        "2)合同XC-LEG-2026-008：CTR-2026-1120金额360,000.00元(LEG-REV-2026-0418)待林清岚会签；"
        "3)差旅XC-FIN-TRV-021：TRV-2026-1102超540.00、TRV-2026-1103超300.00、"
        "TRV-2026-1104公务舱违规；"
        "4)离职XC1003韩景明阻断：IT-MBP-2023-088未还、FIN-ADV-2026-0914未清2,400.00元。"
    )

    run, lease = _make_run(
        run_id="run_ent_12_trigger",
        session_id="ses_ent_12",
        owner_id="mock_fin_admin",
        trigger_message_id=messages[-1].id,
        cutoff_sequence=messages[-1].sequence,
        main_profile_id=main_runtime.version.id,
    )
    repo = _FakeCompressionRunRepository(run, compact_summary)
    captured_requests: list[LlmRequest] = []

    class _SummaryProvider:
        async def complete(self, _runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
            captured_requests.append(request)
            return LlmResponse(
                output=(LlmText(compact_summary),),
                status="completed",
                incomplete_reason=None,
                model="cmp-enterprise-model",
                response_id="resp_ent_12",
                request_id=None,
                usage=LlmUsage(input_tokens=4800, output_tokens=160, total_tokens=4960),
            )

    async def scenario() -> None:
        spec = LlmRequestSpec(
            run=run,
            messages=messages,
            main_prompt=TEST_MAIN_PROMPT,
            preference_context=preference_context,
            project_context=project_context,
        )
        _, _, uncompressed_tokens, _ = await measure_llm_request(
            spec,
            main_runtime.version,
            timeout_seconds=10,
        )
        assert uncompressed_tokens > policy.trigger_tokens

        compressor = ContextCompressor(
            cast(RunRepository, repo),
            SimpleNamespace(get_runtime=AsyncMock(return_value=cmp_runtime)),
            cast(LlmRuntimeTextProvider, _SummaryProvider()),
            SimpleNamespace(publish_persisted=AsyncMock()),
        )
        prepared = await compressor.prepare(
            lease,
            spec,
            main_runtime,
            policy,
            compression_prompt=TEST_COMPRESSION_PROMPT,
            timeout_seconds=10,
        )

        # 验证压缩真实触发、仅派发 1 次，且主请求输入 Token 显著下降。
        assert prepared.compression_called is True
        assert prepared.compression_completed is True
        assert len(captured_requests) == 1
        assert len(repo.saved_commands) == 1
        assert prepared.input_tokens < uncompressed_tokens

        # 验证项目指引、个人偏好与显式记忆均以 USER 角色叠加注入且不污染 SYSTEM。
        user_contents = [msg.content for msg in prepared.messages if msg.role is LlmRole.USER]
        system_contents = [msg.content for msg in prepared.messages if msg.role is LlmRole.SYSTEM]
        assert any(
            "<project_instructions>" in text and "PROJ-2" not in text for text in user_contents
        )
        assert any("2026年11月差旅稽核与用印风控专项" in text for text in user_contents)
        assert any("<user_work_profile>" in text and "林清岚" in text for text in user_contents)
        assert any("<user_explicit_memories>" in text and "21.75" in text for text in user_contents)
        assert all("<project_instructions>" not in text for text in system_contents)
        assert all("<user_explicit_memories>" not in text for text in system_contents)

        # 验证压缩摘要注入为历史资料且保留夹具声明的全部核心业务事实锚点。
        summary_msg = next(
            msg
            for msg in prepared.messages
            if msg.role is LlmRole.USER and "压缩摘要" in msg.content
        )
        required_facts = cast(list[str], scenario_12["required_facts_to_preserve"])
        assert required_facts
        for fact in required_facts:
            assert fact in summary_msg.content, f"summary_lost_fact:{fact}"

    asyncio.run(scenario())


def test_8turn_clarification_dialogue_preserves_atomic_turn_groups_during_split() -> None:
    """8 轮含相邻澄清问答组对话经 _turn_groups 与 _split_recent_suffix 切分时绝不拆断同一澄清组。"""
    dialogues = _load_dialogues()
    scenario_8 = dialogues["scenarios"]["enterprise_8turn_with_clarification"]
    messages = _build_conversation_messages(scenario_8["messages"], session_id="ses_ent_8")
    prefix = messages[:-1]  # 去除最终触发消息（sequence=19）

    groups = _turn_groups(prefix)
    grouped_sequences = [[msg.sequence for msg in group] for group in groups]
    assert grouped_sequences == [
        [1, 2],
        [3, 4],
        [5, 6, 7, 8],  # 澄清组 1：turn_clarify_trv_1102 必须保持原子完整
        [9, 10],
        [11, 12],
        [13, 14, 15, 16],  # 澄清组 2：turn_clarify_ctr_1120 必须保持原子完整
        [17, 18],
    ]

    # 验证在各种近期预算阈值下，_split_recent_suffix 绝不会把 [5,6,7,8] 或 [13,14,15,16] 拆成半截。
    main_runtime = _make_runtime_profile("main_ent_8", window=8192, max_output=512)
    run, _ = _make_run(
        run_id="run_ent_8_split",
        session_id="ses_ent_8",
        owner_id="mock_fin_admin",
        trigger_message_id=messages[-1].id,
        cutoff_sequence=messages[-1].sequence,
        main_profile_id=main_runtime.version.id,
    )

    async def scenario() -> None:
        for recent_limit in (200, 450, 750, 1100):
            remaining_groups, recent_messages = await _split_recent_suffix(
                list(groups),
                main_runtime.version,
                recent_limit,
                timeout_seconds=10,
            )
            recent_seqs = {msg.sequence for msg in recent_messages}
            remaining_seqs = {msg.sequence for group in remaining_groups for msg in group}
            assert recent_seqs.isdisjoint(remaining_seqs)
            assert recent_seqs | remaining_seqs == set(range(1, 19))
            for atomic_group in ({5, 6, 7, 8}, {13, 14, 15, 16}):
                assert atomic_group <= recent_seqs or atomic_group <= remaining_seqs, (
                    f"clarification_group_split limit={recent_limit} group={atomic_group}"
                )

    asyncio.run(scenario())


def test_compression_provider_failure_gracefully_falls_back_without_crashing() -> None:
    """压缩模型上游失败时优雅回退原始上下文、记录单次调用失败事件且返回安全停止提示。"""
    dialogues = _load_dialogues()
    scenario_12 = dialogues["scenarios"]["enterprise_12turn_cross_domain"]
    messages = _build_conversation_messages(scenario_12["messages"], session_id="ses_ent_fail")
    now = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)

    main_runtime = _make_runtime_profile("main_ent_fail", window=16384, max_output=512)
    cmp_runtime = _make_runtime_profile("cmp_ent_fail", window=16384, max_output=1024)
    policy = CompressionPolicyVersion(
        id="policy_ent_fail",
        compression_model_profile_id=cmp_runtime.version.id,
        trigger_tokens=4000,
        recent_ratio=0.25,
        recent_token_cap=1000,
        summary_ratio=0.15,
        summary_token_cap=500,
        created_at=now,
    )
    run, lease = _make_run(
        run_id="run_ent_fail",
        session_id="ses_ent_fail",
        owner_id="mock_super_admin",
        trigger_message_id=messages[-1].id,
        cutoff_sequence=messages[-1].sequence,
        main_profile_id=main_runtime.version.id,
    )
    repo = _FakeCompressionRunRepository(run, "")

    class _FailingProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, *_: object) -> LlmResponse:
            self.calls += 1
            raise LlmProviderError(
                "llm_temporary_upstream_error",
                retry_after_seconds=0,
                retry_safe=True,
            )

    provider = _FailingProvider()

    async def scenario() -> None:
        compressor = ContextCompressor(
            cast(RunRepository, repo),
            SimpleNamespace(get_runtime=AsyncMock(return_value=cmp_runtime)),
            cast(LlmRuntimeTextProvider, provider),
            SimpleNamespace(publish_persisted=AsyncMock()),
        )
        prepared = await compressor.prepare(
            lease,
            LlmRequestSpec(run=run, messages=messages, main_prompt=TEST_MAIN_PROMPT),
            main_runtime,
            policy,
            compression_prompt=TEST_COMPRESSION_PROMPT,
            timeout_seconds=10,
        )
        # 压缩模型虽失败，但严格只派发一次、不崩塌、优雅回退原始全部历史并给出安全错误提示。
        assert provider.calls == 1
        assert prepared.compression_called is True
        assert prepared.compression_completed is False
        assert prepared.stop_reason_hint is ContextStopReason.COMPRESSION_FAILED
        assert prepared.event is not None
        assert repo.saved_commands == []
        # 原始对话第一条与最后一条消息均完好保留在回退上下文中。
        all_user_text = "\n".join(m.content for m in prepared.messages if m.role is LlmRole.USER)
        assert "XC-HR-2026-007" in all_user_text
        assert "15:00 高管例会" in all_user_text

    asyncio.run(scenario())
