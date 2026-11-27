"""M1.5 对话能力底座升级（知识库解耦、项目增强、会话松绑、场景化模型、自动标题与迁移）测试套件。"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from auth_support import FakePasswordHasher
from fastapi.testclient import TestClient
from pg_unit_harness import create_pg_unit_db
from sqlalchemy import DateTime, delete, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from system_prompt_support import (
    TEST_COMPRESSION_PROMPT_ID,
    TEST_MAIN_PROMPT_ID,
    FakeSystemPromptRepository,
)

from adapter.access import ResourceVisibilityGrantModel, SqlAlchemyAccessControlRepository
from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.knowledge.model import KnowledgeBaseModel, ProjectKnowledgeBaseModel
from adapter.knowledge.repository import KnowledgeRepository
from adapter.llm.model import (
    CompressionPolicyVersionModel,
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.org.model import DepartmentModel, UserDepartmentModel
from adapter.project.model import ProjectModel
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.run.model import RunModel
from adapter.run.repository.run import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from adapter.skill.model import SkillModel, SkillVersionModel
from adapter.subagent.model import SubagentModel
from adapter.subagent.repository import SqlAlchemySubagentRepository
from adapter.system_prompt.model import SystemPromptSettingModel, SystemPromptVersionModel
from app.access import AccessPolicyService
from app.auth import AuthService
from app.knowledge.service import KnowledgeService
from app.llm import ModelProfileService
from app.project.knowledge import ProjectKnowledgeService
from app.project.service import ProjectService
from app.run.auto_title import fallback_title_from_message, sanitize_generated_title
from app.run.config import RunWorkerConfig
from app.run.context import LlmRequestSpec, measure_llm_request
from app.run.executor import RunExecutor
from app.run.service import RunService
from app.session.service import SessionService
from app.tool.runtime import (
    KNOWLEDGE_SEARCH_DEFINITION,
    KNOWLEDGE_SEARCH_ID,
    RunToolRuntimeService,
)
from domain.access import GrantTargetType, ResourceType, VisibilityScope
from domain.auth import User, UserRole, UserStatus
from domain.knowledge.model import ChunkLocator, SearchHit, SearchResult
from domain.llm.model import (
    LlmProviderError,
    LlmRequest,
    LlmResponse,
    LlmText,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.mcp import McpCallResult, McpCallStatus, McpRuntimeToolTarget, McpSecretPurpose
from domain.org import DepartmentStatus
from domain.run.model import RunStatus
from domain.skill import SkillScope
from domain.tool import (
    AuthorizedRunContext,
    RuntimeToolDefinition,
    ToolExecutionContext,
    ToolOutcomeStatus,
)
from infrastructure.security.secrets import SecretCipher
from main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
NOW = datetime(2026, 12, 3, 10, 0, tzinfo=UTC)
SECRET_KEY = b"0" * 32


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步测试函数中运行协程。"""
    return asyncio.run(coroutine)


class _EmptyMcpCatalog:
    """返回空 MCP 目录的测试替身。"""

    async def list_runtime_tools(self, *, limit: int) -> tuple[RuntimeToolDefinition, ...]:
        del limit
        return ()

    async def list_readable_runtime_tools(
        self,
        snapshot: Any,
        *,
        limit: int = 200,
    ) -> tuple[RuntimeToolDefinition, ...]:
        del snapshot, limit
        return ()

    async def get_call_target(
        self,
        tool_id: str,
        server_id: str,
        remote_name: str,
        expected_config_version: int,
    ) -> McpRuntimeToolTarget | None:
        del tool_id, server_id, remote_name, expected_config_version
        return None


class _NoopMcpExecutor:
    """不会发起真实 MCP 调用的测试替身。"""

    async def call(
        self,
        target: McpRuntimeToolTarget,
        arguments: dict[str, object],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        del target, arguments, deadline_seconds
        return McpCallResult(McpCallStatus.OK, "ok")


class _StubAuthReader:
    """固定返回指定活跃用户的会话鉴权读取器。"""

    def __init__(self, users: dict[str, User]) -> None:
        self._users = users

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        del session_id, now
        return self._users.get(user_id)


class _RecordingKnowledgeService:
    """记录检索入参并按命中的知识库 ID 返回可配置结果的知识库服务替身。"""

    def __init__(
        self,
        *,
        on_search: Callable[[str, list[str], str], SearchResult] | None = None,
        before_return: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, list[str], str]] = []
        self._on_search = on_search
        self._before_return = before_return

    async def search(
        self,
        owner_id: str,
        base_ids: list[str],
        query: str,
        *,
        limit: int = 8,
        context_budget: int = 2000,
        skip_access_check: bool = False,
    ) -> SearchResult:
        del limit, context_budget, skip_access_check
        self.calls.append((owner_id, list(base_ids), query))
        if self._before_return is not None:
            await self._before_return()
        if self._on_search is not None:
            return self._on_search(owner_id, list(base_ids), query)
        hits = tuple(
            SearchHit(
                chunk_id=f"chunk_{base_id}",
                document_id=f"doc_{base_id}",
                version_id=f"ver_{base_id}",
                title=f"Doc {base_id}",
                text=f"Content from {base_id}",
                locators=(ChunkLocator(kind="page", page=1),),
                channels=("keyword",),
                rank=1.0,
                knowledge_base_id=base_id,
            )
            for base_id in base_ids
        )
        return SearchResult(hits=hits, channels=("keyword",))


class _ScriptedLlmProvider:
    """可控流式与非流式 LLM 供应商替身，用于验证指令注入、子 Run 与自动标题行为。"""

    def __init__(
        self,
        *,
        stream_responses: list[LlmResponse] | None = None,
        complete_handler: Callable[
            [RuntimeModelProfile, LlmRequest], Coroutine[Any, Any, LlmResponse]
        ]
        | None = None,
    ) -> None:
        self._stream_responses = list(stream_responses or [])
        self._complete_handler = complete_handler
        self.stream_requests: list[tuple[str, LlmRequest]] = []
        self.complete_requests: list[tuple[str, LlmRequest]] = []

    async def complete(self, runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
        self.complete_requests.append((runtime.version.id, request))
        if self._complete_handler is not None:
            return await self._complete_handler(runtime, request)
        return _text_response("自动生成标题")

    @asynccontextmanager
    async def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncIterator[AsyncIterator[ResponseCompleted]]:
        self.stream_requests.append((runtime.version.id, request))
        response = (
            self._stream_responses.pop(0)
            if self._stream_responses
            else _text_response("已收到并处理完成。")
        )

        async def _events() -> AsyncIterator[ResponseCompleted]:
            yield ResponseCompleted(response=response)

        yield _events()


def _text_response(text: str, *, model: str = "gpt-4o") -> LlmResponse:
    """构造标准 completed 文本响应。"""
    return LlmResponse(
        output=(LlmText(text),),
        status="completed",
        incomplete_reason=None,
        model=model,
        response_id="resp_1",
        request_id="req_1",
        usage=LlmUsage(input_tokens=60, output_tokens=15, total_tokens=75),
    )


def _tool_call_response(call_id: str, name: str, arguments: dict[str, Any]) -> LlmResponse:
    """构造单次工具调用响应。"""
    return LlmResponse(
        output=(LlmToolCall(call_id=call_id, name=name, arguments=arguments),),
        status="completed",
        incomplete_reason=None,
        model="gpt-4o",
        response_id="resp_tool",
        request_id="req_tool",
        usage=LlmUsage(input_tokens=50, output_tokens=20, total_tokens=70),
    )


async def _create_pg_db(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession], SecretCipher]:
    """获取隔离 PostgreSQL 单元测试库并注入单调递增的可控 clock_timestamp。"""
    tick = itertools.count()
    monkeypatch.setattr(
        func,
        "clock_timestamp",
        lambda: literal(NOW + timedelta(microseconds=next(tick)), type_=DateTime(timezone=True)),
    )
    engine, factory = await create_pg_unit_db()
    cipher = SecretCipher(SECRET_KEY, key_version=1)
    return engine, factory, cipher


async def _seed_base_org_and_prompts(
    factory: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
) -> dict[str, User]:
    """预置超管、研发成员、财务成员、部门树、系统提示词与默认模型配置。"""
    users: dict[str, User] = {}
    async with factory() as session, session.begin():
        # 按 PostgreSQL 外键顺序分批落库：根部门 -> 子部门/提示词版本/用户 -> 会话/成员/指针。
        session.add(
            DepartmentModel(
                id="dept_root",
                external_id="ext_root",
                name="总公司",
                parent_id=None,
                path="/dept_root/",
                order_index=0,
                status=DepartmentStatus.ACTIVE.value,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.flush()
        session.add_all(
            [
                DepartmentModel(
                    id="dept_rd",
                    external_id="ext_rd",
                    name="研发部",
                    parent_id="dept_root",
                    path="/dept_root/dept_rd/",
                    order_index=1,
                    status=DepartmentStatus.ACTIVE.value,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                DepartmentModel(
                    id="dept_fin",
                    external_id="ext_fin",
                    name="财务部",
                    parent_id="dept_root",
                    path="/dept_root/dept_fin/",
                    order_index=2,
                    status=DepartmentStatus.ACTIVE.value,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                SystemPromptVersionModel(
                    id=TEST_MAIN_PROMPT_ID,
                    kind="main",
                    content="平台系统主提示词",
                    created_at=NOW,
                ),
                SystemPromptVersionModel(
                    id=TEST_COMPRESSION_PROMPT_ID,
                    kind="compression",
                    content="平台摘要压缩提示词",
                    created_at=NOW,
                ),
            ]
        )
        for uid, email, role, display in (
            ("usr_admin", "admin@example.com", UserRole.SUPER_ADMIN, "超管"),
            ("usr_rd", "rd@example.com", UserRole.MEMBER, "研发员工"),
            ("usr_fin", "fin@example.com", UserRole.MEMBER, "财务员工"),
        ):
            session.add(
                UserModel(
                    id=uid,
                    email=email,
                    password_hash="fake:pass1234",
                    display_name=display,
                    role=role.value,
                    status=UserStatus.ACTIVE.value,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            users[uid] = User(
                id=uid,
                email=email,
                password_hash="fake:pass1234",
                display_name=display,
                role=role,
                status=UserStatus.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
            )
        await session.flush()
        for uid in ("usr_admin", "usr_rd", "usr_fin"):
            session.add(
                AuthSessionModel(
                    id=f"auth_{uid}",
                    user_id=uid,
                    token_hash=f"hash_{uid}",
                    expires_at=NOW + timedelta(days=7),
                    created_at=NOW,
                )
            )
        session.add_all(
            [
                UserDepartmentModel(
                    user_id="usr_rd",
                    department_id="dept_rd",
                    is_primary=True,
                ),
                UserDepartmentModel(
                    user_id="usr_fin",
                    department_id="dept_fin",
                    is_primary=True,
                ),
                SystemPromptSettingModel(
                    kind="main",
                    current_version_id=TEST_MAIN_PROMPT_ID,
                ),
                SystemPromptSettingModel(
                    kind="compression",
                    current_version_id=TEST_COMPRESSION_PROMPT_ID,
                ),
            ]
        )
        await session.flush()
        await _insert_model_profile(
            session,
            cipher,
            profile_id="llmprof_default",
            name="默认通用模型",
            model="gpt-4o",
            visible_to_users=True,
            scenario_label="日常通用",
            scenario_description="适合常规办公问答与知识检索",
            supports_vision=False,
            is_default=True,
        )
    return users


async def _insert_model_profile(
    session: AsyncSession,
    cipher: SecretCipher,
    *,
    profile_id: str,
    name: str,
    model: str,
    enabled: bool = True,
    visible_to_users: bool = True,
    supports_stream: bool = True,
    supports_tools: bool = True,
    scenario_label: str | None = None,
    scenario_description: str | None = None,
    supports_vision: bool = False,
    deleted_at: datetime | None = None,
    is_default: bool = False,
    created_at: datetime = NOW,
) -> None:
    """直接写入加密后的模型配置记录，供单元测试夹具初始化。"""
    sealed = cipher.seal(profile_id, McpSecretPurpose.LLM_API_KEY, {"api_key": "sk-secret-key"})
    session.add(
        ModelProfileVersionModel(
            id=profile_id,
            name=name,
            protocol="chat_completions",
            base_url="https://api.example.invalid/v1",
            model=model,
            api_key_ciphertext=sealed.ciphertext,
            api_key_nonce=sealed.nonce,
            api_key_key_version=sealed.key_version,
            context_window_tokens=16_384,
            tokenizer_encoding="cl100k_base",
            supports_stream=supports_stream,
            supports_tools=supports_tools,
            supports_parallel_tool_calls=False,
            max_output_tokens=2048,
            request_timeout_seconds=60,
            execution_timeout_seconds=120,
            max_concurrency=4,
            temperature=0.2,
            enabled=enabled,
            visible_to_users=visible_to_users,
            scenario_label=scenario_label,
            scenario_description=scenario_description,
            supports_vision=supports_vision,
            deleted_at=deleted_at,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    if is_default:
        await session.flush()
        await session.merge(DefaultModelProfileModel(singleton_id=1, profile_id=profile_id))


async def _seed_knowledge_base(
    session: AsyncSession,
    *,
    kb_id: str,
    owner_id: str,
    name: str,
    scope: VisibilityScope,
    owner_department_id: str | None = None,
    grant_department_id: str | None = None,
) -> None:
    """写入测试用知识库及其可选部门授权记录。"""
    session.add(
        KnowledgeBaseModel(
            id=kb_id,
            owner_id=owner_id,
            name=name,
            description=None,
            visibility_scope=scope.value,
            owner_department_id=owner_department_id,
            created_at=NOW,
            updated_at=NOW,
            deleted_time=None,
        )
    )
    if scope is VisibilityScope.DEPARTMENT and grant_department_id is not None:
        session.add(
            ResourceVisibilityGrantModel(
                id=f"rvg_{kb_id}_{grant_department_id}",
                resource_type=ResourceType.KNOWLEDGE_BASE.value,
                resource_id=kb_id,
                target_type=GrantTargetType.DEPARTMENT.value,
                target_id=grant_department_id,
                include_subdepartments=True,
                created_at=NOW,
            )
        )


async def _seed_skill(
    session: AsyncSession,
    *,
    skill_id: str,
    slug: str,
    scope: SkillScope,
    owner_id: str | None = None,
    owner_department_id: str | None = None,
    grant_department_id: str | None = None,
    enabled: bool = True,
    deleted_at: datetime | None = None,
) -> None:
    """写入测试用 Skill 与不可变版本，供项目默认技能回退校验。"""
    version_id = f"skver_{skill_id}"
    session.add(
        SkillModel(
            id=skill_id,
            slug=slug,
            scope=scope.value,
            owner_id=owner_id,
            owner_department_id=owner_department_id,
            format="markdown" if scope is SkillScope.PERSONAL else "package",
            featured=False,
            enabled=enabled,
            script_requires_approval=False,
            current_version_id=None,
            revision=1,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=deleted_at,
        )
    )
    await session.flush()
    session.add(
        SkillVersionModel(
            id=version_id,
            skill_id=skill_id,
            ordinal=1,
            label="v1",
            guide=f"# {slug}\n指南内容",
            object_key=f"skills/{skill_id}/v1.zip",
            package_sha256="a" * 64,
            manifest={
                "version": 1,
                "slug": slug,
                "name": f"技能 {slug}",
                "description": f"{slug} 说明",
                "dependencies": [],
                "guide": f"# {slug}\n指南内容",
                "scripts": [],
                "files": [
                    {
                        "path": "SKILL.md",
                        "sha256": "b" * 64,
                        "size_bytes": 16,
                        "text": True,
                    }
                ],
            },
            dependency_requirements=[],
            dependency_check={
                "status": "satisfied",
                "python_version": "3.12.0",
                "python_implementation": "CPython",
                "items": [],
                "checked_at": NOW.isoformat(),
            },
            created_by=owner_id or "usr_admin",
            request_operation="personal_create" if scope is SkillScope.PERSONAL else "import",
            idempotency_key=f"idem_{skill_id}",
            request_fingerprint="f" * 64,
            request_target_skill_id=None,
            created_at=NOW,
        )
    )
    await session.flush()
    await session.execute(
        update(SkillModel).where(SkillModel.id == skill_id).values(current_version_id=version_id)
    )
    if scope is SkillScope.DEPARTMENT and grant_department_id is not None:
        session.add(
            ResourceVisibilityGrantModel(
                id=f"rvg_{skill_id}_{grant_department_id}",
                resource_type=ResourceType.SKILL.value,
                resource_id=skill_id,
                target_type=GrantTargetType.DEPARTMENT.value,
                target_id=grant_department_id,
                include_subdepartments=True,
                created_at=NOW,
            )
        )


def _login_client(app: Any, email: str) -> TestClient:
    """创建已登录指定测试账号的 TestClient。"""
    client = TestClient(app)
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": "pass1234"})
    assert resp.status_code == 200, resp.text
    return client


# ==============================================================================
# 1. 无项目知识库解耦、显式限定、企业共享并集、调岗实时失效与子 Run 继承约束
# ==============================================================================


def test_projectless_knowledge_decoupling_cross_dept_isolation_and_explicit_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无项目对话自动检索公司+本部门库（隔离跨部门与个人库），支持显式关闭与显式子集限定。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            users = await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                # 预置 4 类知识库：公司库、研发部门库、财务部门库、研发员工未绑定个人库。
                await _seed_knowledge_base(
                    session,
                    kb_id="kb_company",
                    owner_id="usr_admin",
                    name="公司制度库",
                    scope=VisibilityScope.COMPANY,
                )
                await _seed_knowledge_base(
                    session,
                    kb_id="kb_rd",
                    owner_id="usr_admin",
                    name="研发架构库",
                    scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_rd",
                    grant_department_id="dept_rd",
                )
                await _seed_knowledge_base(
                    session,
                    kb_id="kb_fin",
                    owner_id="usr_admin",
                    name="财务薪酬库",
                    scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_fin",
                    grant_department_id="dept_fin",
                )
                await _seed_knowledge_base(
                    session,
                    kb_id="kb_rd_personal",
                    owner_id="usr_rd",
                    name="研发个人笔记",
                    scope=VisibilityScope.PERSONAL,
                )
                # 未绑定项目的普通对话。
                session.add(
                    SessionModel(
                        id="sess_free",
                        owner_id="usr_rd",
                        title=None,
                        project_id=None,
                        last_message_sequence=0,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

            access_policy = AccessPolicyService(SqlAlchemyAccessControlRepository(factory))
            sessions_repo = SqlAlchemySessionRepository(factory)
            projects_repo = SqlAlchemyProjectRepository(factory)
            project_service = ProjectService(projects_repo)
            kb_repo = KnowledgeRepository(factory)
            project_kb = ProjectKnowledgeService(
                project_service,
                kb_repo,
                access_policy=access_policy,
            )
            knowledge_stub = _RecordingKnowledgeService()
            runtime = RunToolRuntimeService(
                auth=_StubAuthReader(users),
                sessions=sessions_repo,
                mcp_catalog=_EmptyMcpCatalog(),
                mcp_executor=_NoopMcpExecutor(),
                knowledge=cast(KnowledgeService, knowledge_stub),
                project_knowledge=project_kb,
                access_policy=access_policy,
            )

            ctx_free = AuthorizedRunContext(
                owner_id="usr_rd",
                chat_session_id="sess_free",
                project_id=None,
                auth_session_id="auth_usr_rd",
            )
            # 1) 默认模式（knowledge_base_ids=None）+ 无项目：自动暴露 knowledge_search，
            # 仅检索研发库 + 公司库（按部门 > 公司排序），不含财务库和未绑定个人库。
            snap_default = await runtime.build_snapshot("run-v2", ctx_free, knowledge_base_ids=None)
            assert KNOWLEDGE_SEARCH_ID in {t.tool_id for t in snap_default.tools}

            res_default = await runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                "knowledge_search",
                {"query": "架构规范"},
                ToolExecutionContext(
                    owner_id="usr_rd",
                    chat_session_id="sess_free",
                    project_id=None,
                    run_id="run_1",
                    execution_epoch=1,
                    auth_session_id="auth_usr_rd",
                    deadline_at=NOW + timedelta(seconds=60),
                    knowledge_base_ids=None,
                ),
                deadline_seconds=10.0,
                run_remaining_seconds=60.0,
            )
            assert res_default.outcome.status is ToolOutcomeStatus.OK
            assert knowledge_stub.calls[-1] == ("usr_rd", ["kb_rd", "kb_company"], "架构规范")
            assert "kb_fin" not in knowledge_stub.calls[-1][1]
            assert "kb_rd_personal" not in knowledge_stub.calls[-1][1]

            # 2) 显式关闭知识库检索（knowledge_base_ids=()）：快照不暴露，执行期返回不可用。
            snap_disabled = await runtime.build_snapshot("run-v2", ctx_free, knowledge_base_ids=())
            assert KNOWLEDGE_SEARCH_ID not in {t.tool_id for t in snap_disabled.tools}

            res_disabled = await runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                "knowledge_search",
                {"query": "架构规范"},
                ToolExecutionContext(
                    owner_id="usr_rd",
                    chat_session_id="sess_free",
                    project_id=None,
                    run_id="run_2",
                    execution_epoch=1,
                    auth_session_id="auth_usr_rd",
                    deadline_at=NOW + timedelta(seconds=60),
                    knowledge_base_ids=(),
                ),
                deadline_seconds=10.0,
                run_remaining_seconds=60.0,
            )
            assert res_disabled.outcome.status is ToolOutcomeStatus.ERROR
            assert res_disabled.outcome.code == "knowledge_unavailable"

            # 3) 显式指定有权限子集（含个人库或公司库）：仅在该子集内检索。
            snap_explicit = await runtime.build_snapshot(
                "run-v2",
                ctx_free,
                knowledge_base_ids=("kb_rd_personal",),
            )
            assert KNOWLEDGE_SEARCH_ID in {t.tool_id for t in snap_explicit.tools}
            res_explicit = await runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                "knowledge_search",
                {"query": "个人笔记"},
                ToolExecutionContext(
                    owner_id="usr_rd",
                    chat_session_id="sess_free",
                    project_id=None,
                    run_id="run_3",
                    execution_epoch=1,
                    auth_session_id="auth_usr_rd",
                    deadline_at=NOW + timedelta(seconds=60),
                    knowledge_base_ids=("kb_rd_personal",),
                ),
                deadline_seconds=10.0,
                run_remaining_seconds=60.0,
            )
            assert res_explicit.outcome.status is ToolOutcomeStatus.OK
            assert knowledge_stub.calls[-1] == ("usr_rd", ["kb_rd_personal"], "个人笔记")

            # 4) 默认模式 + 项目绑定库：项目绑定库 ∪ 部门库 ∪ 公司库，且项目库优先。
            project = await project_service.create("usr_rd", "研发项目")
            await project_kb.bind("usr_rd", project.id, "kb_rd_personal")
            async with factory() as db_sess, db_sess.begin():
                db_sess.add(
                    SessionModel(
                        id="sess_proj",
                        owner_id="usr_rd",
                        title=None,
                        project_id=project.id,
                        last_message_sequence=0,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            ctx_proj = AuthorizedRunContext(
                owner_id="usr_rd",
                chat_session_id="sess_proj",
                project_id=project.id,
                auth_session_id="auth_usr_rd",
            )
            res_union = await runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                "knowledge_search",
                {"query": "全范围查询"},
                ToolExecutionContext(
                    owner_id="usr_rd",
                    chat_session_id="sess_proj",
                    project_id=project.id,
                    run_id="run_4",
                    execution_epoch=1,
                    auth_session_id="auth_usr_rd",
                    deadline_at=NOW + timedelta(seconds=60),
                    knowledge_base_ids=None,
                ),
                deadline_seconds=10.0,
                run_remaining_seconds=60.0,
            )
            assert res_union.outcome.status is ToolOutcomeStatus.OK
            assert knowledge_stub.calls[-1][1] == ["kb_rd_personal", "kb_rd", "kb_company"]

            # 5) 子 Run（depth=1）遵循主 Run 传入的 knowledge_base_ids：主 Run 禁用则子 Run 不暴露。
            sub_snap_disabled = await runtime.build_snapshot(
                "run-v2",
                ctx_proj,
                depth=1,
                allowed_tool_ids=(KNOWLEDGE_SEARCH_ID,),
                knowledge_base_ids=(),
            )
            assert KNOWLEDGE_SEARCH_ID not in {t.tool_id for t in sub_snap_disabled.tools}
            sub_snap_subset = await runtime.build_snapshot(
                "run-v2",
                ctx_proj,
                depth=1,
                allowed_tool_ids=(KNOWLEDGE_SEARCH_ID,),
                knowledge_base_ids=("kb_company",),
            )
            assert KNOWLEDGE_SEARCH_ID in {t.tool_id for t in sub_snap_subset.tools}

            # 6) 研发员工调岗至财务部后：调用前实时范围解析立即生效，默认模式剔除原部门研发库。
            async with factory() as db_sess, db_sess.begin():
                await db_sess.execute(
                    delete(UserDepartmentModel).where(UserDepartmentModel.user_id == "usr_rd")
                )
                db_sess.add(
                    UserDepartmentModel(
                        user_id="usr_rd",
                        department_id="dept_fin",
                        is_primary=True,
                    )
                )

            res_after_transfer = await runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                "knowledge_search",
                {"query": "涉密研发设计"},
                ToolExecutionContext(
                    owner_id="usr_rd",
                    chat_session_id="sess_free",
                    project_id=None,
                    run_id="run_5",
                    execution_epoch=1,
                    auth_session_id="auth_usr_rd",
                    deadline_at=NOW + timedelta(seconds=60),
                    knowledge_base_ids=None,
                ),
                deadline_seconds=10.0,
                run_remaining_seconds=60.0,
            )
            assert res_after_transfer.outcome.status is ToolOutcomeStatus.OK
            assert knowledge_stub.calls[-1][1] == ["kb_fin", "kb_company"]
            assert "Content from kb_rd" not in res_after_transfer.outcome.canonical_output()

            # 7) 显式模式指定原部门研发库：调用前实时鉴权立即拦截并拒绝检索。
            res_explicit_revoked = await runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                "knowledge_search",
                {"query": "涉密显式库"},
                ToolExecutionContext(
                    owner_id="usr_rd",
                    chat_session_id="sess_free",
                    project_id=None,
                    run_id="run_6",
                    execution_epoch=1,
                    auth_session_id="auth_usr_rd",
                    deadline_at=NOW + timedelta(seconds=60),
                    knowledge_base_ids=("kb_rd",),
                ),
                deadline_seconds=10.0,
                run_remaining_seconds=60.0,
            )
            assert res_explicit_revoked.outcome.status is ToolOutcomeStatus.ERROR
            assert res_explicit_revoked.outcome.code == "knowledge_scope_unavailable"
            assert "Content from kb_rd" not in res_explicit_revoked.outcome.canonical_output()
        finally:
            await engine.dispose()

    _run(scenario())


def test_default_workbench_caps_shared_bases_at_20_with_deterministic_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企业共享知识库超过 20 个时按 项目绑定库 > 部门库 > 公司库 截取前 20 个，不崩溃。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            users = await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                # 写入 2 个项目绑定的个人库、12 个研发部门库、12 个公司库（总数 26 > 20）。
                for i in range(2):
                    await _seed_knowledge_base(
                        session,
                        kb_id=f"kb_p_{i:02d}",
                        owner_id="usr_rd",
                        name=f"个人库 {i}",
                        scope=VisibilityScope.PERSONAL,
                    )
                for i in range(12):
                    await _seed_knowledge_base(
                        session,
                        kb_id=f"kb_d_{i:02d}",
                        owner_id="usr_admin",
                        name=f"部门库 {i}",
                        scope=VisibilityScope.DEPARTMENT,
                        owner_department_id="dept_rd",
                        grant_department_id="dept_rd",
                    )
                for i in range(12):
                    await _seed_knowledge_base(
                        session,
                        kb_id=f"kb_c_{i:02d}",
                        owner_id="usr_admin",
                        name=f"公司库 {i}",
                        scope=VisibilityScope.COMPANY,
                    )
                session.add(
                    SessionModel(
                        id="sess_cap",
                        owner_id="usr_rd",
                        title=None,
                        project_id=None,
                        last_message_sequence=0,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

            access_policy = AccessPolicyService(SqlAlchemyAccessControlRepository(factory))
            projects_repo = SqlAlchemyProjectRepository(factory)
            project_service = ProjectService(projects_repo)
            kb_repo = KnowledgeRepository(factory)
            project_kb = ProjectKnowledgeService(
                project_service,
                kb_repo,
                access_policy=access_policy,
            )
            project = await project_service.create("usr_rd", "大容量项目")
            await project_kb.bind("usr_rd", project.id, "kb_p_00")
            await project_kb.bind("usr_rd", project.id, "kb_p_01")
            async with factory() as db_sess, db_sess.begin():
                await db_sess.execute(
                    update(SessionModel)
                    .where(SessionModel.id == "sess_cap")
                    .values(project_id=project.id)
                )

            knowledge_stub = _RecordingKnowledgeService()
            runtime = RunToolRuntimeService(
                auth=_StubAuthReader(users),
                sessions=SqlAlchemySessionRepository(factory),
                mcp_catalog=_EmptyMcpCatalog(),
                mcp_executor=_NoopMcpExecutor(),
                knowledge=cast(KnowledgeService, knowledge_stub),
                project_knowledge=project_kb,
                access_policy=access_policy,
            )

            result = await runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                "knowledge_search",
                {"query": "大规模共享库检索"},
                ToolExecutionContext(
                    owner_id="usr_rd",
                    chat_session_id="sess_cap",
                    project_id=project.id,
                    run_id="run_cap",
                    execution_epoch=1,
                    auth_session_id="auth_usr_rd",
                    deadline_at=NOW + timedelta(seconds=60),
                    knowledge_base_ids=None,
                ),
                deadline_seconds=10.0,
                run_remaining_seconds=60.0,
            )
            assert result.outcome.status is ToolOutcomeStatus.OK
            searched = knowledge_stub.calls[-1][1]
            assert len(searched) == 20
            # 前 2 个必须是项目库，随后 12 个部门库，最后 6 个公司库。
            assert set(searched[:2]) == {"kb_p_00", "kb_p_01"}
            assert searched[2:14] == [f"kb_d_{i:02d}" for i in range(12)]
            assert searched[14:20] == [f"kb_c_{i:02d}" for i in range(6)]
        finally:
            await engine.dispose()

    _run(scenario())


def test_run_submit_api_validates_explicit_kb_ids_fingerprint_and_model_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run 提交接口校验跨部门 KB 返回 404、幂等指纹区分 None 与 []、场景模型选择校验与冻结。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                await _seed_knowledge_base(
                    session,
                    kb_id="kb_rd",
                    owner_id="usr_admin",
                    name="研发库",
                    scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_rd",
                    grant_department_id="dept_rd",
                )
                await _seed_knowledge_base(
                    session,
                    kb_id="kb_fin",
                    owner_id="usr_admin",
                    name="财务库",
                    scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_fin",
                    grant_department_id="dept_fin",
                )
                await _insert_model_profile(
                    session,
                    cipher,
                    profile_id="llmprof_reason",
                    name="深度推理模型",
                    model="o3-mini",
                    enabled=True,
                    visible_to_users=True,
                    scenario_label="深度推理",
                    scenario_description="适合复杂分析与架构设计",
                    supports_vision=True,
                    created_at=NOW + timedelta(seconds=1),
                )
                await _insert_model_profile(
                    session,
                    cipher,
                    profile_id="llmprof_hidden",
                    name="内部压缩模型",
                    model="gpt-4o-mini",
                    enabled=True,
                    visible_to_users=False,
                    scenario_label="后台压缩",
                    created_at=NOW + timedelta(seconds=2),
                )
                await _insert_model_profile(
                    session,
                    cipher,
                    profile_id="llmprof_disabled",
                    name="停用模型",
                    model="gpt-3.5",
                    enabled=False,
                    visible_to_users=True,
                    created_at=NOW + timedelta(seconds=3),
                )
                await _insert_model_profile(
                    session,
                    cipher,
                    profile_id="llmprof_deleted",
                    name="软删模型",
                    model="gpt-old",
                    enabled=True,
                    visible_to_users=True,
                    deleted_at=NOW,
                    created_at=NOW + timedelta(seconds=4),
                )

            auth_repo = SqlAlchemyAuthRepository(factory)
            auth_service = AuthService(auth_repo, FakePasswordHasher())
            project_service = ProjectService(SqlAlchemyProjectRepository(factory))
            session_service = SessionService(SqlAlchemySessionRepository(factory))
            message_repo = SqlAlchemyMessageRepository(factory)
            run_repo = SqlAlchemyRunRepository(factory, message_repo)
            run_service = RunService(run_repo)
            profile_repo = SqlAlchemyModelProfileRepository(factory, cipher)
            profile_service = ModelProfileService(profile_repo, auth_repo)

            app = create_app(
                project_service=project_service,
                session_service=session_service,
                run_service=run_service,
                auth_service=auth_service,
                model_profile_service=profile_service,
            )

            # 未登录访问 /api/v1/models 必须返回 401。
            with TestClient(app) as anon:
                assert anon.get("/api/v1/models").status_code == 401

            with _login_client(app, "rd@example.com") as client:
                # 1) GET /api/v1/models 仅返回 enabled & visible_to_users 且未软删除的模型，
                # 且严禁暴露 base_url、has_api_key、max_concurrency 等运维参数。
                models_resp = client.get("/api/v1/models")
                assert models_resp.status_code == 200
                models_body = models_resp.json()
                assert models_body["default_model_id"] == "llmprof_default"
                assert [item["id"] for item in models_body["items"]] == [
                    "llmprof_default",
                    "llmprof_reason",
                ]
                for item in models_body["items"]:
                    assert set(item.keys()) == {
                        "id",
                        "name",
                        "scenario_label",
                        "scenario_description",
                        "is_default",
                        "supports_vision",
                    }
                    assert "base_url" not in item
                    assert "has_api_key" not in item
                    assert "api_key" not in item
                assert models_body["items"][0]["is_default"] is True
                assert models_body["items"][1]["is_default"] is False
                assert models_body["items"][1]["supports_vision"] is True
                assert models_body["items"][1]["scenario_label"] == "深度推理"

                # 创建独立会话测试 Run 提交校验。
                sess_resp = client.post("/api/v1/sessions", json={})
                assert sess_resp.status_code == 201
                session_id = sess_resp.json()["id"]

                # 2) 研发员工显式传财务部知识库 ID -> 返回 404 knowledge_base_not_found 防探测。
                cross_dept = client.post(
                    f"/api/v1/sessions/{session_id}/runs",
                    json={
                        "request_id": "req_cross_dept",
                        "text": "查一下财务薪酬",
                        "knowledge_base_ids": ["kb_fin"],
                    },
                )
                assert cross_dept.status_code == 404
                assert cross_dept.json() == {"detail": "knowledge_base_not_found"}

                # 3) 指定不可见/停用/软删/不存在模型 -> 返回 422 model_profile_unavailable。
                for bad_model in (
                    "llmprof_hidden",
                    "llmprof_disabled",
                    "llmprof_deleted",
                    "llmprof_missing",
                ):
                    bad_resp = client.post(
                        f"/api/v1/sessions/{session_id}/runs",
                        json={
                            "request_id": f"req_bad_{bad_model}",
                            "text": "测试模型选择",
                            "model_profile_id": bad_model,
                        },
                    )
                    assert bad_resp.status_code == 422, (bad_model, bad_resp.text)
                    assert bad_resp.json() == {"detail": "model_profile_unavailable"}

                # 4) 显式选择可见启用模型 + 重复 KB ID 去重规范化持久化。
                ok_resp = client.post(
                    f"/api/v1/sessions/{session_id}/runs",
                    json={
                        "request_id": "req_valid_1",
                        "text": "帮我分析架构",
                        "knowledge_base_ids": ["kb_rd", "kb_rd"],
                        "model_profile_id": "llmprof_reason",
                    },
                )
                assert ok_resp.status_code == 201, ok_resp.text
                ok_body = ok_resp.json()
                assert ok_body["model_config_version_id"] == "llmprof_reason"
                assert ok_body["knowledge_base_ids"] == ["kb_rd"]

                # 5) 幂等指纹校验：同 request_id 改 knowledge_base_ids（["kb_rd"] vs [] vs None）
                # 或改 model_profile_id 必须报 409 冲突。
                conflict_empty = client.post(
                    f"/api/v1/sessions/{session_id}/runs",
                    json={
                        "request_id": "req_valid_1",
                        "text": "帮我分析架构",
                        "knowledge_base_ids": [],
                        "model_profile_id": "llmprof_reason",
                    },
                )
                assert conflict_empty.status_code == 409

                conflict_model = client.post(
                    f"/api/v1/sessions/{session_id}/runs",
                    json={
                        "request_id": "req_valid_1",
                        "text": "帮我分析架构",
                        "knowledge_base_ids": ["kb_rd"],
                        "model_profile_id": "llmprof_default",
                    },
                )
                assert conflict_model.status_code == 409

                # 再测另一个会话：首次传 None，同 request_id 改传 [] 也必须判定指纹不同。
                sess2_id = client.post("/api/v1/sessions", json={}).json()["id"]
                none_resp = client.post(
                    f"/api/v1/sessions/{sess2_id}/runs",
                    json={"request_id": "req_none_vs_empty", "text": "你好"},
                )
                assert none_resp.status_code == 201
                assert none_resp.json()["knowledge_base_ids"] is None
                empty_conflict = client.post(
                    f"/api/v1/sessions/{sess2_id}/runs",
                    json={
                        "request_id": "req_none_vs_empty",
                        "text": "你好",
                        "knowledge_base_ids": [],
                    },
                )
                assert empty_conflict.status_code == 409

            # 6) 管理员侧场景模型创建、控制字符校验及软删除过滤。
            with _login_client(app, "admin@example.com") as admin_client:
                # 列表默认过滤已软删除模型 llmprof_deleted。
                admin_list = admin_client.get("/api/v1/admin/llm/model-profiles")
                assert admin_list.status_code == 200
                admin_ids = {item["id"] for item in admin_list.json()["items"]}
                assert "llmprof_deleted" not in admin_ids
                assert {
                    "llmprof_default",
                    "llmprof_reason",
                    "llmprof_hidden",
                    "llmprof_disabled",
                } <= admin_ids

                # 创建含场景字段的模型：scenario_label 含控制字符被拒。
                bad_create = admin_client.post(
                    "/api/v1/admin/llm/model-profiles",
                    json={
                        "name": "非法标签模型",
                        "base_url": "https://api.example.com/v1",
                        "model": "gpt-4o",
                        "api_key": "sk-test",
                        "context_window_tokens": 128_000,
                        "tokenizer_encoding": "o200k_base",
                        "max_output_tokens": 4096,
                        "request_timeout_seconds": 30,
                        "execution_timeout_seconds": 120,
                        "max_concurrency": 4,
                        "visible_to_users": True,
                        "scenario_label": "非法\x07标签",
                    },
                )
                assert bad_create.status_code == 422
                assert bad_create.json() == {"detail": "llm_model_profile_invalid"}

                ok_create = admin_client.post(
                    "/api/v1/admin/llm/model-profiles",
                    json={
                        "name": "视觉多模态助手",
                        "base_url": "https://api.example.com/v1",
                        "model": "gpt-4o-vision",
                        "api_key": "sk-test",
                        "context_window_tokens": 128_000,
                        "tokenizer_encoding": "o200k_base",
                        "max_output_tokens": 4096,
                        "request_timeout_seconds": 30,
                        "execution_timeout_seconds": 120,
                        "max_concurrency": 4,
                        "visible_to_users": True,
                        "scenario_label": "图文分析",
                        "scenario_description": "适合图表与截图理解",
                        "supports_vision": True,
                    },
                )
                assert ok_create.status_code == 201, ok_create.text
                created_body = ok_create.json()
                assert created_body["visible_to_users"] is True
                assert created_body["scenario_label"] == "图文分析"
                assert created_body["scenario_description"] == "适合图表与截图理解"
                assert created_body["supports_vision"] is True
        finally:
            await engine.dispose()

    _run(scenario())


def test_models_catalog_filters_unready_dialogue_models_and_hides_unready_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/v1/models 过滤不支持流式、不支持工具或缺失加密凭据的模型，并隐藏不合格默认 ID。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                # 预置 2 个虽 enabled=True & visible_to_users=True，但不满足对话前置条件的模型。
                await _insert_model_profile(
                    session,
                    cipher,
                    profile_id="llmprof_no_stream",
                    name="无流式模型",
                    model="gpt-no-stream",
                    supports_stream=False,
                    supports_tools=True,
                    created_at=NOW + timedelta(seconds=1),
                )
                await _insert_model_profile(
                    session,
                    cipher,
                    profile_id="llmprof_no_tools",
                    name="无工具模型",
                    model="gpt-no-tools",
                    supports_stream=True,
                    supports_tools=False,
                    created_at=NOW + timedelta(seconds=2),
                )

            auth_repo = SqlAlchemyAuthRepository(factory)
            auth_service = AuthService(auth_repo, FakePasswordHasher())
            profile_service = ModelProfileService(
                SqlAlchemyModelProfileRepository(factory, cipher),
                auth_repo,
            )
            app = create_app(
                auth_service=auth_service,
                model_profile_service=profile_service,
            )

            with _login_client(app, "rd@example.com") as client:
                # 1) 两个不满足对话前置条件的模型全部被过滤，仅返回 llmprof_default。
                resp = client.get("/api/v1/models")
                assert resp.status_code == 200
                body = resp.json()
                assert [item["id"] for item in body["items"]] == ["llmprof_default"]
                assert body["default_model_id"] == "llmprof_default"

                # 2) 若默认指针指向不支持流式或不支持工具的模型，default_model_id 必须为 None。
                for unready_default_id in ("llmprof_no_stream", "llmprof_no_tools"):
                    async with factory() as db_sess, db_sess.begin():
                        await db_sess.execute(
                            update(DefaultModelProfileModel)
                            .where(DefaultModelProfileModel.singleton_id == 1)
                            .values(profile_id=unready_default_id)
                        )
                    resp_unready = client.get("/api/v1/models")
                    assert resp_unready.status_code == 200
                    unready_body = resp_unready.json()
                    assert [item["id"] for item in unready_body["items"]] == ["llmprof_default"]
                    assert unready_body["default_model_id"] is None
                    assert unready_body["items"][0]["is_default"] is False
        finally:
            await engine.dispose()

    _run(scenario())


def test_run_retry_inherits_and_revalidates_model_and_kb_and_submit_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 retry 继承并复核场景模型与显式 KB 权限，且 submit 断网重放不受后续 KB 删除影响。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                await _seed_knowledge_base(
                    session,
                    kb_id="kb_rd",
                    owner_id="usr_admin",
                    name="研发库",
                    scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_rd",
                    grant_department_id="dept_rd",
                )
                await _seed_knowledge_base(
                    session,
                    kb_id="kb_replay",
                    owner_id="usr_rd",
                    name="重放测试库",
                    scope=VisibilityScope.PERSONAL,
                )
                await _insert_model_profile(
                    session,
                    cipher,
                    profile_id="llmprof_reason",
                    name="深度推理模型",
                    model="o3-mini",
                    enabled=True,
                    visible_to_users=True,
                    scenario_label="深度推理",
                    created_at=NOW + timedelta(seconds=1),
                )

            auth_repo = SqlAlchemyAuthRepository(factory)
            auth_service = AuthService(auth_repo, FakePasswordHasher())
            project_service = ProjectService(SqlAlchemyProjectRepository(factory))
            session_service = SessionService(SqlAlchemySessionRepository(factory))
            message_repo = SqlAlchemyMessageRepository(factory)
            run_repo = SqlAlchemyRunRepository(factory, message_repo)
            run_service = RunService(run_repo)
            profile_service = ModelProfileService(
                SqlAlchemyModelProfileRepository(factory, cipher),
                auth_repo,
            )

            app = create_app(
                project_service=project_service,
                session_service=session_service,
                run_service=run_service,
                auth_service=auth_service,
                model_profile_service=profile_service,
            )

            with _login_client(app, "rd@example.com") as client:
                # 1) Finding 4：submit 幂等重放不受后续知识库被软删除影响。
                replay_sess_id = client.post("/api/v1/sessions", json={}).json()["id"]
                submit_payload = {
                    "request_id": "req_kb_replay_1",
                    "text": "检索重放库",
                    "knowledge_base_ids": ["kb_replay"],
                }
                first_submit = client.post(
                    f"/api/v1/sessions/{replay_sess_id}/runs",
                    json=submit_payload,
                )
                assert first_submit.status_code == 201, first_submit.text
                first_run_id = first_submit.json()["id"]

                # 软删除 kb_replay 后，以完全相同的 request_id 断网重放仍应幂等返回原 Run。
                async with factory() as db_sess, db_sess.begin():
                    await db_sess.execute(
                        update(KnowledgeBaseModel)
                        .where(KnowledgeBaseModel.id == "kb_replay")
                        .values(deleted_time=NOW)
                    )
                replay_submit = client.post(
                    f"/api/v1/sessions/{replay_sess_id}/runs",
                    json=submit_payload,
                )
                assert replay_submit.status_code == 201, replay_submit.text
                assert replay_submit.json()["replayed"] is True
                assert replay_submit.json()["id"] == first_run_id
                assert replay_submit.json()["knowledge_base_ids"] == ["kb_replay"]

                # 2) Finding 1 & 2 正常路径：原 Run 指定非默认场景模型 + 显式部门库，
                # 失败后重试必须完整继承 model_config_version_id 与 knowledge_base_ids。
                retry_sess_id = client.post("/api/v1/sessions", json={}).json()["id"]
                created = client.post(
                    f"/api/v1/sessions/{retry_sess_id}/runs",
                    json={
                        "request_id": "req_src_run",
                        "text": "用深度推理模型查研发库",
                        "model_profile_id": "llmprof_reason",
                        "knowledge_base_ids": ["kb_rd"],
                    },
                )
                assert created.status_code == 201, created.text
                source_run_id = created.json()["id"]
                assert created.json()["model_config_version_id"] == "llmprof_reason"
                assert created.json()["knowledge_base_ids"] == ["kb_rd"]

                async with factory() as db_sess, db_sess.begin():
                    await db_sess.execute(
                        update(RunModel)
                        .where(RunModel.id == source_run_id)
                        .values(status=RunStatus.FAILED.value)
                    )

                retried_ok = client.post(
                    f"/api/v1/sessions/{retry_sess_id}/runs/{source_run_id}/retry",
                    json={"request_id": "req_retry_ok_1"},
                )
                assert retried_ok.status_code == 200, retried_ok.text
                retried_body = retried_ok.json()
                assert retried_body["retry_of_run_id"] == source_run_id
                assert retried_body["model_config_version_id"] == "llmprof_reason"
                assert retried_body["knowledge_base_ids"] == ["kb_rd"]
                latest_run_id = retried_body["id"]

                # 将重试产生的新 Run 再次置为 failed，验证模型失活/隐藏/软删拒绝逻辑。
                async with factory() as db_sess, db_sess.begin():
                    await db_sess.execute(
                        update(RunModel)
                        .where(RunModel.id == latest_run_id)
                        .values(status=RunStatus.FAILED.value)
                    )

                # 2a) 原模型被停用 -> 422 model_profile_unavailable。
                async with factory() as db_sess, db_sess.begin():
                    await db_sess.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == "llmprof_reason")
                        .values(enabled=False)
                    )
                retry_disabled = client.post(
                    f"/api/v1/sessions/{retry_sess_id}/runs/{latest_run_id}/retry",
                    json={"request_id": "req_retry_model_disabled"},
                )
                assert retry_disabled.status_code == 422
                assert retry_disabled.json() == {"detail": "model_profile_unavailable"}

                # 2b) 原模型设为不可见（且非默认模型） -> 422 model_profile_unavailable。
                async with factory() as db_sess, db_sess.begin():
                    await db_sess.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == "llmprof_reason")
                        .values(enabled=True, visible_to_users=False)
                    )
                retry_hidden = client.post(
                    f"/api/v1/sessions/{retry_sess_id}/runs/{latest_run_id}/retry",
                    json={"request_id": "req_retry_model_hidden"},
                )
                assert retry_hidden.status_code == 422
                assert retry_hidden.json() == {"detail": "model_profile_unavailable"}

                # 2c) 若该隐藏模型恰为当前默认模型，重试仍被允许并继承该模型。
                async with factory() as db_sess, db_sess.begin():
                    await db_sess.execute(
                        update(DefaultModelProfileModel)
                        .where(DefaultModelProfileModel.singleton_id == 1)
                        .values(profile_id="llmprof_reason")
                    )
                retry_hidden_default = client.post(
                    f"/api/v1/sessions/{retry_sess_id}/runs/{latest_run_id}/retry",
                    json={"request_id": "req_retry_hidden_default"},
                )
                assert retry_hidden_default.status_code == 200, retry_hidden_default.text
                assert retry_hidden_default.json()["model_config_version_id"] == "llmprof_reason"
                latest_run_id = retry_hidden_default.json()["id"]

                # 2d) 恢复默认模型并软删除 llmprof_reason -> 422 model_profile_unavailable。
                async with factory() as db_sess, db_sess.begin():
                    await db_sess.execute(
                        update(RunModel)
                        .where(RunModel.id == latest_run_id)
                        .values(status=RunStatus.FAILED.value)
                    )
                    await db_sess.execute(
                        update(DefaultModelProfileModel)
                        .where(DefaultModelProfileModel.singleton_id == 1)
                        .values(profile_id="llmprof_default")
                    )
                    await db_sess.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == "llmprof_reason")
                        .values(visible_to_users=True, deleted_at=NOW)
                    )
                retry_deleted_model = client.post(
                    f"/api/v1/sessions/{retry_sess_id}/runs/{latest_run_id}/retry",
                    json={"request_id": "req_retry_model_deleted"},
                )
                assert retry_deleted_model.status_code == 422
                assert retry_deleted_model.json() == {"detail": "model_profile_unavailable"}

                # 3) Finding 2 失权与删除路径：恢复模型后，调岗失权与知识库软删除均返回 404。
                async with factory() as db_sess, db_sess.begin():
                    await db_sess.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == "llmprof_reason")
                        .values(deleted_at=None)
                    )
                    # 将研发员工调岗至财务部，失去 kb_rd 读权限。
                    await db_sess.execute(
                        delete(UserDepartmentModel).where(UserDepartmentModel.user_id == "usr_rd")
                    )
                    db_sess.add(
                        UserDepartmentModel(
                            user_id="usr_rd",
                            department_id="dept_fin",
                            is_primary=True,
                        )
                    )
                retry_kb_revoked = client.post(
                    f"/api/v1/sessions/{retry_sess_id}/runs/{latest_run_id}/retry",
                    json={"request_id": "req_retry_kb_revoked"},
                )
                assert retry_kb_revoked.status_code == 404
                assert retry_kb_revoked.json() == {"detail": "knowledge_base_not_found"}

                # 恢复研发部门但软删除 kb_rd -> 同样返回 404 knowledge_base_not_found。
                async with factory() as db_sess, db_sess.begin():
                    await db_sess.execute(
                        delete(UserDepartmentModel).where(UserDepartmentModel.user_id == "usr_rd")
                    )
                    db_sess.add(
                        UserDepartmentModel(
                            user_id="usr_rd",
                            department_id="dept_rd",
                            is_primary=True,
                        )
                    )
                    await db_sess.execute(
                        update(KnowledgeBaseModel)
                        .where(KnowledgeBaseModel.id == "kb_rd")
                        .values(deleted_time=NOW)
                    )
                retry_kb_deleted = client.post(
                    f"/api/v1/sessions/{retry_sess_id}/runs/{latest_run_id}/retry",
                    json={"request_id": "req_retry_kb_deleted"},
                )
                assert retry_kb_deleted.status_code == 404
                assert retry_kb_deleted.json() == {"detail": "knowledge_base_not_found"}
        finally:
            await engine.dispose()

    _run(scenario())


# ==============================================================================
# 2. 项目模型增强、默认技能安全回退、项目指令注入（主/子隔离）、解绑删除与会话搜索
# ==============================================================================


def test_project_upgrade_detach_delete_session_rebind_and_escaped_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证项目扩展字段 CRUD、detach_sessions 解绑删除、消息后会话改绑与转义搜索 q。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                await _seed_knowledge_base(
                    session,
                    kb_id="kb_proj_bind",
                    owner_id="usr_rd",
                    name="项目绑定库",
                    scope=VisibilityScope.PERSONAL,
                )
                await _seed_skill(
                    session,
                    skill_id="sk_company_ok",
                    slug="company-ok",
                    scope=SkillScope.COMPANY,
                )
                await _seed_skill(
                    session,
                    skill_id="sk_rd_ok",
                    slug="rd-ok",
                    scope=SkillScope.DEPARTMENT,
                    owner_department_id="dept_rd",
                    grant_department_id="dept_rd",
                )
                await _seed_skill(
                    session,
                    skill_id="sk_fin_denied",
                    slug="fin-denied",
                    scope=SkillScope.DEPARTMENT,
                    owner_department_id="dept_fin",
                    grant_department_id="dept_fin",
                )
                await _seed_skill(
                    session,
                    skill_id="sk_disabled",
                    slug="disabled-skill",
                    scope=SkillScope.COMPANY,
                    enabled=False,
                )
                await _seed_skill(
                    session,
                    skill_id="sk_deleted",
                    slug="deleted-skill",
                    scope=SkillScope.PERSONAL,
                    owner_id="usr_rd",
                    deleted_at=NOW,
                )

            access_policy = AccessPolicyService(SqlAlchemyAccessControlRepository(factory))
            auth_service = AuthService(SqlAlchemyAuthRepository(factory), FakePasswordHasher())
            project_repo = SqlAlchemyProjectRepository(factory)
            project_service = ProjectService(project_repo)
            session_service = SessionService(SqlAlchemySessionRepository(factory))
            message_repo = SqlAlchemyMessageRepository(factory)
            run_service = RunService(SqlAlchemyRunRepository(factory, message_repo))
            kb_repo = KnowledgeRepository(factory)
            project_kb = ProjectKnowledgeService(
                project_service, kb_repo, access_policy=access_policy
            )

            app = create_app(
                project_service=project_service,
                session_service=session_service,
                run_service=run_service,
                auth_service=auth_service,
            )

            with _login_client(app, "rd@example.com") as client:
                # 1) 创建项目：携带 description / instructions（含换行制表符）
                # 及 default_skill_ids（含重复与失效项）。
                created = client.post(
                    "/api/v1/projects",
                    json={
                        "name": "Q4 架构演进",
                        "description": "负责核心检索与对话工作台重构",
                        "instructions": "回答时优先给结论。\n\t- 附带关键风险点",
                        "default_skill_ids": [
                            "sk_company_ok",
                            "sk_rd_ok",
                            "sk_rd_ok",
                            "sk_fin_denied",
                            "sk_disabled",
                            "sk_deleted",
                            "sk_nonexistent",
                        ],
                    },
                )
                assert created.status_code == 201, created.text
                proj = created.json()
                project_id = proj["id"]
                assert proj["description"] == "负责核心检索与对话工作台重构"
                assert proj["instructions"] == "回答时优先给结论。\n\t- 附带关键风险点"
                assert proj["default_skill_ids"] == [
                    "sk_company_ok",
                    "sk_rd_ok",
                    "sk_fin_denied",
                    "sk_disabled",
                    "sk_deleted",
                    "sk_nonexistent",
                ]

                # 非法输入校验：超长 description / instructions、非法控制字符、空 PATCH。
                assert (
                    client.post(
                        "/api/v1/projects",
                        json={"name": "坏项目", "description": "a" * 501},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/v1/projects",
                        json={"name": "坏项目", "instructions": "a" * 8001},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/v1/projects",
                        json={"name": "坏项目", "instructions": "含非法控制\x07字符"},
                    ).status_code
                    == 422
                )
                assert client.patch(f"/api/v1/projects/{project_id}", json={}).status_code == 422

                # 2) PATCH 局部更新：仅改 name 时保留其他字段；显式传 null 清空 description。
                p_name = client.patch(
                    f"/api/v1/projects/{project_id}",
                    json={"name": "Q4 架构演进（升级）"},
                )
                assert p_name.status_code == 200
                assert p_name.json()["name"] == "Q4 架构演进（升级）"
                assert p_name.json()["description"] == "负责核心检索与对话工作台重构"
                assert p_name.json()["instructions"] == "回答时优先给结论。\n\t- 附带关键风险点"

                p_clear = client.patch(
                    f"/api/v1/projects/{project_id}",
                    json={"description": None},
                )
                assert p_clear.status_code == 200
                assert p_clear.json()["description"] is None
                assert p_clear.json()["instructions"] == "回答时优先给结论。\n\t- 附带关键风险点"

                # 3) 项目默认技能安全回退：在绑定该项目的会话中提交 Run（skill_ids=[]），
                # 仅选中可读且启用的 sk_company_ok 与 sk_rd_ok，
                # 其余无权/停用/已删/不存在项自动静默忽略。
                bound_sess = client.post(
                    "/api/v1/sessions",
                    json={"title": "项目内讨论", "project_id": project_id},
                )
                assert bound_sess.status_code == 201
                bound_sess_id = bound_sess.json()["id"]

                run_resp = client.post(
                    f"/api/v1/sessions/{bound_sess_id}/runs",
                    json={"request_id": "req_default_skills", "text": "开始分析"},
                )
                assert run_resp.status_code == 201, run_resp.text
                run_body = run_resp.json()
                assert set(run_body["skill_snapshot"]["selected_skill_ids"]) == {
                    "sk_company_ok",
                    "sk_rd_ok",
                }

                # 4) 存在活跃 Run 时：
                # - PATCH /api/v1/sessions/{id} 变更 project_id 返回 409；
                # - DELETE /api/v1/projects/{id}?detach_sessions=true 返回 409。
                other_proj_id = client.post("/api/v1/projects", json={"name": "目标项目"}).json()[
                    "id"
                ]
                blocked_rebind = client.patch(
                    f"/api/v1/sessions/{bound_sess_id}",
                    json={"project_id": other_proj_id},
                )
                assert blocked_rebind.status_code == 409
                assert blocked_rebind.json() == {"detail": "session_has_active_run"}

                blocked_detach = client.delete(
                    f"/api/v1/projects/{project_id}",
                    params={"detach_sessions": "true"},
                )
                assert blocked_detach.status_code == 409
                assert blocked_detach.json() == {"detail": "session_has_active_run"}

                # 取消活跃 Run，使会话回到无活跃 Run
                # 但已有历史消息（last_message_sequence > 0）的状态。
                cancel_resp = client.post(
                    f"/api/v1/sessions/{bound_sess_id}/runs/{run_body['id']}/cancel"
                )
                assert cancel_resp.status_code == 200
                assert cancel_resp.json()["status"] == "cancelled"

                # 用户显式传 skill_ids 时应严格覆盖项目默认技能，不混入 default_skill_ids。
                explicit_skill_run = client.post(
                    f"/api/v1/sessions/{bound_sess_id}/runs",
                    json={
                        "request_id": "req_explicit_skill_override",
                        "text": "仅使用指定技能",
                        "skill_ids": ["sk_company_ok"],
                    },
                )
                assert explicit_skill_run.status_code == 201, explicit_skill_run.text
                assert explicit_skill_run.json()["skill_snapshot"]["selected_skill_ids"] == [
                    "sk_company_ok"
                ]
                assert (
                    client.post(
                        f"/api/v1/sessions/{bound_sess_id}/runs/"
                        f"{explicit_skill_run.json()['id']}/cancel"
                    ).status_code
                    == 200
                )

                # 5) 已有历史消息但无活跃 Run：允许自由改绑项目与解绑为独立对话！
                rebind_ok = client.patch(
                    f"/api/v1/sessions/{bound_sess_id}",
                    json={"project_id": other_proj_id},
                )
                assert rebind_ok.status_code == 200
                assert rebind_ok.json()["project_id"] == other_proj_id

                rebind_back = client.patch(
                    f"/api/v1/sessions/{bound_sess_id}",
                    json={"project_id": project_id},
                )
                assert rebind_back.status_code == 200
                assert rebind_back.json()["project_id"] == project_id

                # 6) DELETE /api/v1/projects/{id}：
                # 先给项目绑定知识库；默认 detach_sessions=false 返回 409 project_has_sessions；
                # detach_sessions=true 批量将未删除会话 project_id 置 NULL、
                # 清理 project_knowledge_bases 并软删项目。
                await project_kb.bind("usr_rd", project_id, "kb_proj_bind")
                refused_del = client.delete(f"/api/v1/projects/{project_id}")
                assert refused_del.status_code == 409
                assert refused_del.json() == {"detail": "project_has_sessions"}

                detached_del = client.delete(
                    f"/api/v1/projects/{project_id}",
                    params={"detach_sessions": "true"},
                )
                assert detached_del.status_code == 204
                # 项目已隐藏，原会话变为未归属项目的独立对话。
                assert client.get(f"/api/v1/projects/{project_id}").status_code == 404
                detached_sess = client.get(f"/api/v1/sessions/{bound_sess_id}")
                assert detached_sess.status_code == 200
                assert detached_sess.json()["project_id"] is None

                async with factory() as db_sess:
                    remaining_links = (
                        await db_sess.scalars(
                            select(ProjectKnowledgeBaseModel.project_id).where(
                                ProjectKnowledgeBaseModel.project_id == project_id
                            )
                        )
                    ).all()
                    assert remaining_links == []

                # 7) GET /api/v1/sessions?q=... 不区分大小写搜索与 % / _ / \ 通配符安全转义。
                s_alpha = client.post("/api/v1/sessions", json={"title": "Alpha 季度汇报"}).json()[
                    "id"
                ]
                s_pct = client.post("/api/v1/sessions", json={"title": "增长率 100% 复盘"}).json()[
                    "id"
                ]
                s_under = client.post("/api/v1/sessions", json={"title": "指标_v2 分析"}).json()[
                    "id"
                ]
                s_slash = client.post(
                    "/api/v1/sessions", json={"title": r"路径 C:\data 排查"}
                ).json()["id"]
                _ = client.post("/api/v1/sessions", json={"title": None}).json()["id"]

                q_ci = client.get("/api/v1/sessions", params={"q": "  alpha  "})
                assert q_ci.status_code == 200
                assert [item["id"] for item in q_ci.json()["items"]] == [s_alpha]

                # 若未转义 %，会错误匹配所有带标题会话；正确转义后仅命中含字面 % 的会话。
                q_pct = client.get("/api/v1/sessions", params={"q": "%"})
                assert q_pct.status_code == 200
                assert [item["id"] for item in q_pct.json()["items"]] == [s_pct]

                # 若未转义 _，会错误匹配任意单字符标题；正确转义后仅命中含字面 _ 的会话。
                q_under = client.get("/api/v1/sessions", params={"q": "_v2"})
                assert q_under.status_code == 200
                assert [item["id"] for item in q_under.json()["items"]] == [s_under]

                q_slash = client.get("/api/v1/sessions", params={"q": r"C:\data"})
                assert q_slash.status_code == 200
                assert [item["id"] for item in q_slash.json()["items"]] == [s_slash]
        finally:
            await engine.dispose()

    _run(scenario())


def test_run_executor_injects_project_instructions_for_main_run_only_and_meters_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 Run 注入 project.instructions 并完整计入 token 预算；子 Run 保持严格隔离不注入。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            users = await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                session.add(
                    ProjectModel(
                        id="proj_inst",
                        owner_id="usr_rd",
                        name="核心交付项目",
                        description="项目简介",
                        instructions="所有方案必须给出回滚步骤与监控指标。",
                        default_skill_ids=[],
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                session.add(
                    SessionModel(
                        id="sess_proj",
                        owner_id="usr_rd",
                        title="已有手动标题",
                        project_id="proj_inst",
                        last_message_sequence=0,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                session.add(
                    SubagentModel(
                        id="sub_worker",
                        slug="researcher",
                        name="调研专家",
                        description="负责专项调研",
                        system_prompt="你是调研专家子智能体。",
                        model_profile_id=None,
                        allowed_tool_ids=[],
                        visibility_scope=VisibilityScope.COMPANY.value,
                        owner_department_id=None,
                        max_llm_requests=5,
                        timeout_seconds=60,
                        enabled=True,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

            access_policy = AccessPolicyService(SqlAlchemyAccessControlRepository(factory))
            message_repo = SqlAlchemyMessageRepository(factory)
            run_repo = SqlAlchemyRunRepository(factory, message_repo)
            run_service = RunService(run_repo)
            profile_repo = SqlAlchemyModelProfileRepository(factory, cipher)
            projects_repo = SqlAlchemyProjectRepository(factory)
            sessions_repo = SqlAlchemySessionRepository(factory)
            subagents_repo = SqlAlchemySubagentRepository(factory)

            tool_runtime = RunToolRuntimeService(
                auth=_StubAuthReader(users),
                sessions=sessions_repo,
                mcp_catalog=_EmptyMcpCatalog(),
                mcp_executor=_NoopMcpExecutor(),
                knowledge=None,
                project_knowledge=None,
                subagents=subagents_repo,
                access_policy=access_policy,
            )

            # 1) 主 Run 第 1 轮派发子 Run，子 Run 完成后主 Run 第 2 轮收尾。
            provider = _ScriptedLlmProvider(
                stream_responses=[
                    _tool_call_response(
                        "call_sub_1",
                        "dispatch_subagents",
                        {"tasks": [{"subagent_slug": "researcher", "task": "调研缓存一致性"}]},
                    ),
                    _text_response("子任务调研结论：采用版本戳校验。"),
                    _text_response("主任务综合回答：已给出回滚与监控方案。"),
                ]
            )
            executor = RunExecutor(
                run_repo,
                profile_repo,
                provider,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=tool_runtime,
                projects=projects_repo,
                sessions=sessions_repo,
                subagents=subagents_repo,
                access_policy=access_policy,
                config=RunWorkerConfig(llm_request_limit=10),
            )

            submitted = await run_service.submit(
                "sess_proj",
                "usr_rd",
                "req_proj_inst",
                "请设计缓存升级方案",
                auth_session_id="auth_usr_rd",
            )
            leases_1 = await run_repo.claim(
                "worker_1", lease_seconds=60, execution_seconds=120, limit=1
            )
            assert len(leases_1) == 1
            await executor.execute(leases_1[0])

            # 领取并执行子 Run（depth=1）。
            sub_leases = await run_repo.claim(
                "worker_1", lease_seconds=60, execution_seconds=120, limit=1
            )
            assert len(sub_leases) == 1
            assert sub_leases[0].run.depth == 1
            await executor.execute(sub_leases[0])

            # 子 Run 完成后主 Run 被重新唤醒。
            main_resumed_leases = await run_repo.claim(
                "worker_1", lease_seconds=60, execution_seconds=120, limit=1
            )
            assert len(main_resumed_leases) == 1
            assert main_resumed_leases[0].run.id == submitted.run.id
            await executor.execute(main_resumed_leases[0])
            await executor.wait_background_tasks()

            # 校验：主 Run 请求（第 0、2 次）包含项目背景指令块；
            # 子 Run 请求（第 1 次）绝不含项目指令。
            assert len(provider.stream_requests) == 3
            main_req_0_texts = [msg.content for msg in provider.stream_requests[0][1].messages]
            sub_req_1_texts = [msg.content for msg in provider.stream_requests[1][1].messages]
            assert any("所有方案必须给出回滚步骤与监控指标。" in t for t in main_req_0_texts)
            assert not any("所有方案必须给出回滚步骤与监控指标。" in t for t in sub_req_1_texts)

            # 验证 token 度量函数将 project_context 完整计入输入 token 预算。
            runtime_profile = await profile_repo.get_runtime("llmprof_default")
            assert runtime_profile is not None
            _, _, tokens_without, _ = await measure_llm_request(
                LlmRequestSpec(
                    run=submitted.run,
                    messages=(),
                    main_prompt="主提示词",
                    project_context=None,
                ),
                runtime_profile.version,
                timeout_seconds=10.0,
            )
            _, _, tokens_with, _ = await measure_llm_request(
                LlmRequestSpec(
                    run=submitted.run,
                    messages=(),
                    main_prompt="主提示词",
                    project_context="项目指令内容：" + "架构规范" * 50,
                ),
                runtime_profile.version,
                timeout_seconds=10.0,
            )
            assert tokens_with > tokens_without
        finally:
            await engine.dispose()

    _run(scenario())


# ==============================================================================
# 3. 首轮自动标题生成：正常生成与清洗、压缩模型优先、超时/报错降级、防覆盖、子 Run 隔离与异常隔离
# ==============================================================================


def test_auto_title_sanitization_and_fallback_helpers() -> None:
    """验证标题清洗（引号、书名号、换行、控制字符、末尾标点、20字截断）与 24 字降级。"""
    assert sanitize_generated_title('  "《季度研发复盘报告》。"\n\x07 ') == "季度研发复盘报告"
    assert sanitize_generated_title("「『多引号标题』！」") == "多引号标题"
    long_title = sanitize_generated_title("这是一个长度明显超过二十个汉字上限的模型生成标题示例")
    assert len(long_title) <= 20
    assert sanitize_generated_title('  "《》。！？" \x00 ') == ""

    fallback = fallback_title_from_message(
        "  帮我整理\n\t\x07 2026年第四季度企业知识库架构升级与安全评审清单  "
    )
    assert "\n" not in fallback and "\t" not in fallback and "\x07" not in fallback
    assert len(fallback) == 24
    assert fallback == "帮我整理 2026年第四季度企业知识库架构升级与"


def test_auto_title_generation_compression_model_priority_and_manual_title_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 Run 完成后优先用压缩模型生成 <=20 字标题；压缩模型停用回退 Run 模型；绝不覆盖手工标题。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            users = await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                # 配置专用压缩模型（visible_to_users=False 但 enabled=True）。
                await _insert_model_profile(
                    session,
                    cipher,
                    profile_id="llmprof_compress",
                    name="压缩小模型",
                    model="gpt-4o-mini",
                    enabled=True,
                    visible_to_users=False,
                    created_at=NOW + timedelta(seconds=1),
                )
                await session.flush()
                session.add(
                    CompressionPolicyVersionModel(
                        id="cmpol_1",
                        compression_model_profile_id="llmprof_compress",
                        trigger_tokens=10_000,
                        recent_ratio=0.4,
                        recent_token_cap=4000,
                        summary_ratio=0.2,
                        summary_token_cap=2000,
                        created_at=NOW,
                    )
                )
                await session.flush()
                session.add(
                    CurrentCompressionPolicyModel(singleton_id=1, policy_version_id="cmpol_1")
                )
                session.add_all(
                    [
                        SessionModel(
                            id="sess_auto_1",
                            owner_id="usr_rd",
                            title=None,
                            project_id=None,
                            last_message_sequence=0,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        SessionModel(
                            id="sess_manual_race",
                            owner_id="usr_rd",
                            title=None,
                            project_id=None,
                            last_message_sequence=0,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        SessionModel(
                            id="sess_pre_titled",
                            owner_id="usr_rd",
                            title="用户预先定好的标题",
                            project_id=None,
                            last_message_sequence=0,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                    ]
                )

            message_repo = SqlAlchemyMessageRepository(factory)
            run_repo = SqlAlchemyRunRepository(factory, message_repo)
            run_service = RunService(run_repo)
            profile_repo = SqlAlchemyModelProfileRepository(factory, cipher)
            sessions_repo = SqlAlchemySessionRepository(factory)
            tool_runtime = RunToolRuntimeService(
                auth=_StubAuthReader(users),
                sessions=sessions_repo,
                mcp_catalog=_EmptyMcpCatalog(),
                mcp_executor=_NoopMcpExecutor(),
                knowledge=None,
                project_knowledge=None,
                access_policy=AccessPolicyService(SqlAlchemyAccessControlRepository(factory)),
            )

            # 1) 正常自动标题：优先选用压缩模型 llmprof_compress，并清洗引号与末尾句号。
            async def _title_complete(
                runtime: RuntimeModelProfile, request: LlmRequest
            ) -> LlmResponse:
                del runtime, request
                return _text_response('  "《分布式缓存架构方案》。"\n')

            provider = _ScriptedLlmProvider(
                stream_responses=[_text_response("这是完整回复。")],
                complete_handler=_title_complete,
            )
            executor = RunExecutor(
                run_repo,
                profile_repo,
                provider,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=tool_runtime,
                sessions=sessions_repo,
            )
            await run_service.submit(
                "sess_auto_1",
                "usr_rd",
                "req_auto_1",
                "请帮我设计一套企业级分布式缓存架构方案",
                auth_session_id="auth_usr_rd",
            )
            leases = await run_repo.claim("w1", lease_seconds=60, execution_seconds=120, limit=1)
            await executor.execute(leases[0])
            await executor.wait_background_tasks()

            assert len(provider.complete_requests) == 1
            assert provider.complete_requests[0][0] == "llmprof_compress"
            updated_sess = await sessions_repo.get_visible(
                cast(Any, type("_Scope", (), {"session_id": "sess_auto_1", "owner_id": "usr_rd"})())
            )
            assert updated_sess is not None
            assert updated_sess.title == "分布式缓存架构方案"

            # 2) 生成标题途中用户手动改名：WHERE title IS NULL 条件更新绝不覆盖用户标题。
            started_gen = asyncio.Event()
            release_gen = asyncio.Event()

            async def _slow_title_complete(
                runtime: RuntimeModelProfile, request: LlmRequest
            ) -> LlmResponse:
                del runtime, request
                started_gen.set()
                await release_gen.wait()
                return _text_response("迟到的自动标题")

            provider_race = _ScriptedLlmProvider(
                stream_responses=[_text_response("主回复完成。")],
                complete_handler=_slow_title_complete,
            )
            executor_race = RunExecutor(
                run_repo,
                profile_repo,
                provider_race,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=tool_runtime,
                sessions=sessions_repo,
            )
            await run_service.submit(
                "sess_manual_race",
                "usr_rd",
                "req_race_1",
                "帮我制定季度 OKR 计划",
                auth_session_id="auth_usr_rd",
            )
            leases_race = await run_repo.claim(
                "w1", lease_seconds=60, execution_seconds=120, limit=1
            )
            await executor_race.execute(leases_race[0])
            await asyncio.wait_for(started_gen.wait(), timeout=2.0)

            # 模拟用户在自动标题返回前手动修改标题。
            session_service = SessionService(sessions_repo)
            await session_service.update(
                "usr_rd",
                "sess_manual_race",
                update_title=True,
                title="用户手工设置的标题",
            )
            release_gen.set()
            await executor_race.wait_background_tasks()

            final_race_sess = await session_service.get("usr_rd", "sess_manual_race")
            assert final_race_sess.title == "用户手工设置的标题"

            # 3) 已有标题的会话：根本不触发 LLM 标题调用。
            provider_skip = _ScriptedLlmProvider(stream_responses=[_text_response("完成。")])
            executor_skip = RunExecutor(
                run_repo,
                profile_repo,
                provider_skip,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=tool_runtime,
                sessions=sessions_repo,
            )
            await run_service.submit(
                "sess_pre_titled",
                "usr_rd",
                "req_pre_titled",
                "继续聊聊方案细节",
                auth_session_id="auth_usr_rd",
            )
            leases_skip = await run_repo.claim(
                "w1", lease_seconds=60, execution_seconds=120, limit=1
            )
            await executor_skip.execute(leases_skip[0])
            await executor_skip.wait_background_tasks()
            assert provider_skip.complete_requests == []
            assert (
                await session_service.get("usr_rd", "sess_pre_titled")
            ).title == "用户预先定好的标题"
        finally:
            await engine.dispose()

    _run(scenario())


def test_auto_title_timeout_error_fallback_and_db_error_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证标题生成超时/报错时降级截取前 24 字，且标题写库异常绝不影响主 Run completed 终态。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            users = await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                for sid in ("sess_timeout", "sess_error", "sess_db_fail"):
                    session.add(
                        SessionModel(
                            id=sid,
                            owner_id="usr_rd",
                            title=None,
                            project_id=None,
                            last_message_sequence=0,
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )

            message_repo = SqlAlchemyMessageRepository(factory)
            run_repo = SqlAlchemyRunRepository(factory, message_repo)
            run_service = RunService(run_repo)
            profile_repo = SqlAlchemyModelProfileRepository(factory, cipher)
            sessions_repo = SqlAlchemySessionRepository(factory)
            session_service = SessionService(sessions_repo)
            tool_runtime = RunToolRuntimeService(
                auth=_StubAuthReader(users),
                sessions=sessions_repo,
                mcp_catalog=_EmptyMcpCatalog(),
                mcp_executor=_NoopMcpExecutor(),
                knowledge=None,
                project_knowledge=None,
                access_policy=AccessPolicyService(SqlAlchemyAccessControlRepository(factory)),
            )

            # 1) 超时降级（0.05s 超时，模型休眠 1s -> 降级截取用户消息前 24 字）。
            async def _hanging_complete(
                runtime: RuntimeModelProfile, request: LlmRequest
            ) -> LlmResponse:
                del runtime, request
                await asyncio.sleep(1.0)
                return _text_response("不会到达")

            provider_timeout = _ScriptedLlmProvider(
                stream_responses=[_text_response("主回复成功。")],
                complete_handler=_hanging_complete,
            )
            executor_timeout = RunExecutor(
                run_repo,
                profile_repo,
                provider_timeout,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=tool_runtime,
                sessions=sessions_repo,
                auto_title_timeout_seconds=0.05,
            )
            long_prompt = "帮我分析一下今年第三季度华东区企业客户留存率下降的核心原因和应对方案"
            await run_service.submit(
                "sess_timeout",
                "usr_rd",
                "req_timeout_1",
                long_prompt,
                auth_session_id="auth_usr_rd",
            )
            leases_t = await run_repo.claim("w1", lease_seconds=60, execution_seconds=120, limit=1)
            await executor_timeout.execute(leases_t[0])
            await executor_timeout.wait_background_tasks()

            # 未配置压缩策略时自动复用本次 Run 模型，超时后降级前 24 字。
            assert provider_timeout.complete_requests[0][0] == "llmprof_default"
            sess_t = await session_service.get("usr_rd", "sess_timeout")
            assert sess_t.title == long_prompt[:24]

            # 2) 模型抛错降级。
            async def _failing_complete(
                runtime: RuntimeModelProfile, request: LlmRequest
            ) -> LlmResponse:
                del runtime, request
                raise LlmProviderError("llm_temporary_upstream_error")

            provider_err = _ScriptedLlmProvider(
                stream_responses=[_text_response("主回复成功。")],
                complete_handler=_failing_complete,
            )
            executor_err = RunExecutor(
                run_repo,
                profile_repo,
                provider_err,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=tool_runtime,
                sessions=sessions_repo,
            )
            await run_service.submit(
                "sess_error",
                "usr_rd",
                "req_err_1",
                "  请帮我写一份\n新员工入职指引文档模板与注意事项清单  ",
                auth_session_id="auth_usr_rd",
            )
            leases_e = await run_repo.claim("w1", lease_seconds=60, execution_seconds=120, limit=1)
            await executor_err.execute(leases_e[0])
            await executor_err.wait_background_tasks()

            sess_e = await session_service.get("usr_rd", "sess_error")
            assert sess_e.title == "请帮我写一份 新员工入职指引文档模板与注意事项清"

            # 3) set_title_if_empty 抛数据库异常：完全隔离，主 Run 保持 completed，无未回收 Task。
            class _FailingTitleSessionRepo(SqlAlchemySessionRepository):
                async def set_title_if_empty(
                    self, owner_id: str, session_id: str, title: str
                ) -> bool:
                    del owner_id, session_id, title
                    raise RuntimeError("simulated_title_db_error")

            failing_sessions = _FailingTitleSessionRepo(factory)
            provider_db_fail = _ScriptedLlmProvider(
                stream_responses=[_text_response("主回复成功。")],
            )
            executor_db_fail = RunExecutor(
                run_repo,
                profile_repo,
                provider_db_fail,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=tool_runtime,
                sessions=failing_sessions,
            )
            submitted_fail = await run_service.submit(
                "sess_db_fail",
                "usr_rd",
                "req_db_fail_1",
                "写库失败隔离测试消息",
                auth_session_id="auth_usr_rd",
            )
            leases_f = await run_repo.claim("w1", lease_seconds=60, execution_seconds=120, limit=1)
            await executor_db_fail.execute(leases_f[0])
            await executor_db_fail.wait_background_tasks()

            run_final = await run_repo.get("sess_db_fail", "usr_rd", submitted_fail.run.id)
            assert run_final is not None
            assert run_final.status is RunStatus.COMPLETED
            assert (await session_service.get("usr_rd", "sess_db_fail")).title is None
        finally:
            await engine.dispose()

    _run(scenario())


def test_subagent_run_never_triggers_auto_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """子 Run（depth >= 1）完成时绝不触发自动标题生成。"""

    async def scenario() -> None:
        engine, factory, cipher = await _create_pg_db(monkeypatch)
        try:
            users = await _seed_base_org_and_prompts(factory, cipher)
            async with factory() as session, session.begin():
                session.add(
                    SessionModel(
                        id="sess_sub_title",
                        owner_id="usr_rd",
                        title=None,
                        project_id=None,
                        last_message_sequence=0,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                session.add(
                    SubagentModel(
                        id="sub_title_guard",
                        slug="helper",
                        name="助手子智能体",
                        description="测试子智能体",
                        system_prompt="子智能体提示词",
                        model_profile_id=None,
                        allowed_tool_ids=[],
                        visibility_scope=VisibilityScope.COMPANY.value,
                        owner_department_id=None,
                        max_llm_requests=3,
                        timeout_seconds=60,
                        enabled=True,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

            message_repo = SqlAlchemyMessageRepository(factory)
            run_repo = SqlAlchemyRunRepository(factory, message_repo)
            run_service = RunService(run_repo)
            profile_repo = SqlAlchemyModelProfileRepository(factory, cipher)
            sessions_repo = SqlAlchemySessionRepository(factory)
            subagents_repo = SqlAlchemySubagentRepository(factory)
            tool_runtime = RunToolRuntimeService(
                auth=_StubAuthReader(users),
                sessions=sessions_repo,
                mcp_catalog=_EmptyMcpCatalog(),
                mcp_executor=_NoopMcpExecutor(),
                knowledge=None,
                project_knowledge=None,
                subagents=subagents_repo,
                access_policy=AccessPolicyService(SqlAlchemyAccessControlRepository(factory)),
            )

            provider = _ScriptedLlmProvider(
                stream_responses=[
                    _tool_call_response(
                        "call_sub_title",
                        "dispatch_subagents",
                        {"tasks": [{"subagent_slug": "helper", "task": "子任务内容"}]},
                    ),
                    _text_response("子任务已完成。"),
                ]
            )
            executor = RunExecutor(
                run_repo,
                profile_repo,
                provider,
                system_prompts=FakeSystemPromptRepository(),
                tool_runtime=tool_runtime,
                sessions=sessions_repo,
                subagents=subagents_repo,
            )
            await run_service.submit(
                "sess_sub_title",
                "usr_rd",
                "req_sub_title",
                "派发一个子任务",
                auth_session_id="auth_usr_rd",
            )
            parent_leases = await run_repo.claim(
                "w1", lease_seconds=60, execution_seconds=120, limit=1
            )
            await executor.execute(parent_leases[0])

            # 仅执行子 Run 并等待完成：子 Run 完成后不得调用标题生成，会话标题仍为空。
            sub_leases = await run_repo.claim(
                "w1", lease_seconds=60, execution_seconds=120, limit=1
            )
            assert len(sub_leases) == 1
            assert sub_leases[0].run.depth == 1
            await executor.execute(sub_leases[0])
            await executor.wait_background_tasks()

            assert provider.complete_requests == []
            async with factory() as db_sess:
                row = await db_sess.get(SessionModel, "sess_sub_title")
                assert row is not None
                assert row.title is None
                sub_row = await db_sess.get(RunModel, sub_leases[0].run.id)
                assert sub_row is not None
                assert sub_row.status == RunStatus.COMPLETED.value
        finally:
            await engine.dispose()

    _run(scenario())


# ==============================================================================
# 4. Alembic 唯一 head 与离线 SQL 双向校验
# ==============================================================================


def test_alembic_single_head_and_m1_5_offline_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """校验 Alembic 唯一 head 为 0001_initial_schema，且离线 SQL 完整覆盖 M1.5 列并可逆。"""
    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/athena_test")
    config = Config(str(ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0001_initial_schema"]
    revision = script.get_revision("0001_initial_schema")
    assert revision is not None
    assert revision.down_revision is None

    upgrade_buf = StringIO()
    with redirect_stdout(upgrade_buf):
        command.upgrade(config, "head", sql=True)
    upgrade_sql = upgrade_buf.getvalue()
    assert "description VARCHAR(500)" in upgrade_sql
    assert "instructions TEXT" in upgrade_sql
    assert "default_skill_ids JSONB" in upgrade_sql
    assert "knowledge_base_ids JSONB" in upgrade_sql
    assert "visible_to_users BOOLEAN" in upgrade_sql
    assert "scenario_label VARCHAR(64)" in upgrade_sql
    assert "scenario_description VARCHAR(255)" in upgrade_sql
    assert "supports_vision BOOLEAN" in upgrade_sql
    assert "deleted_at TIMESTAMP" in upgrade_sql

    downgrade_buf = StringIO()
    with redirect_stdout(downgrade_buf):
        command.downgrade(config, "0001_initial_schema:base", sql=True)
    downgrade_sql = downgrade_buf.getvalue()
    assert "DROP TABLE llm_model_profile_versions" in downgrade_sql
    assert "DROP TABLE runs" in downgrade_sql
    assert "DROP TABLE projects" in downgrade_sql
