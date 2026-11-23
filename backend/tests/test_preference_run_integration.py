"""个人工作偏好 / 显式记忆与 RunExecutor、RunToolRuntimeService 集成单元测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import tiktoken
from sqlalchemy import DateTime, func, literal
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool
from system_prompt_support import (
    TEST_COMPRESSION_PROMPT_ID,
    TEST_MAIN_PROMPT_ID,
    FakeSystemPromptRepository,
)

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.llm.model import ModelProfileVersionModel
from adapter.llm.model.profile import DefaultModelProfileModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.models import Base
from adapter.preference import SqlAlchemyPreferenceRepository
from adapter.run.repository.run import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.subagent.model import SubagentModel
from adapter.system_prompt.model import SystemPromptSettingModel, SystemPromptVersionModel
from app.preference import PreferenceService, render_preference_context
from app.run.config import RunWorkerConfig
from app.run.executor import RunExecutor
from app.tool.runtime import (
    REQUEST_USER_INPUT_ID,
    SAVE_USER_MEMORY_DEFINITION,
    RunToolRuntimeService,
)
from domain.auth.model import User, UserRole, UserStatus
from domain.llm.model import (
    LlmRequest,
    LlmResponse,
    LlmRole,
    LlmText,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.mcp import McpCallResult, McpCallStatus, McpSecretPurpose
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolCallPart,
)
from domain.preference import (
    MAX_USER_MEMORIES_PER_USER,
    SAVE_USER_MEMORY_TOOL_ID,
    PreferenceContext,
    UserMemory,
    UserMemorySource,
)
from domain.run.model import (
    BeginSubagentBatchCommand,
    BeginSubagentBatchItem,
    RunLease,
    RunStatus,
    SubmitRunCommand,
)
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID
from domain.tool import (
    AuthorizedRunContext,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    ToolExecutionContext,
    ToolOutcomeStatus,
)
from infrastructure.security.mcp_secrets import McpSecretCipher

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


class _StubAuth:
    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        del session_id, now
        return User(
            id=user_id,
            email="owner@example.com",
            password_hash="hash",
            display_name="Owner",
            role=UserRole.USER,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )


class _StubSessionRepo:
    async def get_visible(self, scope: Any) -> Any:
        @dataclass
        class _Visible:
            project_id: str | None = None

        del scope
        return _Visible()


class _StubMcpCatalog:
    async def list_runtime_tools(self, *, limit: int = 200) -> tuple[RuntimeToolDefinition, ...]:
        del limit
        return ()

    async def get_call_target(
        self,
        tool_id: str,
        server_id: str,
        remote_name: str,
        config_version: int,
    ) -> Any:
        del tool_id, server_id, remote_name, config_version
        return None


class _StubMcpExecutor:
    async def call(
        self,
        target: Any,
        arguments: dict[str, Any],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        del target, arguments, deadline_seconds
        return McpCallResult(McpCallStatus.OK, "ok")


class _FakeProvider:
    """记录每轮模型请求并按序回放预置响应的假 LLM 提供商。"""

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[LlmRequest] = []

    async def complete(
        self,
        profile: RuntimeModelProfile,
        request: LlmRequest,
    ) -> LlmResponse:
        del profile
        self.requests.append(request)
        return self._responses.pop(0)

    @asynccontextmanager
    async def stream(
        self,
        profile: RuntimeModelProfile,
        request: LlmRequest,
    ) -> AsyncIterator[Any]:
        del profile
        self.requests.append(request)
        response = self._responses.pop(0)

        async def _iter() -> AsyncIterator[Any]:
            yield ResponseCompleted(response=response)

        yield _iter()


class _MutableClock:
    def __init__(self, initial: datetime = NOW) -> None:
        self.value = initial

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int = 1) -> None:
        self.value += timedelta(seconds=seconds)


def _llm_response(
    *output: LlmText | LlmToolCall,
    input_tokens: int = 80,
    output_tokens: int = 20,
) -> LlmResponse:
    return LlmResponse(
        output=output,
        status="completed",
        incomplete_reason=None,
        model="gpt-4o",
        response_id="resp_1",
        request_id="req_1",
        usage=LlmUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def _incrementing_ids() -> Any:
    counter = 0

    def factory(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}_{counter:04d}"

    return factory


async def _setup_env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    SqlAlchemyRunRepository,
    SqlAlchemyModelProfileRepository,
    PreferenceService,
    _MutableClock,
]:
    """初始化包含用户、会话、模型配置、系统提示词与偏好服务的内存 SQLite 测试环境。"""
    clock = _MutableClock(NOW)
    monkeypatch.setattr(
        func,
        "clock_timestamp",
        lambda: literal(NOW, type_=DateTime(timezone=True)),
    )
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    cipher = McpSecretCipher(b"0" * 32, key_version=1)
    encrypted = cipher.seal("mp_1", McpSecretPurpose.LLM_API_KEY, {"api_key": "sk-test-key"})

    async with factory() as session, session.begin():
        session.add_all(
            [
                UserModel(
                    id="usr_1",
                    email="owner@example.com",
                    password_hash="hash",
                    display_name="Owner",
                    role=UserRole.USER.value,
                    status=UserStatus.ACTIVE.value,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                AuthSessionModel(
                    id="auth_1",
                    user_id="usr_1",
                    token_hash="tok_hash",
                    expires_at=NOW + timedelta(days=7),
                    created_at=NOW,
                ),
                SessionModel(
                    id="sess_1",
                    owner_id="usr_1",
                    title="Test Session",
                    last_message_sequence=0,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                SystemPromptVersionModel(
                    id=TEST_MAIN_PROMPT_ID,
                    kind="main",
                    content="Main system prompt",
                    created_at=NOW,
                ),
                SystemPromptVersionModel(
                    id=TEST_COMPRESSION_PROMPT_ID,
                    kind="compression",
                    content="Compression prompt",
                    created_at=NOW,
                ),
                SystemPromptSettingModel(
                    kind="main",
                    current_version_id=TEST_MAIN_PROMPT_ID,
                ),
                SystemPromptSettingModel(
                    kind="compression",
                    current_version_id=TEST_COMPRESSION_PROMPT_ID,
                ),
                ModelProfileVersionModel(
                    id="mp_1",
                    name="Default GPT",
                    protocol="chat_completions",
                    base_url="https://api.example.com/v1",
                    model="gpt-4o",
                    context_window_tokens=128000,
                    max_output_tokens=4096,
                    tokenizer_encoding="cl100k_base",
                    supports_stream=True,
                    supports_tools=True,
                    supports_parallel_tool_calls=True,
                    request_timeout_seconds=60,
                    execution_timeout_seconds=300,
                    max_concurrency=4,
                    enabled=True,
                    api_key_ciphertext=encrypted.ciphertext,
                    api_key_nonce=encrypted.nonce,
                    api_key_key_version=encrypted.key_version,
                    created_at=NOW,
                ),
                SubagentModel(
                    id="sub_prof_1",
                    slug="researcher",
                    name="研究员",
                    description="负责深度检索",
                    system_prompt="你是研究员子智能体",
                    model_profile_id="mp_1",
                    allowed_tool_ids=[REQUEST_USER_INPUT_ID],
                    enabled=True,
                    max_llm_requests=10,
                    timeout_seconds=120,
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.add(DefaultModelProfileModel(singleton_id=1, profile_id="mp_1"))

    run_repo = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
    profile_repo = SqlAlchemyModelProfileRepository(factory, cipher)
    pref_service = PreferenceService(
        SqlAlchemyPreferenceRepository(factory),
        id_factory=_incrementing_ids(),
        clock=clock,
    )
    return engine, factory, run_repo, profile_repo, pref_service, clock


def _make_runtime(preferences: PreferenceService | None) -> RunToolRuntimeService:
    return RunToolRuntimeService(
        auth=_StubAuth(),
        sessions=cast(Any, _StubSessionRepo()),
        mcp_catalog=_StubMcpCatalog(),
        mcp_executor=_StubMcpExecutor(),
        knowledge=None,
        project_knowledge=None,
        preferences=preferences,
        clock=lambda: NOW,
    )


async def _submit_and_claim_main_run(
    run_repo: SqlAlchemyRunRepository,
    run_id: str,
) -> RunLease:
    await run_repo.submit(
        "sess_1",
        "usr_1",
        SubmitRunCommand(
            run_id=run_id,
            request_id=f"req_{run_id}",
            fingerprint=f"fp_{run_id}",
            prompt_template_version="run-v2",
            message=ConversationMessageDraft(
                id=f"msg_{run_id}",
                session_id="sess_1",
                owner_id="usr_1",
                role=ConversationRole.USER,
                status=MessageStatus.COMPLETED,
                parts=(TextPart("请按我的偏好给出方案"),),
                reply_to_message_id=None,
                run_id=run_id,
                client_message_id=None,
                created_at=NOW,
                completed_at=NOW,
                metadata=MessageMetadata(),
                context_turn_id=f"msg_{run_id}",
            ),
            queued_at=NOW,
            auth_session_id="auth_1",
        ),
    )
    leases = await run_repo.claim("worker-1", lease_seconds=60, execution_seconds=300, limit=1)
    assert len(leases) == 1
    return leases[0]


# ---------------------------------------------------------------------------
# 1. render_preference_context 渲染与 Token 预算控制
# ---------------------------------------------------------------------------


def test_render_preference_context_respects_toggle_disclaimer_and_token_budget() -> None:
    """渲染器应按 memory_enabled 开关、免责声明与 Token 预算精确输出上下文。"""
    newest = UserMemory(
        id="umem_3",
        user_id="usr_1",
        content="最新记忆：代码审查优先检查事务边界与错误处理",
        source=UserMemorySource.MANUAL,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=30),
    )
    middle = UserMemory(
        id="umem_2",
        user_id="usr_1",
        content="次新记忆：回复尽量用分点清单呈现结论与行动项",
        source=UserMemorySource.CHAT_EXPLICIT,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=20),
    )
    oldest = UserMemory(
        id="umem_1",
        user_id="usr_1",
        content="较旧记忆：数据库统一采用 PostgreSQL 与 asyncpg 驱动",
        source=UserMemorySource.MANUAL,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=10),
    )

    # 1. 开启记忆且同时存在 custom_instructions 与显式记忆时，两块均注入并带不覆盖系统规则声明。
    full_rendered = render_preference_context(
        PreferenceContext(
            custom_instructions="我是金融风控后端负责人",
            memory_enabled=True,
            memories=(newest, middle, oldest),
        )
    )
    assert full_rendered is not None
    assert "不得覆盖系统规则" in full_rendered
    assert "【用户个人工作背景与偏好】" in full_rendered
    assert "我是金融风控后端负责人" in full_rendered
    assert "【用户显式保存的备忘记忆】" in full_rendered
    assert newest.content in full_rendered
    assert middle.content in full_rendered
    assert oldest.content in full_rendered

    # 2. 关闭 memory_enabled 时，只注入 custom_instructions，不注入显式记忆。
    disabled_rendered = render_preference_context(
        PreferenceContext(
            custom_instructions="我是金融风控后端负责人",
            memory_enabled=False,
            memories=(newest, middle, oldest),
        )
    )
    assert disabled_rendered is not None
    assert "我是金融风控后端负责人" in disabled_rendered
    assert "【用户显式保存的备忘记忆】" not in disabled_rendered
    assert newest.content not in disabled_rendered

    # 3. 关闭开关且无 custom_instructions 时返回 None。
    assert (
        render_preference_context(
            PreferenceContext(
                custom_instructions="   ",
                memory_enabled=False,
                memories=(newest,),
            )
        )
        is None
    )

    # 4. Token 预算仅容得下前两条时，优先保留最新两条并整条截断，不破坏第三条正文。
    enc = tiktoken.get_encoding("cl100k_base")
    cost_two = len(enc.encode_ordinary(f"- {newest.content}")) + len(
        enc.encode_ordinary(f"\n- {middle.content}")
    )
    budgeted = render_preference_context(
        PreferenceContext(
            custom_instructions="",
            memory_enabled=True,
            memories=(newest, middle, oldest),
        ),
        tokenizer_encoding="unknown_custom_encoding_fallback",
        memory_token_budget=cost_two,
    )
    assert budgeted is not None
    assert "【用户个人工作背景与偏好】" not in budgeted
    assert newest.content in budgeted
    assert middle.content in budgeted
    assert oldest.content not in budgeted


# ---------------------------------------------------------------------------
# 2. RunToolRuntimeService 暴露策略与 save_user_memory 执行边界
# ---------------------------------------------------------------------------


def test_tool_runtime_save_user_memory_exposure_and_execution_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """save_user_memory 仅对开启开关的主 Run 免审批暴露，满配额/关闭开关返回结构化错误。"""

    async def scenario() -> None:
        engine, _, _, _, pref_service, clock = await _setup_env(monkeypatch)
        try:
            authorized = AuthorizedRunContext(
                owner_id="usr_1",
                chat_session_id="sess_1",
                project_id=None,
                auth_session_id="auth_1",
            )
            exec_ctx = ToolExecutionContext(
                owner_id="usr_1",
                chat_session_id="sess_1",
                project_id=None,
                run_id="run_1",
                execution_epoch=1,
                auth_session_id="auth_1",
                deadline_at=NOW + timedelta(seconds=60),
            )

            # 未配置 PreferenceService 时不暴露 save_user_memory。
            runtime_without_pref = _make_runtime(None)
            snap_no_pref = await runtime_without_pref.build_snapshot("run-v2", authorized, depth=0)
            assert "save_user_memory" not in {t.model_name for t in snap_no_pref.tools}

            # 配置 PreferenceService 且默认 memory_enabled=True 时，主 Run 暴露且无需审批。
            runtime = _make_runtime(pref_service)
            snap_enabled = await runtime.build_snapshot("run-v2", authorized, depth=0)
            save_tool = next(
                (t for t in snap_enabled.tools if t.tool_id == SAVE_USER_MEMORY_TOOL_ID),
                None,
            )
            assert save_tool is not None
            assert save_tool.model_name == "save_user_memory"
            assert save_tool.requires_approval is False

            # 子 Run (depth=1) 即使白名单显式传入 SAVE_USER_MEMORY_TOOL_ID 也绝不暴露。
            snap_subagent = await runtime.build_snapshot(
                "run-v2",
                authorized,
                depth=1,
                allowed_tool_ids=[REQUEST_USER_INPUT_ID, SAVE_USER_MEMORY_TOOL_ID],
            )
            assert SAVE_USER_MEMORY_TOOL_ID not in {t.tool_id for t in snap_subagent.tools}
            assert "save_user_memory" not in {t.model_name for t in snap_subagent.tools}

            # prepare_call 验证合法入参且免审批。
            prep = await runtime.prepare_call(
                SAVE_USER_MEMORY_DEFINITION,
                "save_user_memory",
                {"content": "以后 SQL 例子默认用 PostgreSQL 语法"},
                cast(Any, object()),
                authorized,
                call_id="call_save_1",
            )
            assert prep.outcome is None
            assert prep.requires_approval is False

            # 正常执行：落库 source=chat_explicit 并返回 OK 结构化结果。
            ok_res = await runtime.execute(
                SAVE_USER_MEMORY_DEFINITION,
                "save_user_memory",
                {"content": "  以后 SQL 例子默认用 PostgreSQL 语法  "},
                exec_ctx,
                deadline_seconds=5,
                run_remaining_seconds=30,
            )
            assert ok_res.outcome.status is ToolOutcomeStatus.OK
            assert ok_res.outcome.code == "user_memory_saved"
            assert ok_res.outcome.data["source"] == "chat_explicit"
            assert ok_res.outcome.data["content"] == "以后 SQL 例子默认用 PostgreSQL 语法"

            stored = await pref_service.list_memories("usr_1")
            assert stored.total == 1
            assert stored.items[0].source is UserMemorySource.CHAT_EXPLICIT

            # 非法控制字符与纯空白内容返回结构化 ERROR（不抛异常）。
            bad_ctrl = await runtime.execute(
                SAVE_USER_MEMORY_DEFINITION,
                "save_user_memory",
                {"content": "包含\x00空字节"},
                exec_ctx,
                deadline_seconds=5,
                run_remaining_seconds=30,
            )
            assert bad_ctrl.outcome.status is ToolOutcomeStatus.ERROR
            assert bad_ctrl.outcome.code == "user_memory_content_invalid"

            # 纯空白字符串在 Schema 校验阶段即拦截为 tool_arguments_invalid，不触发写入。
            bad_blank = await runtime.execute(
                SAVE_USER_MEMORY_DEFINITION,
                "save_user_memory",
                {"content": "   "},
                exec_ctx,
                deadline_seconds=5,
                run_remaining_seconds=30,
            )
            assert bad_blank.outcome.status is ToolOutcomeStatus.ERROR
            assert bad_blank.outcome.code == "tool_arguments_invalid"

            # 用户关闭 memory_enabled 开关后：新快照隐藏工具，已冻结工具执行返回 disabled。
            await pref_service.update_profile(
                "usr_1",
                custom_instructions="",
                memory_enabled=False,
            )
            snap_disabled = await runtime.build_snapshot("run-v2", authorized, depth=0)
            assert "save_user_memory" not in {t.model_name for t in snap_disabled.tools}

            disabled_res = await runtime.execute(
                SAVE_USER_MEMORY_DEFINITION,
                "save_user_memory",
                {"content": "开关关闭后尝试写入"},
                exec_ctx,
                deadline_seconds=5,
                run_remaining_seconds=30,
            )
            assert disabled_res.outcome.status is ToolOutcomeStatus.ERROR
            assert disabled_res.outcome.code == "user_memory_disabled"
            assert (await pref_service.list_memories("usr_1")).total == 1

            # 重新开启开关并填满 50 条后，再调用返回结构化 user_memory_limit_reached。
            await pref_service.update_profile(
                "usr_1",
                custom_instructions="",
                memory_enabled=True,
            )
            for idx in range(2, MAX_USER_MEMORIES_PER_USER + 1):
                clock.advance(seconds=1)
                await pref_service.create_memory("usr_1", f"预置记忆 {idx}")

            limit_res = await runtime.execute(
                SAVE_USER_MEMORY_DEFINITION,
                "save_user_memory",
                {"content": "第 51 条对话记忆"},
                exec_ctx,
                deadline_seconds=5,
                run_remaining_seconds=30,
            )
            assert limit_res.outcome.status is ToolOutcomeStatus.ERROR
            assert limit_res.outcome.code == "user_memory_limit_reached"
            assert (await pref_service.list_memories("usr_1")).total == MAX_USER_MEMORIES_PER_USER
        finally:
            await engine.dispose()

    _run(scenario())


# ---------------------------------------------------------------------------
# 3. RunExecutor 端到端上下文注入、开关/预算/子 Run 隔离与免审批工具调用
# ---------------------------------------------------------------------------


def test_run_executor_injects_preferences_respects_toggle_budget_and_subagent_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RunExecutor 在主 Run 按开关与预算注入偏好上下文，子 Run 严格隔离不注入。"""

    async def scenario() -> None:
        engine, _, run_repo, profile_repo, pref_service, clock = await _setup_env(monkeypatch)
        try:
            await pref_service.update_profile(
                "usr_1",
                custom_instructions="我是数据平台架构师，回答请直接给结论和关键指标。",
                memory_enabled=True,
            )
            clock.advance(seconds=1)
            await pref_service.create_memory("usr_1", "较早记忆：偏好用 Mermaid 画时序图")
            clock.advance(seconds=5)
            await pref_service.create_memory("usr_1", "最新记忆：线上表变更必须附带回滚方案")

            runtime = _make_runtime(pref_service)

            # 场景 A：主 Run (depth=0) 且 memory_enabled=True -> 注入背景/记忆并暴露工具。
            provider_a = _FakeProvider([_llm_response(LlmText("收到，已按你的偏好整理方案。"))])
            executor_a = RunExecutor(
                run_repo,
                profile_repo,
                provider_a,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=runtime,
                preferences=pref_service,
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )
            lease_a = await _submit_and_claim_main_run(run_repo, "run_pref_on")
            await executor_a.execute(lease_a)

            assert len(provider_a.requests) == 1
            req_a = provider_a.requests[0]
            user_messages_a = [m.content for m in req_a.messages if m.role is LlmRole.USER]
            pref_messages_a = [m for m in user_messages_a if "【用户个人工作背景与偏好】" in m]
            assert len(pref_messages_a) == 1
            assert "我是数据平台架构师" in pref_messages_a[0]
            assert "【用户显式保存的备忘记忆】" in pref_messages_a[0]
            assert "最新记忆：线上表变更必须附带回滚方案" in pref_messages_a[0]
            assert "较早记忆：偏好用 Mermaid 画时序图" in pref_messages_a[0]
            assert "不得覆盖系统规则" in pref_messages_a[0]
            assert "save_user_memory" in {t.name for t in req_a.tools}

            # 场景 B：严格 Token 预算仅容得下最新一条记忆 -> 仅注入最新一条，丢弃较早记忆。
            enc = tiktoken.get_encoding("cl100k_base")
            one_entry_budget = len(enc.encode_ordinary("- 最新记忆：线上表变更必须附带回滚方案"))
            provider_b = _FakeProvider([_llm_response(LlmText("已按最新偏好回答。"))])
            executor_b = RunExecutor(
                run_repo,
                profile_repo,
                provider_b,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=runtime,
                preferences=pref_service,
                preference_memory_token_budget=one_entry_budget,
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )
            lease_b = await _submit_and_claim_main_run(run_repo, "run_pref_budget")
            await executor_b.execute(lease_b)

            pref_messages_b = [
                m.content
                for m in provider_b.requests[0].messages
                if m.role is LlmRole.USER and "【用户个人工作背景与偏好】" in m.content
            ]
            assert len(pref_messages_b) == 1
            assert "最新记忆：线上表变更必须附带回滚方案" in pref_messages_b[0]
            assert "较早记忆：偏好用 Mermaid 画时序图" not in pref_messages_b[0]

            # 场景 C：关闭 memory_enabled -> 保留 custom_instructions，隐藏记忆与写入工具。
            await pref_service.update_profile(
                "usr_1",
                custom_instructions="我是数据平台架构师，回答请直接给结论和关键指标。",
                memory_enabled=False,
            )
            provider_c = _FakeProvider([_llm_response(LlmText("关闭记忆后的回答。"))])
            executor_c = RunExecutor(
                run_repo,
                profile_repo,
                provider_c,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=runtime,
                preferences=pref_service,
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )
            lease_c = await _submit_and_claim_main_run(run_repo, "run_pref_off")
            await executor_c.execute(lease_c)

            req_c = provider_c.requests[0]
            pref_messages_c = [
                m.content
                for m in req_c.messages
                if m.role is LlmRole.USER and "【用户个人工作背景与偏好】" in m.content
            ]
            assert len(pref_messages_c) == 1
            assert "我是数据平台架构师" in pref_messages_c[0]
            assert "【用户显式保存的备忘记忆】" not in pref_messages_c[0]
            assert "最新记忆：线上表变更必须附带回滚方案" not in pref_messages_c[0]
            assert "save_user_memory" not in {t.name for t in req_c.tools}

            # 场景 D：子 Run (depth=1) 严格隔离 -> 不注入背景/记忆，也不暴露写入工具。
            await pref_service.update_profile(
                "usr_1",
                custom_instructions="我是数据平台架构师，回答请直接给结论和关键指标。",
                memory_enabled=True,
            )
            parent_lease = await _submit_and_claim_main_run(run_repo, "run_parent_for_sub")
            sub_result = await run_repo.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id=parent_lease.run.id,
                    worker_id=parent_lease.run.worker_id or "",
                    execution_epoch=parent_lease.run.execution_epoch,
                    tool_call_id="call_sub_1",
                    call_message=ExecutionMessageDraft(
                        id="exec_dispatch_call",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id=parent_lease.run.id,
                        thread_id=parent_lease.run.id,
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_sub_1", DISPATCH_SUBAGENTS_TOOL_ID, {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        metadata=MessageMetadata(),
                        created_at=NOW,
                    ),
                    record_id="tcr_sub_1",
                    items=(
                        BeginSubagentBatchItem(
                            sub_run_id="run_sub_isolated",
                            subagent_profile_id="sub_prof_1",
                            subagent_slug="researcher",
                            subagent_name="研究员",
                            system_prompt="你是研究员子智能体",
                            model_profile_id="mp_1",
                            tool_snapshot=RuntimeToolSnapshot(tools=()),
                            skill_snapshot=None,
                            input_file_ids=(),
                            task_message=ExecutionMessageDraft(
                                id="exec_sub_task",
                                session_id="sess_1",
                                owner_id="usr_1",
                                run_id="run_sub_isolated",
                                thread_id="run_sub_isolated",
                                role=ExecutionRole.USER,
                                parts=(TextPart("只调研客观资料"),),
                                tool_call_id=None,
                                source_message_id=None,
                                metadata=MessageMetadata(),
                                created_at=NOW,
                            ),
                        ),
                    ),
                    requested_at=NOW,
                )
            )
            assert sub_result is not None
            sub_leases = await run_repo.claim(
                "worker-2", lease_seconds=60, execution_seconds=120, limit=1
            )
            assert len(sub_leases) == 1
            assert sub_leases[0].run.depth == 1

            provider_d = _FakeProvider([_llm_response(LlmText("子智能体客观调研结果。"))])
            executor_d = RunExecutor(
                run_repo,
                profile_repo,
                provider_d,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=runtime,
                preferences=pref_service,
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )
            await executor_d.execute(sub_leases[0])

            req_d = provider_d.requests[0]
            all_text_d = "\n".join(m.content for m in req_d.messages)
            assert "【用户个人工作背景与偏好】" not in all_text_d
            assert "【用户显式保存的备忘记忆】" not in all_text_d
            assert "我是数据平台架构师" not in all_text_d
            assert "最新记忆：线上表变更必须附带回滚方案" not in all_text_d
            assert "save_user_memory" not in {t.name for t in req_d.tools}
        finally:
            await engine.dispose()

    _run(scenario())


def test_run_executor_runs_save_user_memory_without_approval_and_handles_quota_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 Run 调用 save_user_memory 免审批落库；满配额时回传结构化错误且不崩溃 Run。"""

    async def scenario() -> None:
        engine, _, run_repo, profile_repo, pref_service, clock = await _setup_env(monkeypatch)
        try:
            runtime = _make_runtime(pref_service)

            # 1. 免审批写入一条 chat_explicit 记忆并完成 Run。
            provider_ok = _FakeProvider(
                [
                    _llm_response(
                        LlmToolCall(
                            call_id="call_mem_1",
                            name="save_user_memory",
                            arguments={"content": "周报固定在每周五下午发送"},
                        )
                    ),
                    _llm_response(LlmText("好的，已经为你记下周报发送习惯。")),
                ]
            )
            executor_ok = RunExecutor(
                run_repo,
                profile_repo,
                provider_ok,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=runtime,
                preferences=pref_service,
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )
            lease_ok = await _submit_and_claim_main_run(run_repo, "run_save_ok")
            await executor_ok.execute(lease_ok)

            finished_ok = await run_repo.get("sess_1", "usr_1", "run_save_ok")
            assert finished_ok is not None
            assert finished_ok.status is RunStatus.COMPLETED
            listed = await pref_service.list_memories("usr_1")
            assert listed.total == 1
            assert listed.items[0].content == "周报固定在每周五下午发送"
            assert listed.items[0].source is UserMemorySource.CHAT_EXPLICIT

            # 2. 填满 50 条后再次触发 save_user_memory：工具回传结构化错误给模型，Run 正常完成。
            for idx in range(2, MAX_USER_MEMORIES_PER_USER + 1):
                clock.advance(seconds=1)
                await pref_service.create_memory("usr_1", f"填充记忆 {idx}")

            provider_full = _FakeProvider(
                [
                    _llm_response(
                        LlmToolCall(
                            call_id="call_mem_full",
                            name="save_user_memory",
                            arguments={"content": "第 51 条记忆尝试"},
                        )
                    ),
                    _llm_response(LlmText("你的记忆已达 50 条上限，请先清理后再保存。")),
                ]
            )
            executor_full = RunExecutor(
                run_repo,
                profile_repo,
                provider_full,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=runtime,
                preferences=pref_service,
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )
            lease_full = await _submit_and_claim_main_run(run_repo, "run_save_full")
            await executor_full.execute(lease_full)

            finished_full = await run_repo.get("sess_1", "usr_1", "run_save_full")
            assert finished_full is not None
            assert finished_full.status is RunStatus.COMPLETED
            assert len(provider_full.requests) == 2
            tool_messages = [
                m for m in provider_full.requests[1].messages if m.role is LlmRole.TOOL
            ]
            assert len(tool_messages) == 1
            payload = json.loads(tool_messages[0].content)
            assert payload["status"] == "error"
            assert payload["code"] == "user_memory_limit_reached"
        finally:
            await engine.dispose()

    _run(scenario())
