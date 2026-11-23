"""Phase 2 Subagent 异步编排引擎单元测试与集成测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import DateTime, func, literal, select
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
from adapter.file.model import SessionFileModel
from adapter.llm.model import ModelProfileVersionModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.models import Base
from adapter.run.model import RunInputRequestModel, RunModel, RunToolCallModel
from adapter.run.repository.run import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.session.repository.session import SqlAlchemySessionRepository
from adapter.subagent.model import SubagentModel
from adapter.subagent.repository.subagent import SqlAlchemySubagentRepository
from adapter.system_prompt.model import SystemPromptSettingModel, SystemPromptVersionModel
from app.run.config import RunWorkerConfig
from app.run.executor import RunExecutor
from app.run.service import RunService
from app.run.stream import RunStreamService
from app.tool.runtime import (
    KNOWLEDGE_SEARCH_DEFINITION,
    KNOWLEDGE_SEARCH_ID,
    REQUEST_USER_INPUT_ID,
    RunToolRuntimeService,
)
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.file import SessionFileSource
from domain.llm.model import (
    LlmRequest,
    LlmResponse,
    LlmText,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.mcp import McpCallResult
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
from domain.run.model import (
    BeginSubagentBatchCommand,
    BeginSubagentBatchItem,
    FailRunCommand,
    FinishRunCommand,
    InputRequestStatus,
    RequestRunInputCommand,
    RunLease,
    RunSnapshotEvent,
    RunStatus,
    RunUsage,
    SubmitRunCommand,
)
from domain.run.model.errors import (
    RunConflictError,
    RunInputResourceError,
    RunStateError,
    SessionHasActiveRunError,
)
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID, SubagentProfile
from domain.tool import (
    AuthorizedRunContext,
    BeginToolCallBatchCommand,
    BeginToolCallBatchItem,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    ToolApprovalDecision,
    ToolCallStatus,
    ToolKind,
)
from infrastructure.security.mcp_secrets import McpSecretCipher

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


class _StubMcpCatalog:
    def __init__(self, tools: tuple[RuntimeToolDefinition, ...] = ()) -> None:
        self._tools = tools

    async def list_runtime_tools(self, *, limit: int = 200) -> tuple[RuntimeToolDefinition, ...]:
        return self._tools[:limit]

    async def get_call_target(
        self, tool_id: str, server_id: str, remote_name: str, config_version: int
    ) -> Any:
        del tool_id, server_id, remote_name, config_version
        return None


class _StubMcpExecutor:
    async def call(
        self, target: Any, arguments: dict[str, Any], *, deadline_seconds: float
    ) -> McpCallResult:
        del target, arguments, deadline_seconds
        raise NotImplementedError


class _StubSubagentRepo:
    def __init__(self, items: tuple[SubagentProfile, ...]) -> None:
        self._items = items

    async def list_enabled(self) -> tuple[SubagentProfile, ...]:
        return tuple(item for item in self._items if item.enabled)

    async def get_by_slug(self, slug: str) -> SubagentProfile | None:
        return next((item for item in self._items if item.slug == slug), None)


class _StubAuthReader:
    async def get_current_session_user(
        self, owner_id: str, session_id: str, now: datetime
    ) -> User | None:
        del now
        return User(
            id=owner_id,
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


class _FakeProvider:
    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[LlmRequest] = []

    @asynccontextmanager
    async def stream(self, profile: RuntimeModelProfile, request: LlmRequest) -> AsyncIterator[Any]:
        del profile
        self.requests.append(request)
        response = self._responses.pop(0)

        async def _iter() -> AsyncIterator[Any]:
            yield ResponseCompleted(response=response)

        yield _iter()


async def _setup_db(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession], SqlAlchemyRunRepository]:
    clock_now = NOW
    monkeypatch.setattr(
        func,
        "clock_timestamp",
        lambda: literal(clock_now, type_=DateTime(timezone=True)),
    )
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = SqlAlchemyRunRepository(
        factory,
        SqlAlchemyMessageRepository(factory),
    )
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
                SessionModel(
                    id="sess_other",
                    owner_id="usr_1",
                    title="Other Session",
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
                    tokenizer_encoding="o200k_base",
                    supports_stream=True,
                    supports_tools=True,
                    supports_parallel_tool_calls=True,
                    request_timeout_seconds=60,
                    execution_timeout_seconds=300,
                    max_concurrency=4,
                    enabled=True,
                    api_key_ciphertext=b"cipher",
                    api_key_nonce=b"nonce1234567",
                    api_key_key_version=1,
                    created_at=NOW,
                ),
                SubagentModel(
                    id="sub_prof_1",
                    slug="researcher",
                    name="研究员",
                    description="负责深度检索",
                    system_prompt="你是研究员子智能体",
                    model_profile_id="mp_1",
                    allowed_tool_ids=[REQUEST_USER_INPUT_ID, KNOWLEDGE_SEARCH_ID],
                    enabled=True,
                    max_llm_requests=10,
                    timeout_seconds=120,
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                SubagentModel(
                    id="sub_prof_2",
                    slug="coder",
                    name="编码员",
                    description="负责编写代码",
                    system_prompt="你是编码员子智能体",
                    model_profile_id=None,
                    allowed_tool_ids=[REQUEST_USER_INPUT_ID],
                    enabled=True,
                    max_llm_requests=8,
                    timeout_seconds=90,
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        from adapter.llm.model.profile import DefaultModelProfileModel

        session.add(DefaultModelProfileModel(singleton_id=1, profile_id="mp_1"))
    return engine, factory, repo


async def _submit_main_run(repo: SqlAlchemyRunRepository, run_id: str = "run_main") -> RunLease:
    await repo.submit(
        "sess_1",
        "usr_1",
        SubmitRunCommand(
            run_id=run_id,
            request_id=f"req_{run_id}",
            fingerprint=f"fp_{run_id}",
            prompt_template_version="run-v2",
            message=ConversationMessageDraft(
                id=f"msg_trigger_{run_id}",
                session_id="sess_1",
                owner_id="usr_1",
                role=ConversationRole.USER,
                status=MessageStatus.COMPLETED,
                parts=(TextPart("请帮我并行调研两个主题"),),
                reply_to_message_id=None,
                run_id=run_id,
                client_message_id=None,
                created_at=NOW,
                completed_at=NOW,
                metadata=MessageMetadata(),
                context_turn_id=f"msg_trigger_{run_id}",
            ),
            queued_at=NOW,
            auth_session_id="auth_1",
        ),
    )
    leases = await repo.claim("worker-1", lease_seconds=60, execution_seconds=300, limit=1)
    assert len(leases) == 1
    return leases[0]


def _subagent_batch_item(sub_run_id: str) -> BeginSubagentBatchItem:
    """构造一个最小子 Run 派发项，供工具序号回归复用。"""
    return BeginSubagentBatchItem(
        sub_run_id=sub_run_id,
        subagent_profile_id="sub_prof_1",
        subagent_slug="researcher",
        subagent_name="研究员",
        system_prompt="你是研究员",
        model_profile_id="mp_1",
        tool_snapshot=RuntimeToolSnapshot(tools=()),
        skill_snapshot=None,
        input_file_ids=(),
        task_message=ExecutionMessageDraft(
            id=f"exec_task_{sub_run_id}",
            session_id="sess_1",
            owner_id="usr_1",
            run_id=sub_run_id,
            thread_id=sub_run_id,
            role=ExecutionRole.USER,
            parts=(TextPart(f"任务 {sub_run_id}"),),
            tool_call_id=None,
            source_message_id=None,
            created_at=NOW,
            metadata=MessageMetadata(),
        ),
    )


def _subagent_batch_command(
    lease: RunLease,
    *,
    sequence: int,
    sub_run_id: str,
) -> BeginSubagentBatchCommand:
    """构造一次使用当前租约的 Subagent 派发命令。"""
    tool_call_id = f"call_subagent_{sequence}"
    return BeginSubagentBatchCommand(
        run_id=lease.run.id,
        worker_id=lease.run.worker_id or "",
        execution_epoch=lease.run.execution_epoch,
        tool_call_id=tool_call_id,
        call_message=ExecutionMessageDraft(
            id=f"exec_subagent_batch_{sequence}",
            session_id=lease.run.session_id,
            owner_id=lease.run.owner_id,
            run_id=lease.run.id,
            thread_id=lease.run.id,
            role=ExecutionRole.ASSISTANT,
            parts=(ToolCallPart(tool_call_id, DISPATCH_SUBAGENTS_TOOL_ID, {}),),
            tool_call_id=None,
            source_message_id=None,
            created_at=NOW,
            metadata=MessageMetadata(),
        ),
        record_id=f"tc_subagent_batch_{sequence}",
        items=(_subagent_batch_item(sub_run_id),),
        requested_at=NOW,
    )


def test_tool_runtime_snapshot_injects_dispatch_and_filters_subagent_tools() -> None:
    async def scenario() -> None:
        profile = SubagentProfile(
            id="sub_1",
            slug="researcher",
            name="研究员",
            description="深度检索资料",
            system_prompt="你是研究员",
            model_profile_id=None,
            allowed_tool_ids=(REQUEST_USER_INPUT_ID, DISPATCH_SUBAGENTS_TOOL_ID),
            enabled=True,
            max_llm_requests=10,
            timeout_seconds=120,
            version=1,
            created_by=None,
            deleted_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
        ctx = AuthorizedRunContext(
            owner_id="usr_1",
            chat_session_id="sess_1",
            project_id=None,
            auth_session_id="auth_1",
        )
        runtime_no_sub = RunToolRuntimeService(
            auth=_StubAuthReader(),  # type: ignore[arg-type]
            sessions=_StubSessionRepo(),  # type: ignore[arg-type]
            mcp_catalog=_StubMcpCatalog(),  # type: ignore[arg-type]
            mcp_executor=_StubMcpExecutor(),  # type: ignore[arg-type]
            knowledge=None,
            project_knowledge=None,
            subagents=_StubSubagentRepo(()),  # type: ignore[arg-type]
        )
        snap_empty = await runtime_no_sub.build_snapshot("run-v2", ctx, depth=0)
        assert snap_empty.by_model_name("dispatch_subagents") is None

        runtime = RunToolRuntimeService(
            auth=_StubAuthReader(),  # type: ignore[arg-type]
            sessions=_StubSessionRepo(),  # type: ignore[arg-type]
            mcp_catalog=_StubMcpCatalog((KNOWLEDGE_SEARCH_DEFINITION,)),  # type: ignore[arg-type]
            mcp_executor=_StubMcpExecutor(),  # type: ignore[arg-type]
            knowledge=None,
            project_knowledge=None,
            subagents=_StubSubagentRepo((profile,)),  # type: ignore[arg-type]
        )
        snap_main = await runtime.build_snapshot("run-v2", ctx, depth=0)
        dispatch_def = snap_main.by_model_name("dispatch_subagents")
        assert dispatch_def is not None
        assert "researcher（研究员）：深度检索资料" in dispatch_def.description

        # depth >= 1 强制剔除 dispatch_subagents 并按 allowed_tool_ids 严格求交集。
        snap_sub = await runtime.build_snapshot(
            "run-v2",
            ctx,
            depth=1,
            allowed_tool_ids=(REQUEST_USER_INPUT_ID, DISPATCH_SUBAGENTS_TOOL_ID),
        )
        assert snap_sub.by_model_name("dispatch_subagents") is None
        assert snap_sub.by_model_name("request_user_input") is not None
        assert snap_sub.by_model_name("knowledge_search") is None

    asyncio.run(scenario())


def test_executor_dispatch_subagents_releases_lease_and_rejects_mixed_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            lease = await _submit_main_run(repo)
            sub_repo = SqlAlchemySubagentRepository(factory)
            tool_runtime = RunToolRuntimeService(
                auth=_StubAuthReader(),  # type: ignore[arg-type]
                sessions=SqlAlchemySessionRepository(factory),
                mcp_catalog=_StubMcpCatalog(),  # type: ignore[arg-type]
                mcp_executor=_StubMcpExecutor(),  # type: ignore[arg-type]
                knowledge=None,
                project_knowledge=None,
                subagents=sub_repo,
            )
            provider = _FakeProvider(
                [
                    LlmResponse(
                        status="completed",
                        output=(
                            LlmText("我来派发两个子任务。"),
                            LlmToolCall(
                                call_id="call_dispatch_1",
                                name="dispatch_subagents",
                                arguments={
                                    "tasks": [
                                        {
                                            "subagent_slug": "researcher",
                                            "task": "调研 A",
                                            "context": "背景 A",
                                        },
                                        {"subagent_slug": "coder", "task": "实现 B"},
                                    ]
                                },
                            ),
                        ),
                        usage=LlmUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                        model="gpt-4o",
                        incomplete_reason=None,
                        response_id="resp_1",
                        request_id="req_1",
                    )
                ]
            )
            cipher = McpSecretCipher(b"0" * 32, key_version=1)
            executor = RunExecutor(
                repo,
                SqlAlchemyModelProfileRepository(factory, cipher),
                provider,  # type: ignore[arg-type]
                system_prompts=FakeSystemPromptRepository(),  # type: ignore[arg-type]
                tool_runtime=tool_runtime,
                subagents=sub_repo,
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )

            # 绕过 cipher 解密：直接替换 profiles.get_runtime
            async def _get_runtime(version_id: str) -> RuntimeModelProfile:
                from domain.llm.model.profile import ModelProfileVersion

                return RuntimeModelProfile(
                    version=ModelProfileVersion(
                        id=version_id,
                        name="Default",
                        protocol="chat_completions",
                        base_url="https://api.example.com/v1",
                        model="gpt-4o",
                        context_window_tokens=128000,
                        max_output_tokens=4096,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        supports_parallel_tool_calls=True,
                        request_timeout_seconds=60,
                        execution_timeout_seconds=300,
                        max_concurrency=4,
                        temperature=0.0,
                        has_api_key=True,
                        enabled=True,
                        created_at=NOW,
                    ),
                    api_key="sk-test",
                )

            executor._profiles.get_runtime = _get_runtime  # type: ignore[method-assign]
            await executor.execute(lease)

            main_run = await repo.get("sess_1", "usr_1", "run_main")
            assert main_run is not None
            assert main_run.status is RunStatus.WAITING_FOR_SUBAGENTS
            assert main_run.worker_id is None
            assert main_run.lease_expires_at is None
            assert main_run.execution_deadline_at is None

            tasks = await repo.list_subagent_tasks("sess_1", "usr_1", "run_main")
            assert len(tasks) == 2
            assert all(t.status is RunStatus.QUEUED for t in tasks)

            # 混合调用 request_user_input 与 dispatch_subagents 必须失败。
            provider_mix = _FakeProvider(
                [
                    LlmResponse(
                        status="completed",
                        output=(
                            LlmToolCall("c1", "request_user_input", {"question": "q"}),
                            LlmToolCall(
                                "c2",
                                "dispatch_subagents",
                                {"tasks": [{"subagent_slug": "coder", "task": "t"}]},
                            ),
                        ),
                        usage=LlmUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                        model="gpt-4o",
                        incomplete_reason=None,
                        response_id="resp_1",
                        request_id="req_1",
                    )
                ]
            )
            # 先把 run_main 与子 Run 置为 completed 以允许提交 run_mix_2
            async with factory() as s, s.begin():
                for row in (
                    await s.scalars(select(RunModel).where(RunModel.session_id == "sess_1"))
                ).all():
                    row.status = RunStatus.COMPLETED.value
            lease2 = await _submit_main_run(repo, run_id="run_mix_2")
            executor_mix = RunExecutor(
                repo,
                executor._profiles,
                provider_mix,  # type: ignore[arg-type]
                system_prompts=FakeSystemPromptRepository(),  # type: ignore[arg-type]
                tool_runtime=tool_runtime,
                subagents=sub_repo,
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )
            await executor_mix.execute(lease2)
            mix_run = await repo.get("sess_1", "usr_1", "run_mix_2")
            assert mix_run is not None
            assert mix_run.status is RunStatus.FAILED
            assert mix_run.safe_error_code == "llm_tool_call_mix_unsupported"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_begin_subagent_batch_enforces_limits_and_file_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            lease = await _submit_main_run(repo)
            async with factory() as s, s.begin():
                s.add(
                    SessionFileModel(
                        id="file_other_sess",
                        session_id="sess_other",
                        owner_id="usr_1",
                        filename="secret.txt",
                        mime_type="text/plain",
                        size_bytes=10,
                        sha256="a" * 64,
                        source=SessionFileSource.UPLOAD.value,
                        object_key="k1",
                        idempotency_key="idem_other",
                        request_fingerprint="fp_other",
                        created_at=NOW,
                    )
                )

            def _make_item(idx: int, file_ids: tuple[str, ...] = ()) -> BeginSubagentBatchItem:
                sub_id = f"sub_{idx}"
                return BeginSubagentBatchItem(
                    sub_run_id=sub_id,
                    subagent_profile_id="sub_prof_1",
                    subagent_slug="researcher",
                    subagent_name="研究员",
                    system_prompt="你是研究员子智能体",
                    model_profile_id="mp_1",
                    tool_snapshot=RuntimeToolSnapshot(tools=()),
                    skill_snapshot=None,
                    input_file_ids=file_ids,
                    task_message=ExecutionMessageDraft(
                        id=f"exec_task_{idx}",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id=sub_id,
                        thread_id=sub_id,
                        role=ExecutionRole.USER,
                        parts=(TextPart("task"),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                )

            def _make_cmd(items: tuple[BeginSubagentBatchItem, ...]) -> BeginSubagentBatchCommand:
                return BeginSubagentBatchCommand(
                    run_id="run_main",
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    tool_call_id="call_1",
                    call_message=ExecutionMessageDraft(
                        id="exec_call_1",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="run_main",
                        thread_id="run_main",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_1", "dispatch_subagents", {"tasks": []}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    record_id="tc_1",
                    items=items,
                    requested_at=NOW,
                )

            # 1. 单批 > 5 拒绝
            with pytest.raises(RunStateError, match="subagent_batch_size_invalid"):
                await repo.begin_subagent_batch(_make_cmd(tuple(_make_item(i) for i in range(6))))

            # 2. 跨会话文件拒绝
            with pytest.raises(RunInputResourceError, match="run_input_file_unavailable"):
                await repo.begin_subagent_batch(_make_cmd((_make_item(1, ("file_other_sess",)),)))

            # 3. 累计上限 15 拒绝（模拟已有 14 条子 Run，再派发 2 条 -> 拒绝）
            async with factory() as s, s.begin():
                for i in range(14):
                    s.add(
                        RunModel(
                            id=f"existing_sub_{i}",
                            session_id="sess_1",
                            owner_id="usr_1",
                            status=RunStatus.COMPLETED.value,
                            trigger_message_id="msg_trigger_run_main",
                            context_cutoff_sequence=0,
                            model_config_version_id="mp_1",
                            main_prompt_version_id=TEST_MAIN_PROMPT_ID,
                            compression_prompt_version_id=TEST_COMPRESSION_PROMPT_ID,
                            prompt_template_version="run-v2",
                            queued_at=NOW,
                            created_at=NOW,
                            updated_at=NOW,
                            usage={},
                            parent_run_id="run_main",
                            root_run_id="run_main",
                            depth=1,
                        )
                    )
            with pytest.raises(RunStateError, match="subagent_dispatch_limit_exceeded"):
                await repo.begin_subagent_batch(_make_cmd((_make_item(100), _make_item(101))))
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_begin_subagent_batch_uses_parent_max_tool_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通工具已落库后，Subagent 批次必须续接父 Run 的最大工具序号。"""

    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            lease = await _submit_main_run(repo)
            await repo.begin_tool_call_batch(
                BeginToolCallBatchCommand(
                    run_id=lease.run.id,
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    call_message=ExecutionMessageDraft(
                        id="exec_normal_tool",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id=lease.run.id,
                        thread_id=lease.run.id,
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_normal_tool", "safe_tool", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    calls=(
                        BeginToolCallBatchItem(
                            record_id="normal_tool_record",
                            tool_call_id="call_normal_tool",
                            model_name="safe_tool",
                            tool_id="builtin_safe_tool",
                            tool_kind=ToolKind.BUILTIN,
                            requires_approval=False,
                        ),
                    ),
                    requested_at=NOW,
                )
            )
            async with factory() as session, session.begin():
                normal_record = await session.get(RunToolCallModel, "normal_tool_record")
                assert normal_record is not None
                normal_record.status = ToolCallStatus.COMPLETED.value
                normal_record.finished_at = NOW

            await repo.begin_subagent_batch(
                _subagent_batch_command(
                    lease,
                    sequence=1,
                    sub_run_id="sub_after_normal_tool",
                )
            )

            async with factory() as session:
                records = (
                    await session.scalars(
                        select(RunToolCallModel)
                        .where(RunToolCallModel.run_id == lease.run.id)
                        .order_by(RunToolCallModel.ordinal)
                    )
                ).all()
            assert [(record.id, record.ordinal) for record in records] == [
                ("normal_tool_record", 1),
                ("tc_subagent_batch_1", 2),
            ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_begin_subagent_batch_continues_after_previous_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一父 Run 完成首批子任务并恢复执行后，第二批必须继续递增 ordinal。"""

    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            first_parent_lease = await _submit_main_run(repo)
            await repo.begin_subagent_batch(
                _subagent_batch_command(
                    first_parent_lease,
                    sequence=1,
                    sub_run_id="sub_sequence_1",
                )
            )

            child_leases = await repo.claim(
                "worker-child",
                lease_seconds=60,
                execution_seconds=300,
                limit=1,
            )
            assert len(child_leases) == 1
            child_lease = child_leases[0]
            await repo.finish(
                FinishRunCommand(
                    run_id=child_lease.run.id,
                    worker_id=child_lease.run.worker_id or "",
                    execution_epoch=child_lease.run.execution_epoch,
                    reply_message=ConversationMessageDraft(
                        id="msg_sub_sequence_1",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("第一批完成"),),
                        reply_to_message_id=child_lease.run.trigger_message_id,
                        run_id=child_lease.run.id,
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id=child_lease.run.trigger_message_id,
                    ),
                    usage=RunUsage(),
                    finished_at=NOW,
                )
            )

            second_parent_leases = await repo.claim(
                "worker-parent-2",
                lease_seconds=60,
                execution_seconds=300,
                limit=1,
            )
            assert len(second_parent_leases) == 1
            second_parent_lease = second_parent_leases[0]
            assert second_parent_lease.run.id == "run_main"
            await repo.begin_subagent_batch(
                _subagent_batch_command(
                    second_parent_lease,
                    sequence=2,
                    sub_run_id="sub_sequence_2",
                )
            )

            async with factory() as session:
                records = (
                    await session.scalars(
                        select(RunToolCallModel)
                        .where(RunToolCallModel.run_id == "run_main")
                        .order_by(RunToolCallModel.ordinal)
                    )
                ).all()
            assert [(record.tool_call_id, record.ordinal) for record in records] == [
                ("call_subagent_1", 1),
                ("call_subagent_2", 2),
            ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_subagent_context_isolation_without_conversation_pollution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            lease = await _submit_main_run(repo)
            await repo.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id="run_main",
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    tool_call_id="call_iso",
                    call_message=ExecutionMessageDraft(
                        id="exec_call_iso",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="run_main",
                        thread_id="run_main",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_iso", "dispatch_subagents", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    record_id="tc_iso",
                    items=(
                        BeginSubagentBatchItem(
                            sub_run_id="sub_iso_1",
                            subagent_profile_id="sub_prof_1",
                            subagent_slug="researcher",
                            subagent_name="研究员",
                            system_prompt="你是研究员",
                            model_profile_id="mp_1",
                            tool_snapshot=RuntimeToolSnapshot(tools=()),
                            skill_snapshot=None,
                            input_file_ids=(),
                            task_message=ExecutionMessageDraft(
                                id="exec_sub_task_1",
                                session_id="sess_1",
                                owner_id="usr_1",
                                run_id="sub_iso_1",
                                thread_id="sub_iso_1",
                                role=ExecutionRole.USER,
                                parts=(TextPart("只给子智能体的独立任务"),),
                                tool_call_id=None,
                                source_message_id=None,
                                created_at=NOW,
                                metadata=MessageMetadata(),
                            ),
                        ),
                    ),
                    requested_at=NOW,
                )
            )
            # 子 Run 不拉取主会话正式对话历史，仅见自身执行消息。
            sub_messages = await repo.list_context_messages("sess_1", "usr_1", "sub_iso_1")
            assert sub_messages is not None
            assert len(sub_messages) == 1
            assert sub_messages[0].id == "exec_sub_task_1"
            assert await repo.list_context_summaries("sess_1", "usr_1", "sub_iso_1") == ()

            # 领取并完成子 Run：不触发自动记忆提取，也不污染 conversation_messages。
            sub_leases = await repo.claim("w-sub", lease_seconds=60, execution_seconds=300, limit=5)
            assert len(sub_leases) == 1
            sub_lease = sub_leases[0]
            finished = await repo.finish(
                FinishRunCommand(
                    run_id=sub_lease.run.id,
                    worker_id=sub_lease.run.worker_id or "",
                    execution_epoch=sub_lease.run.execution_epoch,
                    reply_message=ConversationMessageDraft(
                        id="msg_ignored",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("子智能体调研结论"),),
                        reply_to_message_id=sub_lease.run.trigger_message_id,
                        run_id=sub_lease.run.id,
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id=sub_lease.run.trigger_message_id,
                    ),
                    usage=RunUsage(),
                    finished_at=NOW,
                )
            )
            assert finished is not None
            assert finished.status is RunStatus.COMPLETED

            async with factory() as s:
                conv_count = len(
                    (
                        await s.scalars(
                            select(ConversationMessageModel).where(
                                ConversationMessageModel.session_id == "sess_1"
                            )
                        )
                    ).all()
                )
                # 主会话仅有一条原始用户消息，未被子 Run 输出污染。
                assert conv_count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_subagent_completion_wakes_parent_once_with_partial_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            lease = await _submit_main_run(repo)
            items = tuple(
                BeginSubagentBatchItem(
                    sub_run_id=f"sub_c_{i}",
                    subagent_profile_id="sub_prof_1",
                    subagent_slug="researcher",
                    subagent_name=f"研究员{i}",
                    system_prompt="你是研究员",
                    model_profile_id="mp_1",
                    tool_snapshot=RuntimeToolSnapshot(tools=()),
                    skill_snapshot=None,
                    input_file_ids=(),
                    task_message=ExecutionMessageDraft(
                        id=f"exec_c_task_{i}",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id=f"sub_c_{i}",
                        thread_id=f"sub_c_{i}",
                        role=ExecutionRole.USER,
                        parts=(TextPart(f"任务{i}"),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                )
                for i in range(3)
            )
            await repo.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id="run_main",
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    tool_call_id="call_conc",
                    call_message=ExecutionMessageDraft(
                        id="exec_call_conc",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="run_main",
                        thread_id="run_main",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_conc", "dispatch_subagents", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    record_id="tc_conc",
                    items=items,
                    requested_at=NOW,
                )
            )
            sub_leases = await repo.claim(
                "w-conc", lease_seconds=60, execution_seconds=300, limit=5
            )
            assert len(sub_leases) == 3

            # 为 sub_c_0 注入一条带 artifact 的 ToolResultPart 执行消息
            async with factory() as s, s.begin():
                s.add(
                    ExecutionMessageModel(
                        id="exec_art_0",
                        session_id="sess_1",
                        run_id="sub_c_0",
                        thread_id="sub_c_0",
                        sequence=100,
                        role=ExecutionRole.TOOL.value,
                        parts=[
                            {
                                "type": "tool_result",
                                "tool_call_id": "tc_sub_0",
                                "status": "success",
                                "output": "ok",
                                "artifacts": [
                                    {
                                        "file_id": "file_art_1",
                                        "filename": "report.pdf",
                                        "label": "报告",
                                        "mime_type": "application/pdf",
                                        "size_bytes": 1234,
                                    }
                                ],
                            }
                        ],
                        tool_call_id="tc_sub_0",
                        created_at=NOW,
                    )
                )

            def _finish_cmd(sl: RunLease, text: str) -> FinishRunCommand:
                return FinishRunCommand(
                    run_id=sl.run.id,
                    worker_id=sl.run.worker_id or "",
                    execution_epoch=sl.run.execution_epoch,
                    reply_message=ConversationMessageDraft(
                        id=f"msg_{sl.run.id}",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart(text),),
                        reply_to_message_id=sl.run.trigger_message_id,
                        run_id=sl.run.id,
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id=sl.run.trigger_message_id,
                    ),
                    usage=RunUsage(),
                    finished_at=NOW,
                )

            by_id = {sl.run.id: sl for sl in sub_leases}
            # 依次结算 2 个完成、1 个失败（SQLite 内存库无行锁，PG 下由 FOR UPDATE 串行化）。
            await repo.finish(_finish_cmd(by_id["sub_c_0"], "结果0"))
            # 首个完成后父 Run 仍为 waiting_for_subagents
            mid_parent = await repo.get("sess_1", "usr_1", "run_main")
            assert mid_parent is not None
            assert mid_parent.status is RunStatus.WAITING_FOR_SUBAGENTS
            await repo.finish(_finish_cmd(by_id["sub_c_1"], "结果1"))
            await repo.fail(
                FailRunCommand(
                    run_id="sub_c_2",
                    worker_id=by_id["sub_c_2"].run.worker_id or "",
                    execution_epoch=by_id["sub_c_2"].run.execution_epoch,
                    safe_error_code="execution_timeout",
                    failed_at=NOW,
                )
            )

            parent = await repo.get("sess_1", "usr_1", "run_main")
            assert parent is not None
            assert parent.status is RunStatus.QUEUED

            parent_events = await repo.list_events(
                "sess_1", "usr_1", "run_main", after_sequence=0, limit=50
            )
            resumed_events = [e for e in parent_events if e.type == "subagents_resumed"]
            assert len(resumed_events) == 1

            async with factory() as s:
                tool_results = (
                    await s.scalars(
                        select(ExecutionMessageModel).where(
                            ExecutionMessageModel.run_id == "run_main",
                            ExecutionMessageModel.role == ExecutionRole.TOOL.value,
                            ExecutionMessageModel.tool_call_id == "call_conc",
                        )
                    )
                ).all()
                assert len(tool_results) == 1
                summary = json.loads(tool_results[0].parts[0]["output"])  # type: ignore[index]
                assert summary["completed_count"] == 2
                assert summary["failed_count"] == 1
                assert summary["results"][0]["output"] == "结果0"
                assert summary["results"][0]["artifacts"][0]["file_id"] == "file_art_1"
                assert summary["results"][2]["safe_error_code"] == "execution_timeout"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_reclaim_expired_subagent_wakes_parent_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            lease = await _submit_main_run(repo)
            await repo.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id="run_main",
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    tool_call_id="call_rec",
                    call_message=ExecutionMessageDraft(
                        id="exec_call_rec",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="run_main",
                        thread_id="run_main",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_rec", "dispatch_subagents", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    record_id="tc_rec",
                    items=(
                        BeginSubagentBatchItem(
                            sub_run_id="sub_rec_1",
                            subagent_profile_id="sub_prof_1",
                            subagent_slug="researcher",
                            subagent_name="研究员",
                            system_prompt="你是研究员",
                            model_profile_id="mp_1",
                            tool_snapshot=RuntimeToolSnapshot(tools=()),
                            skill_snapshot=None,
                            input_file_ids=(),
                            task_message=ExecutionMessageDraft(
                                id="exec_rec_task",
                                session_id="sess_1",
                                owner_id="usr_1",
                                run_id="sub_rec_1",
                                thread_id="sub_rec_1",
                                role=ExecutionRole.USER,
                                parts=(TextPart("任务"),),
                                tool_call_id=None,
                                source_message_id=None,
                                created_at=NOW,
                                metadata=MessageMetadata(),
                            ),
                        ),
                    ),
                    requested_at=NOW,
                )
            )
            sub_leases = await repo.claim("w-rec", lease_seconds=60, execution_seconds=300, limit=5)
            assert len(sub_leases) == 1
            # 强制将租约设为已过期
            async with factory() as s, s.begin():
                sub_row = await s.get(RunModel, "sub_rec_1")
                assert sub_row is not None
                sub_row.lease_expires_at = NOW - timedelta(seconds=10)

            reclaimed = await repo.reclaim_expired(limit=10)
            assert len(reclaimed) == 1
            assert reclaimed[0].status is RunStatus.FAILED
            assert reclaimed[0].safe_error_code == "execution_interrupted"

            parent = await repo.get("sess_1", "usr_1", "run_main")
            assert parent is not None
            assert parent.status is RunStatus.QUEUED
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_cancel_parent_cascades_to_active_subagents_and_blocks_session_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            lease = await _submit_main_run(repo)
            items = tuple(
                BeginSubagentBatchItem(
                    sub_run_id=f"sub_k_{i}",
                    subagent_profile_id="sub_prof_1",
                    subagent_slug="researcher",
                    subagent_name=f"研究员{i}",
                    system_prompt="你是研究员",
                    model_profile_id="mp_1",
                    tool_snapshot=RuntimeToolSnapshot(tools=()),
                    skill_snapshot=None,
                    input_file_ids=(),
                    task_message=ExecutionMessageDraft(
                        id=f"exec_k_task_{i}",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id=f"sub_k_{i}",
                        thread_id=f"sub_k_{i}",
                        role=ExecutionRole.USER,
                        parts=(TextPart(f"任务{i}"),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                )
                for i in range(3)
            )
            await repo.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id="run_main",
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    tool_call_id="call_k",
                    call_message=ExecutionMessageDraft(
                        id="exec_call_k",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="run_main",
                        thread_id="run_main",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_k", "dispatch_subagents", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    record_id="tc_k",
                    items=items,
                    requested_at=NOW,
                )
            )

            # waiting_for_subagents 期间并发发消息返回 409，删除会话返回 409。
            with pytest.raises(RunConflictError, match="run_already_active"):
                await repo.submit(
                    "sess_1",
                    "usr_1",
                    SubmitRunCommand(
                        run_id="run_blocked",
                        request_id="req_blocked",
                        fingerprint="fp_blocked",
                        prompt_template_version="run-v2",
                        message=ConversationMessageDraft(
                            id="msg_blocked",
                            session_id="sess_1",
                            owner_id="usr_1",
                            role=ConversationRole.USER,
                            status=MessageStatus.COMPLETED,
                            parts=(TextPart("新消息"),),
                            reply_to_message_id=None,
                            run_id="run_blocked",
                            client_message_id=None,
                            created_at=NOW,
                            completed_at=NOW,
                            metadata=MessageMetadata(),
                            context_turn_id="msg_blocked",
                        ),
                        queued_at=NOW,
                    ),
                )
            sess_repo = SqlAlchemySessionRepository(factory)
            with pytest.raises(SessionHasActiveRunError, match="session_has_active_run"):
                await sess_repo.soft_delete("usr_1", "sess_1", NOW)

            # 领取 sub_k_0 (running) 和 sub_k_1 (转为 waiting_for_input)，sub_k_2 保持 queued。
            claimed = await repo.claim("w-k", lease_seconds=60, execution_seconds=300, limit=2)
            assert len(claimed) == 2
            await repo.request_input(
                RequestRunInputCommand(
                    run_id="sub_k_1",
                    worker_id=claimed[1].run.worker_id or "",
                    execution_epoch=claimed[1].run.execution_epoch,
                    input_request_id="inp_k_1",
                    question_message=ConversationMessageDraft(
                        id="msg_q_k_1",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("请确认范围？"),),
                        reply_to_message_id=claimed[1].run.trigger_message_id,
                        run_id="sub_k_1",
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id=claimed[1].run.trigger_message_id,
                    ),
                    execution_messages=(
                        ExecutionMessageDraft(
                            id="exec_q_k_1",
                            session_id="sess_1",
                            owner_id="usr_1",
                            run_id="sub_k_1",
                            thread_id="sub_k_1",
                            role=ExecutionRole.ASSISTANT,
                            parts=(
                                ToolCallPart(
                                    "tc_q_1", "request_user_input", {"question": "请确认范围？"}
                                ),
                            ),
                            tool_call_id=None,
                            source_message_id="msg_q_k_1",
                            created_at=NOW,
                            metadata=MessageMetadata(),
                        ),
                    ),
                    usage=RunUsage(),
                    created_at=NOW,
                )
            )

            cancelled_parent = await repo.cancel("sess_1", "usr_1", "run_main", NOW)
            assert cancelled_parent is not None
            assert cancelled_parent.status is RunStatus.CANCELLED

            sub0 = await repo.get("sess_1", "usr_1", "sub_k_0")
            sub1 = await repo.get("sess_1", "usr_1", "sub_k_1")
            sub2 = await repo.get("sess_1", "usr_1", "sub_k_2")
            assert sub0 is not None and sub0.status is RunStatus.CANCELLING
            assert sub1 is not None and sub1.status is RunStatus.CANCELLED
            assert sub2 is not None and sub2.status is RunStatus.CANCELLED
            async with factory() as s:
                req = await s.get(RunInputRequestModel, "inp_k_1")
                assert req is not None
                assert req.status == InputRequestStatus.CLOSED.value
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_subagent_hitl_routing_aggregation_and_stream_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            lease = await _submit_main_run(repo)
            items = tuple(
                BeginSubagentBatchItem(
                    sub_run_id=f"sub_h_{i}",
                    subagent_profile_id="sub_prof_1",
                    subagent_slug="researcher",
                    subagent_name=f"研究员{i}",
                    system_prompt="你是研究员",
                    model_profile_id="mp_1",
                    tool_snapshot=RuntimeToolSnapshot(tools=()),
                    skill_snapshot=None,
                    input_file_ids=(),
                    task_message=ExecutionMessageDraft(
                        id=f"exec_h_task_{i}",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id=f"sub_h_{i}",
                        thread_id=f"sub_h_{i}",
                        role=ExecutionRole.USER,
                        parts=(TextPart(f"任务{i}"),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                )
                for i in range(2)
            )
            await repo.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id="run_main",
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    tool_call_id="call_h",
                    call_message=ExecutionMessageDraft(
                        id="exec_call_h",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="run_main",
                        thread_id="run_main",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_h", "dispatch_subagents", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    record_id="tc_h",
                    items=items,
                    requested_at=NOW,
                )
            )
            claimed = await repo.claim("w-h", lease_seconds=60, execution_seconds=300, limit=2)
            assert len(claimed) == 2

            # sub_h_0 请求用户输入
            await repo.request_input(
                RequestRunInputCommand(
                    run_id="sub_h_0",
                    worker_id=claimed[0].run.worker_id or "",
                    execution_epoch=claimed[0].run.execution_epoch,
                    input_request_id="inp_h_0",
                    question_message=ConversationMessageDraft(
                        id="msg_q_h_0",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("需要什么格式？"),),
                        reply_to_message_id=claimed[0].run.trigger_message_id,
                        run_id="sub_h_0",
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id=claimed[0].run.trigger_message_id,
                    ),
                    execution_messages=(
                        ExecutionMessageDraft(
                            id="exec_q_h_0",
                            session_id="sess_1",
                            owner_id="usr_1",
                            run_id="sub_h_0",
                            thread_id="sub_h_0",
                            role=ExecutionRole.ASSISTANT,
                            parts=(
                                ToolCallPart(
                                    "tc_q_0", "request_user_input", {"question": "需要什么格式？"}
                                ),
                            ),
                            tool_call_id=None,
                            source_message_id="msg_q_h_0",
                            created_at=NOW,
                            metadata=MessageMetadata(),
                        ),
                    ),
                    usage=RunUsage(),
                    created_at=NOW,
                )
            )

            # sub_h_1 触发需要审批的工具
            await repo.begin_tool_call_batch(
                BeginToolCallBatchCommand(
                    run_id="sub_h_1",
                    worker_id=claimed[1].run.worker_id or "",
                    execution_epoch=claimed[1].run.execution_epoch,
                    call_message=ExecutionMessageDraft(
                        id="exec_app_1",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="sub_h_1",
                        thread_id="sub_h_1",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("tc_app_1", "knowledge_search", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    calls=(
                        BeginToolCallBatchItem(
                            record_id="call_rec_1",
                            tool_call_id="tc_app_1",
                            model_name="knowledge_search",
                            tool_id=KNOWLEDGE_SEARCH_ID,
                            tool_kind=ToolKind.BUILTIN,
                            requires_approval=True,
                        ),
                    ),
                    requested_at=NOW,
                    round=1,
                )
            )

            # RunService.get 返回聚合后的 subagent_tasks 与待处理子任务 HITL 队列
            service = RunService(repo, clock=lambda: NOW)
            details = await service.get("sess_1", "usr_1", "run_main")
            assert len(details.subagent_tasks) == 2
            assert len(details.pending_subagent_inputs) == 1
            assert details.pending_subagent_inputs[0].sub_run_id == "sub_h_0"
            assert details.pending_subagent_inputs[0].question_text == "需要什么格式？"
            assert len(details.pending_subagent_approvals) == 1
            assert details.pending_subagent_approvals[0].sub_run_id == "sub_h_1"
            assert details.pending_subagent_approvals[0].call_id == "call_rec_1"

            # 定向提交回答与审批后，各自子 Run 恢复为 queued
            resumed_0 = await service.answer(
                "sess_1", "usr_1", "sub_h_0", "inp_h_0", "req_ans_0", "Markdown"
            )
            assert resumed_0.status is RunStatus.QUEUED
            resumed_1 = await service.decide_tool_approval(
                "sess_1",
                "usr_1",
                "sub_h_1",
                "call_rec_1",
                "req_app_1",
                ToolApprovalDecision.APPROVE,
            )
            assert resumed_1.run.status is RunStatus.QUEUED

            # SSE 流在 waiting_for_subagents 期间不提前关闭（发出初始 snapshot 后仍保持活跃）
            class _FakeAuth:
                async def authenticate(self, token: str) -> AuthContext | None:
                    del token
                    return AuthContext(
                        user=User(
                            id="usr_1",
                            email="owner@example.com",
                            display_name="Owner",
                            role=UserRole.USER,
                            status=UserStatus.ACTIVE,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        session_id="auth_1",
                    )

            stream_service = RunStreamService(repo, _FakeAuth(), None)  # type: ignore[arg-type]
            gen = stream_service.events("sess_1", "usr_1", "run_main", "tok")
            first_event = await anext(gen)
            assert isinstance(first_event, RunSnapshotEvent)
            assert first_event.payload.status is RunStatus.WAITING_FOR_SUBAGENTS
            assert len(first_event.payload.subagent_tasks) == 2
            # 确认流未结束（等待下一事件超时而非立即 StopAsyncIteration）
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(gen), timeout=0.1)
            await gen.aclose()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_same_subagent_concurrent_tasks_expose_task_index_and_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 Subagent 并发派发多任务时，task_index 与 task_summary 能准确区分任务来源。"""

    async def scenario() -> None:
        from api.converter.run import run_details_to_response

        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            lease = await _submit_main_run(repo)
            long_task = "分析华南区" + ("超长任务细节" * 40)
            items = (
                BeginSubagentBatchItem(
                    sub_run_id="sub_idx_1",
                    subagent_profile_id="sub_prof_1",
                    subagent_slug="researcher",
                    subagent_name="研究员",
                    system_prompt="你是研究员",
                    model_profile_id="mp_1",
                    tool_snapshot=RuntimeToolSnapshot(tools=()),
                    skill_snapshot=None,
                    input_file_ids=(),
                    task_message=ExecutionMessageDraft(
                        id="exec_idx_task_1",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="sub_idx_1",
                        thread_id="sub_idx_1",
                        role=ExecutionRole.USER,
                        parts=(
                            TextPart(
                                "任务：\n分析第一季度\n华东区营收\n\n补充上下文：\n只看线上渠道"
                            ),
                        ),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                ),
                BeginSubagentBatchItem(
                    sub_run_id="sub_idx_2",
                    subagent_profile_id="sub_prof_1",
                    subagent_slug="researcher",
                    subagent_name="研究员",
                    system_prompt="你是研究员",
                    model_profile_id="mp_1",
                    tool_snapshot=RuntimeToolSnapshot(tools=()),
                    skill_snapshot=None,
                    input_file_ids=(),
                    task_message=ExecutionMessageDraft(
                        id="exec_idx_task_2",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="sub_idx_2",
                        thread_id="sub_idx_2",
                        role=ExecutionRole.USER,
                        parts=(TextPart(long_task),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW + timedelta(seconds=1),
                        metadata=MessageMetadata(),
                    ),
                ),
            )
            await repo.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id="run_main",
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    tool_call_id="call_idx",
                    call_message=ExecutionMessageDraft(
                        id="exec_call_idx",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="run_main",
                        thread_id="run_main",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_idx", "dispatch_subagents", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    record_id="tc_idx",
                    items=items,
                    requested_at=NOW,
                )
            )
            claimed = await repo.claim("w-idx", lease_seconds=60, execution_seconds=300, limit=2)
            assert len(claimed) == 2

            # 两个同 slug 子任务同时提问
            for idx, claim_item in enumerate(claimed, start=1):
                await repo.request_input(
                    RequestRunInputCommand(
                        run_id=claim_item.run.id,
                        worker_id=claim_item.run.worker_id or "",
                        execution_epoch=claim_item.run.execution_epoch,
                        input_request_id=f"inp_idx_{idx}",
                        question_message=ConversationMessageDraft(
                            id=f"msg_q_idx_{idx}",
                            session_id="sess_1",
                            owner_id="usr_1",
                            role=ConversationRole.ASSISTANT,
                            status=MessageStatus.COMPLETED,
                            parts=(TextPart(f"请确认任务{idx}口径？"),),
                            reply_to_message_id=claim_item.run.trigger_message_id,
                            run_id=claim_item.run.id,
                            client_message_id=None,
                            created_at=NOW + timedelta(seconds=idx),
                            completed_at=NOW + timedelta(seconds=idx),
                            metadata=MessageMetadata(),
                            context_turn_id=claim_item.run.trigger_message_id,
                        ),
                        execution_messages=(
                            ExecutionMessageDraft(
                                id=f"exec_q_idx_{idx}",
                                session_id="sess_1",
                                owner_id="usr_1",
                                run_id=claim_item.run.id,
                                thread_id=claim_item.run.id,
                                role=ExecutionRole.ASSISTANT,
                                parts=(
                                    ToolCallPart(
                                        f"tc_q_idx_{idx}",
                                        "request_user_input",
                                        {"question": f"请确认任务{idx}口径？"},
                                    ),
                                ),
                                tool_call_id=None,
                                source_message_id=f"msg_q_idx_{idx}",
                                created_at=NOW + timedelta(seconds=idx),
                                metadata=MessageMetadata(),
                            ),
                        ),
                        usage=RunUsage(),
                        created_at=NOW + timedelta(seconds=idx),
                    )
                )

            service = RunService(repo, clock=lambda: NOW)
            details = await service.get("sess_1", "usr_1", "run_main")
            task_summaries = [
                (t.sub_run_id, t.task_index, t.task_summary) for t in details.subagent_tasks
            ]
            assert task_summaries == [
                ("sub_idx_1", 1, "分析第一季度 华东区营收"),
                ("sub_idx_2", 2, long_task[:119] + "…"),
            ]
            assert len(details.subagent_tasks[1].task_summary) == 120
            assert [
                (i.sub_run_id, i.task_index, i.task_summary)
                for i in details.pending_subagent_inputs
            ] == [
                ("sub_idx_1", 1, "分析第一季度 华东区营收"),
                ("sub_idx_2", 2, long_task[:119] + "…"),
            ]

            # 验证仅第 2 个子任务处于待审批时，task_index 仍为 2（而非误算为 1）
            await service.answer("sess_1", "usr_1", "sub_idx_2", "inp_idx_2", "req_idx_2", "按含税")
            claimed_2 = await repo.claim(
                "w-idx-2", lease_seconds=60, execution_seconds=300, limit=1
            )
            assert len(claimed_2) == 1 and claimed_2[0].run.id == "sub_idx_2"
            await repo.begin_tool_call_batch(
                BeginToolCallBatchCommand(
                    run_id="sub_idx_2",
                    worker_id=claimed_2[0].run.worker_id or "",
                    execution_epoch=claimed_2[0].run.execution_epoch,
                    call_message=ExecutionMessageDraft(
                        id="exec_app_idx_2",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="sub_idx_2",
                        thread_id="sub_idx_2",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("tc_app_idx_2", "knowledge_search", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    calls=(
                        BeginToolCallBatchItem(
                            record_id="call_rec_idx_2",
                            tool_call_id="tc_app_idx_2",
                            model_name="knowledge_search",
                            tool_id=KNOWLEDGE_SEARCH_ID,
                            tool_kind=ToolKind.BUILTIN,
                            requires_approval=True,
                        ),
                    ),
                    requested_at=NOW,
                    round=1,
                )
            )
            details_after = await service.get("sess_1", "usr_1", "run_main")
            assert len(details_after.pending_subagent_approvals) == 1
            approval = details_after.pending_subagent_approvals[0]
            assert approval.sub_run_id == "sub_idx_2"
            assert approval.task_index == 2
            assert approval.task_summary == long_task[:119] + "…"

            dto = run_details_to_response(details_after)
            assert dto.subagent_tasks[0].task_index == 1
            assert dto.subagent_tasks[0].task_summary == "分析第一季度 华东区营收"
            assert dto.pending_subagent_inputs[0].task_index == 1
            assert dto.pending_subagent_approvals[0].task_index == 2
            assert dto.pending_subagent_approvals[0].task_summary == long_task[:119] + "…"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_subagent_input_and_answer_isolated_from_conversation_and_main_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子 Run 的提问与回答必须彻底隔离出主聊天历史与主 Run 上下文。"""

    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        msg_repo = SqlAlchemyMessageRepository(factory)
        try:
            lease = await _submit_main_run(repo)
            items = (
                BeginSubagentBatchItem(
                    sub_run_id="sub_iso_1",
                    subagent_profile_id="sub_prof_1",
                    subagent_slug="researcher",
                    subagent_name="研究员",
                    system_prompt="你是研究员",
                    model_profile_id="mp_1",
                    tool_snapshot=RuntimeToolSnapshot(tools=()),
                    skill_snapshot=None,
                    input_file_ids=(),
                    task_message=ExecutionMessageDraft(
                        id="exec_iso_task_1",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="sub_iso_1",
                        thread_id="sub_iso_1",
                        role=ExecutionRole.USER,
                        parts=(TextPart("子任务隔离检查"),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                ),
            )
            await repo.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id="run_main",
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    tool_call_id="call_iso",
                    call_message=ExecutionMessageDraft(
                        id="exec_call_iso",
                        session_id="sess_1",
                        owner_id="usr_1",
                        run_id="run_main",
                        thread_id="run_main",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_iso", "dispatch_subagents", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    record_id="tc_iso",
                    items=items,
                    requested_at=NOW,
                )
            )
            claimed = await repo.claim("w-iso", lease_seconds=60, execution_seconds=300, limit=1)
            assert len(claimed) == 1
            sub_lease = claimed[0]

            # 子 Run 提问
            await repo.request_input(
                RequestRunInputCommand(
                    run_id="sub_iso_1",
                    worker_id=sub_lease.run.worker_id or "",
                    execution_epoch=sub_lease.run.execution_epoch,
                    input_request_id="inp_iso_1",
                    question_message=ConversationMessageDraft(
                        id="msg_q_iso_1",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("子任务内部提问：需要哪个年份？"),),
                        reply_to_message_id=sub_lease.run.trigger_message_id,
                        run_id="sub_iso_1",
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id=sub_lease.run.trigger_message_id,
                    ),
                    execution_messages=(
                        ExecutionMessageDraft(
                            id="exec_q_iso_1",
                            session_id="sess_1",
                            owner_id="usr_1",
                            run_id="sub_iso_1",
                            thread_id="sub_iso_1",
                            role=ExecutionRole.ASSISTANT,
                            parts=(
                                ToolCallPart(
                                    "tc_q_iso_1",
                                    "request_user_input",
                                    {"question": "子任务内部提问：需要哪个年份？"},
                                ),
                            ),
                            tool_call_id=None,
                            source_message_id="msg_q_iso_1",
                            created_at=NOW,
                            metadata=MessageMetadata(),
                        ),
                    ),
                    usage=RunUsage(),
                    created_at=NOW,
                )
            )

            # 用户定向回答子 Run 前，子 Run 详情应包含待回答的 input_interactions
            service = RunService(repo, clock=lambda: NOW)
            pending_sub_details = await service.get("sess_1", "usr_1", "sub_iso_1")
            assert len(pending_sub_details.run.input_interactions) == 1
            assert (
                pending_sub_details.run.input_interactions[0].status is InputRequestStatus.PENDING
            )
            assert (
                pending_sub_details.run.input_interactions[0].question_text
                == "子任务内部提问：需要哪个年份？"
            )
            assert pending_sub_details.run.input_interactions[0].answer_text is None

            # 用户定向回答子 Run
            answered_sub = await service.answer(
                "sess_1", "usr_1", "sub_iso_1", "inp_iso_1", "req_ans_iso_1", "2025年"
            )
            assert answered_sub.status is RunStatus.QUEUED

            # 子 Run 详情应可回看完整的 question_text 与 answer_text（包括 DTO 序列化）
            from api.converter.run import run_details_to_response

            sub_details_after_answer = await service.get("sess_1", "usr_1", "sub_iso_1")
            assert len(sub_details_after_answer.run.input_interactions) == 1
            interaction = sub_details_after_answer.run.input_interactions[0]
            assert interaction.input_request_id == "inp_iso_1"
            assert interaction.status is InputRequestStatus.ANSWERED
            assert interaction.question_text == "子任务内部提问：需要哪个年份？"
            assert interaction.answer_text == "2025年"
            assert interaction.answered_at is not None

            sub_dto = run_details_to_response(sub_details_after_answer)
            assert len(sub_dto.input_interactions) == 1
            assert sub_dto.input_interactions[0].question_text == "子任务内部提问：需要哪个年份？"
            assert sub_dto.input_interactions[0].answer_text == "2025年"
            assert sub_dto.input_interactions[0].status == "answered"

            # 1. 主聊天历史不包含子 Run 的提问与回答，且分页不会误判 has_more
            page = await msg_repo.list_conversation_for_owner(
                "sess_1", "usr_1", before_sequence=None, limit=10
            )
            assert page is not None
            assert [m.id for m in page.items] == ["msg_trigger_run_main"]
            assert page.next_before_sequence is None

            # 在 before_sequence=1（没有任何可见主消息）时，仍返回空页而非 None
            empty_page = await msg_repo.list_conversation_for_owner(
                "sess_1", "usr_1", before_sequence=1, limit=10
            )
            assert empty_page is not None
            assert empty_page.items == ()
            assert empty_page.next_before_sequence is None

            # 2. 子 Run 完成后唤醒主 Run，主 Run 上下文不包含子 Run 的提问与回答
            claimed_sub2 = await repo.claim(
                "w-iso-2", lease_seconds=60, execution_seconds=300, limit=1
            )
            assert len(claimed_sub2) == 1
            await repo.finish(
                FinishRunCommand(
                    run_id="sub_iso_1",
                    worker_id=claimed_sub2[0].run.worker_id or "",
                    execution_epoch=claimed_sub2[0].run.execution_epoch,
                    reply_message=ConversationMessageDraft(
                        id="msg_sub_final",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("子任务完成结论"),),
                        reply_to_message_id=claimed_sub2[0].run.trigger_message_id,
                        run_id="sub_iso_1",
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id=claimed_sub2[0].run.trigger_message_id,
                    ),
                    usage=RunUsage(),
                    finished_at=NOW,
                )
            )
            main_ctx = await repo.list_context_messages("sess_1", "usr_1", "run_main")
            assert main_ctx is not None
            main_ctx_ids = {m.id for m in main_ctx}
            assert "msg_q_iso_1" not in main_ctx_ids
            assert "msg_sub_final" not in main_ctx_ids
            assert not any(
                isinstance(m.parts[0], TextPart) and m.parts[0].text == "2025年"
                for m in main_ctx
                if m.id != "msg_trigger_run_main"
            )

            # 3. 结束主 Run 后，在同一会话提交后续新 Run，新 Run 上下文也不包含子 Run 消息
            claimed_main2 = await repo.claim(
                "w-iso-main", lease_seconds=60, execution_seconds=300, limit=1
            )
            assert len(claimed_main2) == 1 and claimed_main2[0].run.id == "run_main"
            await repo.finish(
                FinishRunCommand(
                    run_id="run_main",
                    worker_id=claimed_main2[0].run.worker_id or "",
                    execution_epoch=claimed_main2[0].run.execution_epoch,
                    reply_message=ConversationMessageDraft(
                        id="msg_main_final",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("主任务汇总完成"),),
                        reply_to_message_id="msg_trigger_run_main",
                        run_id="run_main",
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id="msg_trigger_run_main",
                    ),
                    usage=RunUsage(),
                    finished_at=NOW,
                )
            )
            next_submitted = await repo.submit(
                "sess_1",
                "usr_1",
                SubmitRunCommand(
                    run_id="run_next",
                    request_id="req_next",
                    fingerprint="fp_next",
                    prompt_template_version="v1",
                    message=ConversationMessageDraft(
                        id="msg_trigger_next",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.USER,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("继续下一步"),),
                        reply_to_message_id=None,
                        run_id="run_next",
                        client_message_id="cmsg_next",
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id="msg_trigger_next",
                    ),
                    queued_at=NOW,
                ),
            )
            next_ctx = await repo.list_context_messages("sess_1", "usr_1", next_submitted.run.id)
            assert next_ctx is not None
            next_ctx_ids = [m.id for m in next_ctx]
            assert next_ctx_ids == ["msg_trigger_run_main", "msg_main_final", "msg_trigger_next"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_subagent_skips_compression_policy_and_avoids_trigger_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 Run 配置压缩策略时，子 Run 不继承策略且跳过压缩准备，不报 trigger_missing。"""

    async def scenario() -> None:
        from domain.llm.model.compression_policy import CompressionPolicyVersion
        from domain.llm.model.profile import ModelProfileVersion

        engine, factory, repo = await _setup_db(monkeypatch)
        sub_repo = SqlAlchemySubagentRepository(factory)
        try:
            lease = await _submit_main_run(repo)
            # 为主 Run 绑定压缩策略版本，模拟真实生产/模型测试配置
            async with factory() as s, s.begin():
                main_row = await s.get(RunModel, "run_main")
                assert main_row is not None
                main_row.compression_policy_version_id = "cp_1"
            claimed_main = await repo.get("sess_1", "usr_1", "run_main")
            assert claimed_main is not None
            lease = RunLease(
                run=claimed_main,
                lease_expires_at=lease.lease_expires_at,
                execution_deadline_at=lease.execution_deadline_at,
            )

            tool_runtime = RunToolRuntimeService(
                auth=_StubAuthReader(),  # type: ignore[arg-type]
                sessions=SqlAlchemySessionRepository(factory),
                mcp_catalog=_StubMcpCatalog(),  # type: ignore[arg-type]
                mcp_executor=_StubMcpExecutor(),  # type: ignore[arg-type]
                knowledge=None,
                project_knowledge=None,
                subagents=sub_repo,
            )
            provider = _FakeProvider(
                [
                    # 1. 主 Run 派发 2 个子任务
                    LlmResponse(
                        status="completed",
                        output=(
                            LlmToolCall(
                                "call_disp_cp",
                                "dispatch_subagents",
                                {
                                    "tasks": [
                                        {"subagent_slug": "researcher", "task": "任务一"},
                                        {"subagent_slug": "researcher", "task": "任务二"},
                                    ]
                                },
                            ),
                        ),
                        usage=LlmUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                        model="gpt-4o",
                        incomplete_reason=None,
                        response_id="resp_main",
                        request_id="req_main",
                    ),
                    # 2. 子任务 1 触发 request_user_input
                    LlmResponse(
                        status="completed",
                        output=(
                            LlmToolCall(
                                "call_sub_q1",
                                "request_user_input",
                                {"question": "请确认时间范围？"},
                            ),
                        ),
                        usage=LlmUsage(input_tokens=8, output_tokens=4, total_tokens=12),
                        model="gpt-4o",
                        incomplete_reason=None,
                        response_id="resp_sub1",
                        request_id="req_sub1",
                    ),
                    # 3. 子任务 2（即使携带遗留压缩策略）直接完成
                    LlmResponse(
                        status="completed",
                        output=(LlmText("任务二分析完成"),),
                        usage=LlmUsage(input_tokens=8, output_tokens=4, total_tokens=12),
                        model="gpt-4o",
                        incomplete_reason=None,
                        response_id="resp_sub2",
                        request_id="req_sub2",
                    ),
                ]
            )
            cipher = McpSecretCipher(b"0" * 32, key_version=1)
            executor = RunExecutor(
                repo,
                SqlAlchemyModelProfileRepository(factory, cipher),
                provider,  # type: ignore[arg-type]
                system_prompts=FakeSystemPromptRepository(),  # type: ignore[arg-type]
                tool_runtime=tool_runtime,
                subagents=sub_repo,
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )

            async def _get_runtime(version_id: str) -> RuntimeModelProfile:
                return RuntimeModelProfile(
                    version=ModelProfileVersion(
                        id=version_id,
                        name="Default",
                        protocol="chat_completions",
                        base_url="https://api.example.com/v1",
                        model="gpt-4o",
                        context_window_tokens=128000,
                        max_output_tokens=4096,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        supports_parallel_tool_calls=True,
                        request_timeout_seconds=60,
                        execution_timeout_seconds=300,
                        max_concurrency=4,
                        temperature=0.0,
                        has_api_key=True,
                        enabled=True,
                        created_at=NOW,
                    ),
                    api_key="sk-test",
                )

            async def _get_compression_policy(
                policy_id: str,
            ) -> CompressionPolicyVersion | None:
                if policy_id != "cp_1":
                    return None
                return CompressionPolicyVersion(
                    id="cp_1",
                    compression_model_profile_id="mp_1",
                    trigger_tokens=80000,
                    recent_ratio=0.3,
                    recent_token_cap=24000,
                    summary_ratio=0.2,
                    summary_token_cap=16000,
                    prompt_template_version="v1",
                    created_at=NOW,
                )

            executor._profiles.get_runtime = _get_runtime  # type: ignore[method-assign]
            executor._profiles.get_compression_policy = (  # type: ignore[method-assign]
                _get_compression_policy
            )

            # 执行主 Run：派发子任务
            await executor.execute(lease)
            tasks = await repo.list_subagent_tasks("sess_1", "usr_1", "run_main")
            assert len(tasks) == 2

            # 验证创建出的子 Run 未继承 compression_policy_version_id
            sub1 = await repo.get("sess_1", "usr_1", tasks[0].sub_run_id)
            sub2 = await repo.get("sess_1", "usr_1", tasks[1].sub_run_id)
            assert sub1 is not None and sub1.compression_policy_version_id is None
            assert sub2 is not None and sub2.compression_policy_version_id is None

            # 强行给 sub2 写入 compression_policy_version_id，验证 executor 的 depth >= 1 兜底保护
            async with factory() as s, s.begin():
                sub2_row = await s.get(RunModel, sub2.id)
                assert sub2_row is not None
                sub2_row.compression_policy_version_id = "cp_1"

            claimed_subs = await repo.claim(
                "w-cp", lease_seconds=60, execution_seconds=300, limit=2
            )
            assert len(claimed_subs) == 2
            assert {s.run.id for s in claimed_subs} == {sub1.id, sub2.id}
            for sub_lease in claimed_subs:
                await executor.execute(sub_lease)

            # claim 顺序按 (queued_at, id) 决定，不假设与 list_subagent_tasks 顺序一致：
            # 第 1 个被领取的子 Run 消费 request_user_input 响应，第 2 个消费完成文本响应。
            first_after = await repo.get("sess_1", "usr_1", claimed_subs[0].run.id)
            second_after = await repo.get("sess_1", "usr_1", claimed_subs[1].run.id)
            assert first_after is not None
            assert first_after.status is RunStatus.WAITING_FOR_INPUT
            assert first_after.safe_error_code is None
            assert second_after is not None
            assert second_after.status is RunStatus.COMPLETED
            assert second_after.safe_error_code is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_subagent_artifact_finishes_without_delivery_message_and_parent_summarizes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子 Run 产出产物时不写公开交付消息可正常落库，并在唤醒父 Run 时正确汇总。"""
    from domain.file import PendingSessionFile
    from domain.message import ArtifactFileRef, FileRefPart, ToolResultPart, ToolResultStatus
    from domain.tool import (
        AdmitToolCallCommand,
        FinishToolCallCommand,
        ToolOutcome,
        ToolOutcomeStatus,
    )

    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            main_lease = await _submit_main_run(repo, run_id="run_main_artifact")
            sub_run_id = "run_sub_artifact"
            await repo.begin_subagent_batch(
                _subagent_batch_command(main_lease, sequence=1, sub_run_id=sub_run_id)
            )
            sub_leases = await repo.claim(
                "worker-sub", lease_seconds=60, execution_seconds=300, limit=1
            )
            assert len(sub_leases) == 1
            sub_lease = sub_leases[0]
            assert sub_lease.run.id == sub_run_id
            assert sub_lease.run.depth == 1

            call_msg = ExecutionMessageDraft(
                id="exec_sub_call_1",
                session_id="sess_1",
                owner_id="usr_1",
                run_id=sub_run_id,
                thread_id=sub_run_id,
                role=ExecutionRole.ASSISTANT,
                parts=(ToolCallPart("tc_sub_1", "artifact_helper", {"script": "run.py"}),),
                tool_call_id=None,
                source_message_id=None,
                created_at=NOW,
                metadata=MessageMetadata(),
            )
            await repo.begin_tool_call_batch(
                BeginToolCallBatchCommand(
                    run_id=sub_run_id,
                    worker_id=sub_lease.run.worker_id or "",
                    execution_epoch=sub_lease.run.execution_epoch,
                    call_message=call_msg,
                    calls=(
                        BeginToolCallBatchItem(
                            record_id="rec_sub_1",
                            tool_call_id="tc_sub_1",
                            model_name="artifact_helper",
                            tool_id="builtin_test_artifact",
                            tool_kind=ToolKind.BUILTIN,
                            requires_approval=False,
                        ),
                    ),
                    requested_at=NOW,
                )
            )
            admitted = await repo.admit_tool_call(
                AdmitToolCallCommand(
                    run_id=sub_run_id,
                    worker_id=sub_lease.run.worker_id or "",
                    execution_epoch=sub_lease.run.execution_epoch,
                    record_id="rec_sub_1",
                    admitted_at=NOW,
                )
            )
            assert admitted is not None

            artifact = PendingSessionFile(
                id="file_sub_art_1",
                filename="summary.csv",
                label="调研报表",
                mime_type="text/csv",
                size_bytes=128,
                sha256="c" * 64,
                object_key="session-artifacts/cc/rec_sub_1/file_sub_art_1",
                output_index=0,
                created_at=NOW,
            )
            outcome = ToolOutcome(
                status=ToolOutcomeStatus.OK,
                code="skill_execution_succeeded",
                message="脚本执行成功。",
                data={"artifact_count": 1},
            )
            result_msg = ExecutionMessageDraft(
                id="exec_sub_res_1",
                session_id="sess_1",
                owner_id="usr_1",
                run_id=sub_run_id,
                thread_id=sub_run_id,
                role=ExecutionRole.TOOL,
                parts=(
                    ToolResultPart(
                        tool_call_id="tc_sub_1",
                        status=ToolResultStatus.SUCCESS,
                        output=outcome.canonical_output(),
                        artifacts=(
                            ArtifactFileRef(
                                file_id=artifact.id,
                                filename=artifact.filename,
                                label=artifact.label,
                                mime_type=artifact.mime_type,
                                size_bytes=artifact.size_bytes,
                            ),
                        ),
                    ),
                ),
                tool_call_id="tc_sub_1",
                source_message_id=None,
                created_at=NOW,
                metadata=MessageMetadata(),
            )
            # 子 Run 错误传入 delivery_message 时必须被明确拒绝。
            bad_delivery = ConversationMessageDraft(
                id="msg_bad_sub_delivery",
                session_id="sess_1",
                owner_id="usr_1",
                role=ConversationRole.ASSISTANT,
                status=MessageStatus.COMPLETED,
                parts=(
                    FileRefPart(
                        file_id=artifact.id,
                        filename=artifact.filename,
                        mime_type=artifact.mime_type,
                        size_bytes=artifact.size_bytes,
                    ),
                ),
                reply_to_message_id=sub_lease.run.trigger_message_id,
                run_id=sub_run_id,
                client_message_id=None,
                created_at=NOW,
                completed_at=NOW,
                metadata=MessageMetadata(),
                context_turn_id=sub_lease.run.trigger_message_id,
            )
            with pytest.raises(RunStateError, match="run_tool_delivery_message_invalid"):
                await repo.finish_tool_call(
                    FinishToolCallCommand(
                        run_id=sub_run_id,
                        worker_id=sub_lease.run.worker_id or "",
                        execution_epoch=sub_lease.run.execution_epoch,
                        record_id="rec_sub_1",
                        result_message=result_msg,
                        outcome=outcome,
                        finished_at=NOW,
                        artifacts=(artifact,),
                        delivery_message=bad_delivery,
                    )
                )

            # 子 Run delivery_message=None 正常落库，并支持幂等重放。
            finish_cmd = FinishToolCallCommand(
                run_id=sub_run_id,
                worker_id=sub_lease.run.worker_id or "",
                execution_epoch=sub_lease.run.execution_epoch,
                record_id="rec_sub_1",
                result_message=result_msg,
                outcome=outcome,
                finished_at=NOW,
                artifacts=(artifact,),
                delivery_message=None,
            )
            finished = await repo.finish_tool_call(finish_cmd)
            assert finished is not None
            replayed = await repo.finish_tool_call(finish_cmd)
            assert replayed is not None

            # 完成子 Run 唤醒父 Run，校验父 Run 汇总结果包含该产物。
            await repo.finish(
                FinishRunCommand(
                    run_id=sub_run_id,
                    worker_id=sub_lease.run.worker_id or "",
                    execution_epoch=sub_lease.run.execution_epoch,
                    reply_message=ConversationMessageDraft(
                        id="msg_sub_final_ignored",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("子任务完成，已生成报表。"),),
                        reply_to_message_id=sub_lease.run.trigger_message_id,
                        run_id=sub_run_id,
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id=sub_lease.run.trigger_message_id,
                    ),
                    usage=RunUsage(),
                    finished_at=NOW,
                )
            )
            async with factory() as session:
                stored_file = await session.get(SessionFileModel, artifact.id)
                assert stored_file is not None
                parent_tool_msg = await session.scalar(
                    select(ExecutionMessageModel).where(
                        ExecutionMessageModel.run_id == "run_main_artifact",
                        ExecutionMessageModel.role == ExecutionRole.TOOL.value,
                    )
                )
                assert parent_tool_msg is not None
                summary = json.loads(parent_tool_msg.parts[0]["output"])
                assert summary["completed_count"] == 1
                assert summary["results"][0]["artifacts"] == [
                    {
                        "file_id": "file_sub_art_1",
                        "filename": "summary.csv",
                        "label": "调研报表",
                        "mime_type": "text/csv",
                        "size_bytes": 128,
                    }
                ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_subagent_max_llm_requests_50_runs_without_budget_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置 max_llm_requests=50 的子 Run 可正常通过 RunLlmBudget 并完成执行。"""
    from app.run.budget import RunLlmBudget
    from domain.llm.model.profile import ModelProfileVersion
    from domain.tool import ToolExecutionContext

    async def scenario() -> None:
        engine, factory, repo = await _setup_db(monkeypatch)
        try:
            main_lease = await _submit_main_run(repo, run_id="run_main_budget50")
            sub_run_id = "run_sub_budget50"
            item = _subagent_batch_item(sub_run_id)
            item_with_50 = BeginSubagentBatchItem(
                sub_run_id=item.sub_run_id,
                subagent_profile_id=item.subagent_profile_id,
                subagent_slug=item.subagent_slug,
                subagent_name=item.subagent_name,
                system_prompt=item.system_prompt,
                model_profile_id=item.model_profile_id,
                tool_snapshot=item.tool_snapshot,
                skill_snapshot=item.skill_snapshot,
                input_file_ids=item.input_file_ids,
                task_message=item.task_message,
                max_llm_requests=50,
            )
            base_cmd = _subagent_batch_command(main_lease, sequence=1, sub_run_id=sub_run_id)
            await repo.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id=base_cmd.run_id,
                    worker_id=base_cmd.worker_id,
                    execution_epoch=base_cmd.execution_epoch,
                    tool_call_id=base_cmd.tool_call_id,
                    call_message=base_cmd.call_message,
                    record_id=base_cmd.record_id,
                    items=(item_with_50,),
                    requested_at=base_cmd.requested_at,
                )
            )
            sub_leases = await repo.claim(
                "worker-50", lease_seconds=60, execution_seconds=300, limit=1
            )
            assert len(sub_leases) == 1
            sub_lease = sub_leases[0]
            assert sub_lease.run.max_llm_requests == 50

            # 验证 RunLlmBudget 允许 50 上限且仍拒绝 51。
            ctx = ToolExecutionContext(
                owner_id="usr_1",
                chat_session_id="sess_1",
                project_id=None,
                run_id=sub_run_id,
                execution_epoch=sub_lease.run.execution_epoch,
                auth_session_id="auth_1",
                deadline_at=sub_lease.execution_deadline_at,
            )
            budget = RunLlmBudget(repo, ctx, used=0, maximum=50, worker_id="worker-50")
            assert budget.maximum == 50
            with pytest.raises(ValueError, match="run_llm_request_limit_invalid"):
                RunLlmBudget(repo, ctx, used=0, maximum=51, worker_id="worker-50")

            # 实际通过 RunExecutor 运行该子 Run，确认不触发预算校验崩溃。
            cipher = McpSecretCipher(b"k" * 32, key_version=1)
            profiles = SqlAlchemyModelProfileRepository(factory, cipher)

            async def _get_runtime(profile_id: str) -> RuntimeModelProfile | None:
                del profile_id
                return RuntimeModelProfile(
                    version=ModelProfileVersion(
                        id="mp_1",
                        name="Default GPT",
                        protocol="chat_completions",
                        base_url="https://api.example.com/v1",
                        model="gpt-4o",
                        context_window_tokens=128000,
                        max_output_tokens=4096,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        supports_parallel_tool_calls=True,
                        request_timeout_seconds=60,
                        execution_timeout_seconds=300,
                        max_concurrency=4,
                        temperature=0.0,
                        has_api_key=True,
                        enabled=True,
                        created_at=NOW,
                    ),
                    api_key="sk-test",
                )

            profiles.get_runtime = _get_runtime  # type: ignore[method-assign]
            provider = _FakeProvider(
                [
                    LlmResponse(
                        output=(LlmText("budget 50 completed"),),
                        status="completed",
                        incomplete_reason=None,
                        model="gpt-4o",
                        response_id="resp_50",
                        request_id=None,
                        usage=LlmUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                    )
                ]
            )
            executor = RunExecutor(
                repo,
                profiles,
                provider,  # type: ignore[arg-type]
                system_prompts=FakeSystemPromptRepository(),
                config=RunWorkerConfig(),
                clock=lambda: NOW,
            )
            await executor.execute(sub_lease)

            finished_sub = await repo.get("sess_1", "usr_1", sub_run_id)
            assert finished_sub is not None
            assert finished_sub.status is RunStatus.COMPLETED
            assert finished_sub.llm_request_count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_dispatch_subagents_catalog_bounds_description_and_preserves_newlines() -> None:
    """启用 20+ 长描述 Subagent 时，工具快照描述不超 8192 字符且保留多行换行结构。"""

    async def scenario() -> None:
        profiles = tuple(
            SubagentProfile(
                id=f"sub_{index}",
                slug=f"agent-{index:02d}",
                name=f"专业子智能体{index:02d}",
                description=(
                    f"第一行职责{index}\r\n\t第二行细则：" + "详" * 960 + "\x00\x07"
                )[:1000],
                system_prompt="你是专业子智能体",
                model_profile_id=None,
                allowed_tool_ids=(REQUEST_USER_INPUT_ID,),
                enabled=True,
                max_llm_requests=50,
                timeout_seconds=120,
                version=1,
                created_by=None,
                deleted_at=None,
                created_at=NOW,
                updated_at=NOW,
            )
            for index in range(40)
        )
        runtime = RunToolRuntimeService(
            auth=_StubAuthReader(),  # type: ignore[arg-type]
            sessions=_StubSessionRepo(),  # type: ignore[arg-type]
            mcp_catalog=_StubMcpCatalog(),  # type: ignore[arg-type]
            mcp_executor=_StubMcpExecutor(),  # type: ignore[arg-type]
            knowledge=None,
            project_knowledge=None,
            subagents=_StubSubagentRepo(profiles),  # type: ignore[arg-type]
        )
        ctx = AuthorizedRunContext(
            owner_id="usr_1",
            chat_session_id="sess_1",
            project_id=None,
            auth_session_id="auth_1",
        )
        snapshot = await runtime.build_snapshot("run-v2", ctx, depth=0)
        dispatch_def = snapshot.by_model_name("dispatch_subagents")
        assert dispatch_def is not None
        assert len(dispatch_def.description) <= 8192
        assert "\n可用子智能体：\n- agent-00（专业子智能体00）：" in dispatch_def.description
        assert "\t第二行细则：" in dispatch_def.description
        assert "\r" not in dispatch_def.description
        assert "\x00" not in dispatch_def.description
        assert "\x07" not in dispatch_def.description
        assert "个子智能体已省略" in dispatch_def.description

    asyncio.run(scenario())


def test_get_stream_snapshot_deduplicates_subagent_metadata_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一主 Run 快照仅加载一次子任务元数据；无子任务时跳过下游 3 个查询。"""

    async def scenario() -> None:
        engine, _factory, repo = await _setup_db(monkeypatch)
        try:
            root_lease = await _submit_main_run(repo, "run_main_dedup")
            orig_meta = SqlAlchemyRunRepository._load_subagent_task_metadata_in_session
            orig_tasks = SqlAlchemyRunRepository._load_subagent_tasks_in_session
            orig_inputs = SqlAlchemyRunRepository._load_pending_subagent_inputs_in_session
            orig_approvals = SqlAlchemyRunRepository._load_pending_subagent_approvals_in_session
            counts = {"meta": 0, "tasks": 0, "inputs": 0, "approvals": 0}

            async def spy_meta(
                session: AsyncSession, session_id: str, parent_run_id: str
            ) -> tuple[dict[str, int], dict[str, str]]:
                counts["meta"] += 1
                return await orig_meta(session, session_id, parent_run_id)

            async def spy_tasks(
                session: AsyncSession,
                session_id: str,
                parent_run_id: str,
                *,
                metadata: tuple[dict[str, int], dict[str, str]] | None = None,
            ) -> Any:
                counts["tasks"] += 1
                return await orig_tasks(session, session_id, parent_run_id, metadata=metadata)

            async def spy_inputs(
                session: AsyncSession,
                session_id: str,
                parent_run_id: str,
                *,
                metadata: tuple[dict[str, int], dict[str, str]] | None = None,
            ) -> Any:
                counts["inputs"] += 1
                return await orig_inputs(session, session_id, parent_run_id, metadata=metadata)

            async def spy_approvals(
                session: AsyncSession,
                session_id: str,
                parent_run_id: str,
                *,
                metadata: tuple[dict[str, int], dict[str, str]] | None = None,
            ) -> Any:
                counts["approvals"] += 1
                return await orig_approvals(session, session_id, parent_run_id, metadata=metadata)

            monkeypatch.setattr(
                SqlAlchemyRunRepository,
                "_load_subagent_task_metadata_in_session",
                staticmethod(spy_meta),
            )
            monkeypatch.setattr(
                SqlAlchemyRunRepository,
                "_load_subagent_tasks_in_session",
                staticmethod(spy_tasks),
            )
            monkeypatch.setattr(
                SqlAlchemyRunRepository,
                "_load_pending_subagent_inputs_in_session",
                staticmethod(spy_inputs),
            )
            monkeypatch.setattr(
                SqlAlchemyRunRepository,
                "_load_pending_subagent_approvals_in_session",
                staticmethod(spy_approvals),
            )

            # 无子任务时只查 1 次元数据，后续 3 个子任务查询全部短路跳过。
            empty_snapshot = await repo.get_stream_snapshot("sess_1", "usr_1", root_lease.run.id)
            assert empty_snapshot is not None
            assert empty_snapshot.subagent_tasks == ()
            assert counts == {"meta": 1, "tasks": 0, "inputs": 0, "approvals": 0}

            # 派发一个子任务并进入待输入状态，验证元数据仍仅查询 1 次且三处摘要一致。
            await repo.begin_subagent_batch(
                _subagent_batch_command(root_lease, sequence=1, sub_run_id="run_sub_dedup_1")
            )
            sub_leases = await repo.claim(
                "worker-sub", lease_seconds=60, execution_seconds=300, limit=1
            )
            assert len(sub_leases) == 1
            sub_lease = sub_leases[0]
            await repo.request_input(
                RequestRunInputCommand(
                    run_id=sub_lease.run.id,
                    worker_id=sub_lease.run.worker_id or "",
                    execution_epoch=sub_lease.run.execution_epoch,
                    input_request_id="req_input_dedup",
                    question_message=ConversationMessageDraft(
                        id="msg_q_dedup",
                        session_id="sess_1",
                        owner_id="usr_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("请补充参数"),),
                        reply_to_message_id=sub_lease.run.trigger_message_id,
                        run_id=sub_lease.run.id,
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                        context_turn_id=sub_lease.run.trigger_message_id,
                    ),
                    execution_messages=(
                        ExecutionMessageDraft(
                            id="exec_ask_dedup",
                            session_id="sess_1",
                            owner_id="usr_1",
                            run_id=sub_lease.run.id,
                            thread_id=sub_lease.run.id,
                            role=ExecutionRole.ASSISTANT,
                            parts=(
                                ToolCallPart(
                                    tool_call_id="call_ask_1",
                                    name="request_user_input",
                                    arguments={"question": "请补充参数"},
                                ),
                            ),
                            tool_call_id=None,
                            source_message_id="msg_q_dedup",
                            created_at=NOW,
                            metadata=MessageMetadata(),
                        ),
                    ),
                    usage=RunUsage(),
                    created_at=NOW,
                )
            )

            counts = {"meta": 0, "tasks": 0, "inputs": 0, "approvals": 0}
            populated = await repo.get_stream_snapshot("sess_1", "usr_1", root_lease.run.id)
            assert populated is not None
            assert counts == {"meta": 1, "tasks": 1, "inputs": 1, "approvals": 1}
            assert len(populated.subagent_tasks) == 1
            assert populated.subagent_tasks[0].task_index == 1
            assert populated.subagent_tasks[0].task_summary == "任务 run_sub_dedup_1"
            assert len(populated.pending_subagent_inputs) == 1
            assert populated.pending_subagent_inputs[0].task_index == 1
            assert populated.pending_subagent_inputs[0].task_summary == "任务 run_sub_dedup_1"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
