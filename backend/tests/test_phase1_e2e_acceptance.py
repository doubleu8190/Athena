"""Athena v2 第一期企业级底座重构（M1.1–M1.6）端到端验收自动化测试剧本。

本套件串联第一期全部 6 条核心业务闭环与全量 Alembic 迁移验证：
1. Mock 多部门登录、组织身份投影与生产安全护栏；
2. 飞书通讯录同步、部门树物化路径生成与员工离职会话实时吊销；
3. 三层权限引擎跨部门隔离、部门管理员越权矩阵与调岗实时失效；
4. 日常办公无项目对话、场景化模型目录、Run 模型快照固化、自动标题、工作偏好与显式记忆；
5. 模型保密局部更新与默认保护、MCP 保密局部更新、启停、独立重发现、批量审批与业务化待审批卡片；
6. 全量 Alembic 迁移链离线升级/第一期全量回滚 SQL 与核心表/索引/列核验。
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from auth_support import FakePasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_unit_harness import create_pg_unit_db
from sqlalchemy import DateTime, delete, func, literal, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from adapter.access import SqlAlchemyAccessControlRepository
from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.knowledge.model import (
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    KnowledgeVersionModel,
)
from adapter.knowledge.repository import KnowledgeRepository
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.org import DepartmentModel, SqlAlchemyOrgRepository, UserDepartmentModel
from adapter.preference import SqlAlchemyPreferenceRepository
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.run.model import RunModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.repository import SqlAlchemySessionRepository
from adapter.skill.repository import SqlAlchemySkillRepository
from adapter.subagent.repository import (
    SqlAlchemySubagentModelChecker,
    SqlAlchemySubagentRepository,
)
from adapter.system_prompt.model import SystemPromptSettingModel, SystemPromptVersionModel
from adapter.system_prompt.repository import SqlAlchemySystemPromptRepository
from app.access import AccessPolicyService
from app.auth import AuthService, InMemoryOAuthStateStore
from app.knowledge.service import KnowledgeService
from app.llm import ModelProfileService
from app.org import (
    MOCK_DEPT_FIN_ID,
    MOCK_DEPT_PLATFORM_ID,
    MOCK_DEPT_RD_ID,
    MOCK_DEPT_ROOT_ID,
    FeishuOrgSyncService,
    OrgService,
    seed_mock_organization,
)
from app.preference import PreferenceService
from app.project.knowledge import ProjectKnowledgeService
from app.project.service import ProjectService
from app.run.auto_title import (
    AUTO_TITLE_MAX_OUTPUT_TOKENS,
    AUTO_TITLE_TIMEOUT_SECONDS,
    build_auto_title_user_prompt,
)
from app.run.config import RunWorkerConfig
from app.run.executor import RunExecutor
from app.run.service import RunService
from app.session.service import SessionService
from app.skill.service import SkillService
from app.subagent import DefaultSubagentToolCatalog, SubagentService
from app.tool.manager import ToolManager
from app.tool.runtime import RunToolRuntimeService
from bootstrap.api import build_api_dependencies
from common.ids import new_id
from domain.auth import UserStatus
from domain.file import FileObjectScanEntry
from domain.knowledge.model import DocumentState
from domain.knowledge.ports import OriginalStore
from domain.llm.model import (
    LlmError,
    LlmRequest,
    LlmResponse,
    LlmText,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.mcp import (
    DiscoveredMcpTool,
    McpCallResult,
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolTarget,
)
from domain.org import RemoteDepartment, RemoteEmployee
from domain.run.model import Run, RunModelSnapshot, RunStatus, RunUsage
from domain.skill import RunSkillSnapshot
from domain.tool import McpToolSource, ToolExecutionContext, ToolOutcomeStatus
from infrastructure.feishu import FeishuUserIdentity
from infrastructure.knowledge.tokenizer import JiebaChineseTokenizer
from infrastructure.security.secrets import SecretCipher
from main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
BASE_NOW = datetime(2026, 12, 5, 9, 0, tzinfo=UTC)
SECRET_KEY = b"P" * 32


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步测试函数中执行异步协程。"""
    return asyncio.run(coroutine)


class _MutableClock:
    """单调可控时钟，确保创建时间、更新时间与租约判定完全可复现。"""

    def __init__(self, current: datetime = BASE_NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int = 5) -> datetime:
        self.current = self.current + timedelta(seconds=seconds)
        return self.current


class _SimpleTokenizer:
    """使用生产中文分词器切分检索词，直接对接 PostgreSQL tsvector 全文检索。"""

    def __init__(self) -> None:
        self._inner = JiebaChineseTokenizer()

    def tokenize(self, text: str) -> tuple[str, ...]:
        return self._inner.tokenize(text)


class _MemoryOriginalStore(OriginalStore):
    """内存知识库原件存储，隔离真实对象存储 I/O。"""

    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes) -> None:
        self.items[key] = content

    async def get(self, key: str) -> bytes:
        if key not in self.items:
            raise KeyError(key)
        return self.items[key]

    async def delete(self, key: str) -> None:
        self.items.pop(key, None)

    async def iter_orphan_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
    ) -> AsyncIterator[str]:
        del older_than_seconds
        if False:
            yield ""


class _MemoryFileObjectStore:
    """内存 Skill 文件对象存储，满足 FileObjectStore 协议。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def start(self) -> None:
        return None

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        del content_type
        self.objects[key] = path.read_bytes()

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        del content_type
        self.objects[key] = content

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        return self.objects[key][:maximum_bytes]

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        return self.objects[key][offset : offset + length]

    async def iter_range(
        self,
        key: str,
        *,
        offset: int = 0,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        del chunk_size
        yield self.objects[key][offset:]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def iter_maintenance_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
        after_key: str | None = None,
        maximum_items: int = 1000,
        prefix: str | None = None,
    ) -> AsyncIterator[FileObjectScanEntry]:
        del older_than_seconds, after_key, maximum_items, prefix
        if False:
            yield cast(FileObjectScanEntry, None)

    async def aclose(self) -> None:
        return None


class _ScriptedMcpDiscovery:
    """按目标 URL 返回预设远端工具列表的 MCP 发现替身，并记录调用时解密后的凭据。"""

    def __init__(self, tools_by_url: Mapping[str, Sequence[DiscoveredMcpTool]]) -> None:
        self.tools_by_url: dict[str, tuple[DiscoveredMcpTool, ...]] = {
            url: tuple(items) for url, items in tools_by_url.items()
        }
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def discover(self, connection: McpConnectionInput) -> tuple[DiscoveredMcpTool, ...]:
        url = getattr(connection.config, "url", "")
        self.calls.append((str(url), dict(connection.secrets)))
        return self.tools_by_url.get(str(url), ())


class _RecordingMcpExecutor:
    """记录远端工具调用轨迹的 MCP 执行器替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[McpRuntimeToolTarget, dict[str, object]]] = []

    async def call(
        self,
        target: McpRuntimeToolTarget,
        arguments: dict[str, object],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        del deadline_seconds
        self.calls.append((target, dict(arguments)))
        return McpCallResult(
            McpCallStatus.OK,
            "mcp_tool_completed",
            f"executed:{target.remote_name}",
            text=f"ok:{target.remote_name}",
        )


class _FakeFeishuClient:
    """同时满足飞书 OAuth 授权换码与通讯录拉取协议的测试替身。"""

    def __init__(
        self,
        *,
        departments: Sequence[RemoteDepartment],
        users_by_dept: dict[str, Sequence[RemoteEmployee]],
        identities_by_code: dict[str, FeishuUserIdentity],
    ) -> None:
        self.departments = list(departments)
        self.users_by_dept = {k: list(v) for k, v in users_by_dept.items()}
        self.identities_by_code = dict(identities_by_code)

    def build_authorize_url(self, state: str) -> str:
        return f"https://open.feishu.cn/open-apis/authen/v1/authorize?state={state}"

    async def exchange_code_for_user(self, code: str) -> FeishuUserIdentity:
        return self.identities_by_code[code]

    async def list_departments(self) -> list[RemoteDepartment]:
        return list(self.departments)

    async def list_department_users(self, department_external_id: str) -> list[RemoteEmployee]:
        return list(self.users_by_dept.get(department_external_id, ()))


class _ScriptedLlmProvider:
    """按脚本回放流式与非流式模型响应，完整记录每次调用的模型 ID 与入参。"""

    def __init__(
        self,
        *,
        stream_responses: Sequence[LlmResponse] = (),
        complete_responses: Sequence[LlmResponse] = (),
    ) -> None:
        self._stream_responses = list(stream_responses)
        self._complete_responses = list(complete_responses)
        self.stream_requests: list[tuple[str, LlmRequest]] = []
        self.complete_requests: list[tuple[str, LlmRequest]] = []

    async def complete(self, profile: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
        self.complete_requests.append((profile.version.id, request))
        if self._complete_responses:
            return self._complete_responses.pop(0)
        return _llm_response(LlmText(text="自动生成标题"))

    @asynccontextmanager
    async def stream(
        self,
        profile: RuntimeModelProfile,
        request: LlmRequest,
    ) -> AsyncIterator[AsyncIterator[ResponseCompleted]]:
        self.stream_requests.append((profile.version.id, request))
        response = (
            self._stream_responses.pop(0)
            if self._stream_responses
            else _llm_response(LlmText(text="已完成回复。"))
        )

        async def _events() -> AsyncIterator[ResponseCompleted]:
            yield ResponseCompleted(response=response)

        yield _events()


def _llm_response(
    *output: LlmText | LlmToolCall,
    model: str = "gpt-4o",
    input_tokens: int = 120,
    output_tokens: int = 40,
) -> LlmResponse:
    """构造带完整 Token 用量的标准化模型响应。"""
    return LlmResponse(
        output=output,
        status="completed",
        incomplete_reason=None,
        model=model,
        response_id=new_id("resp"),
        request_id=new_id("req"),
        usage=LlmUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def _make_skill_zip(
    *,
    slug: str,
    name: str,
    description: str,
    guide: str,
) -> bytes:
    """构造可通过 SkillPackageValidator 校验的合法 Skill ZIP 包。"""
    markdown = (
        f"---\n"
        f"name: {name}\n"
        f"slug: {slug}\n"
        f"description: {description}\n"
        f"dependencies: []\n"
        f"scripts:\n"
        f"  - path: scripts/run.py\n"
        f"    label: 执行脚本\n"
        f"    purpose: 返回验收测试结果。\n"
        f"    parameters:\n"
        f"      type: object\n"
        f"      additionalProperties: false\n"
        f"      properties: {{}}\n"
        f"---\n"
        f"{guide}\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", markdown)
        archive.writestr("scripts/run.py", 'print("ok")\n')
    return buffer.getvalue()


class _Phase1E2EHarness:
    """串联 M1.1–M1.6 全套仓储、服务、工具运行时与 FastAPI 应用的端到端测试夹具。"""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _MutableClock,
        cipher: SecretCipher,
        auth_repo: SqlAlchemyAuthRepository,
        org_repo: SqlAlchemyOrgRepository,
        access_policy: AccessPolicyService,
        auth_service: AuthService,
        org_service: OrgService,
        preference_service: PreferenceService,
        knowledge_service: KnowledgeService,
        project_repo: SqlAlchemyProjectRepository,
        project_service: ProjectService,
        project_knowledge_service: ProjectKnowledgeService,
        session_repo: SqlAlchemySessionRepository,
        session_service: SessionService,
        skill_service: SkillService,
        profile_repo: SqlAlchemyModelProfileRepository,
        profile_service: ModelProfileService,
        mcp_repo: SqlAlchemyMcpRepository,
        mcp_discovery: _ScriptedMcpDiscovery,
        mcp_executor: _RecordingMcpExecutor,
        tool_manager: ToolManager,
        subagent_repo: SqlAlchemySubagentRepository,
        subagent_service: SubagentService,
        system_prompt_repo: SqlAlchemySystemPromptRepository,
        run_repo: SqlAlchemyRunRepository,
        run_service: RunService,
        tool_runtime: RunToolRuntimeService,
        app: FastAPI,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.clock = clock
        self.cipher = cipher
        self.auth_repo = auth_repo
        self.org_repo = org_repo
        self.access_policy = access_policy
        self.auth_service = auth_service
        self.org_service = org_service
        self.preference_service = preference_service
        self.knowledge_service = knowledge_service
        self.project_repo = project_repo
        self.project_service = project_service
        self.project_knowledge_service = project_knowledge_service
        self.session_repo = session_repo
        self.session_service = session_service
        self.skill_service = skill_service
        self.profile_repo = profile_repo
        self.profile_service = profile_service
        self.mcp_repo = mcp_repo
        self.mcp_discovery = mcp_discovery
        self.mcp_executor = mcp_executor
        self.tool_manager = tool_manager
        self.subagent_repo = subagent_repo
        self.subagent_service = subagent_service
        self.system_prompt_repo = system_prompt_repo
        self.run_repo = run_repo
        self.run_service = run_service
        self.tool_runtime = tool_runtime
        self.app = app

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def seed_default_prompts(self, creator_user_id: str) -> None:
        """写入主对话与上下文压缩默认系统提示词，支撑 Run 提交与执行。"""
        async with self.session_factory() as session, session.begin():
            session.add_all(
                [
                    SystemPromptVersionModel(
                        id="sp_main_e2e",
                        kind="main",
                        content="你是 Athena 企业智能助手。",
                        created_by=creator_user_id,
                        created_at=self.clock(),
                    ),
                    SystemPromptVersionModel(
                        id="sp_comp_e2e",
                        kind="compression",
                        content="请简洁总结对话上下文。",
                        created_by=creator_user_id,
                        created_at=self.clock(),
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    SystemPromptSettingModel(kind="main", current_version_id="sp_main_e2e"),
                    SystemPromptSettingModel(kind="compression", current_version_id="sp_comp_e2e"),
                ]
            )

    async def mark_document_ready_with_chunk(self, *, document_id: str, content: str) -> None:
        """将已导入知识文档置为 ready 并写入可检索文本片段。"""
        now = self.clock()
        async with self.session_factory() as session, session.begin():
            doc = await session.scalar(
                select(KnowledgeDocumentModel).where(KnowledgeDocumentModel.id == document_id)
            )
            assert doc is not None
            version = await session.scalar(
                select(KnowledgeVersionModel).where(
                    KnowledgeVersionModel.document_id == document_id
                )
            )
            assert version is not None
            version.state = DocumentState.READY.value
            version.chunk_count = 1
            version.completed_at = now
            doc.active_version_id = version.id
            doc.pending_version_id = None
            doc.updated_at = now
            session.add(
                KnowledgeChunkModel(
                    id=new_id("kchk"),
                    document_id=document_id,
                    version_id=version.id,
                    ordinal=0,
                    content=content,
                    locator={"kind": "text"},
                    token_count=max(1, len(content)),
                    search_document=cast(
                        str,
                        func.to_tsvector(
                            "simple",
                            " ".join(JiebaChineseTokenizer().tokenize(content)) or content,
                        ),
                    ),
                    embedding=None,
                )
            )

    async def transfer_user_department(self, user_id: str, new_department_id: str) -> None:
        """模拟员工实时调岗：仅变更组织归属表，不注销现有登录 Cookie 会话。"""
        async with self.session_factory() as session, session.begin():
            await session.execute(
                delete(UserDepartmentModel).where(UserDepartmentModel.user_id == user_id)
            )
            session.add(
                UserDepartmentModel(
                    user_id=user_id,
                    department_id=new_department_id,
                    is_primary=True,
                )
            )

    async def latest_auth_session_id(self, user_id: str) -> str:
        """查询用户当前最新有效登录态的 auth_session_id。"""
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AuthSessionModel.id)
                .where(
                    AuthSessionModel.user_id == user_id,
                    AuthSessionModel.revoked_time.is_(None),
                )
                .order_by(AuthSessionModel.created_at.desc())
            )
            assert row is not None
            return str(row)

    def build_executor(self, provider: _ScriptedLlmProvider) -> RunExecutor:
        """构造挂载真实仓储、权限引擎、偏好服务与会话仓储的 RunExecutor。"""
        return RunExecutor(
            self.run_repo,
            self.profile_repo,
            provider,
            system_prompts=self.system_prompt_repo,
            tool_runtime=self.tool_runtime,
            config=RunWorkerConfig(),
            preferences=self.preference_service,
            projects=self.project_repo,
            sessions=self.session_repo,
            subagents=self.subagent_repo,
            access_policy=self.access_policy,
            clock=self.clock,
        )


async def _create_harness(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path | None = None,
    *,
    mock_enabled: bool = True,
    feishu_client: _FakeFeishuClient | None = None,
    mcp_tools_by_url: Mapping[str, Sequence[DiscoveredMcpTool]] | None = None,
) -> _Phase1E2EHarness:
    """初始化完整的第一期端到端 PostgreSQL 测试夹具（原生支持并行行锁与全文检索）。"""
    del db_path
    clock = _MutableClock(BASE_NOW)
    # 注入可控时钟，便于确定性推进创建时间与租约超时断言。
    monkeypatch.setattr(
        func,
        "clock_timestamp",
        lambda: literal(clock(), type_=DateTime(timezone=True)),
    )
    engine, session_factory = await create_pg_unit_db()
    cipher = SecretCipher(SECRET_KEY, key_version=1)

    auth_repo = SqlAlchemyAuthRepository(session_factory)
    org_repo = SqlAlchemyOrgRepository(session_factory)
    access_repo = SqlAlchemyAccessControlRepository(session_factory)
    access_policy = AccessPolicyService(access_repo, clock=clock)

    oauth_state_store = InMemoryOAuthStateStore() if feishu_client is not None else None
    feishu_sync_service = (
        FeishuOrgSyncService(feishu_client, org_repo, clock=clock)
        if feishu_client is not None
        else None
    )
    auth_service = AuthService(
        auth_repo,
        FakePasswordHasher(),
        org_reader=org_repo,
        feishu_client=feishu_client,
        oauth_state_store=oauth_state_store,
        mock_enabled=mock_enabled,
        clock=clock,
    )
    org_service = OrgService(
        org_repo,
        auth_repo,
        feishu_sync_service=feishu_sync_service,
        clock=clock,
    )
    preference_service = PreferenceService(
        SqlAlchemyPreferenceRepository(session_factory),
        clock=clock,
    )
    knowledge_repo = KnowledgeRepository(session_factory)
    knowledge_service = KnowledgeService(
        knowledge_repo,
        _SimpleTokenizer(),
        embedding=None,
        profile_id="bge-m3-default",
        originals=_MemoryOriginalStore(),
        access_policy=access_policy,
        clock=clock,
    )
    project_repo = SqlAlchemyProjectRepository(session_factory)
    project_service = ProjectService(project_repo)
    project_knowledge_service = ProjectKnowledgeService(
        project_service,
        knowledge_repo,
        access_policy=access_policy,
    )
    session_repo = SqlAlchemySessionRepository(session_factory)
    session_service = SessionService(session_repo)
    skill_repo = SqlAlchemySkillRepository(session_factory)
    skill_service = SkillService(
        skill_repo,
        _MemoryFileObjectStore(),
        auth_repo,
        access_policy=access_policy,
        clock=clock,
    )
    profile_repo = SqlAlchemyModelProfileRepository(session_factory, cipher, clock=clock)
    profile_service = ModelProfileService(profile_repo, auth_repo, clock=clock)
    mcp_repo = SqlAlchemyMcpRepository(session_factory, cipher, clock=clock)
    mcp_discovery = _ScriptedMcpDiscovery(mcp_tools_by_url or {})
    mcp_executor = _RecordingMcpExecutor()
    tool_manager = ToolManager(
        mcp_repo,
        mcp_discovery,
        auth_repo,
        access_policy=access_policy,
        clock=clock,
    )
    subagent_repo = SqlAlchemySubagentRepository(session_factory)
    subagent_service = SubagentService(
        subagent_repo,
        auth_repo,
        access_policy=access_policy,
        model_checker=SqlAlchemySubagentModelChecker(session_factory),
        tool_catalog=DefaultSubagentToolCatalog(mcp_repo),
        clock=clock,
    )
    system_prompt_repo = SqlAlchemySystemPromptRepository(session_factory)
    message_repo = SqlAlchemyMessageRepository(session_factory)
    run_repo = SqlAlchemyRunRepository(session_factory, message_repo)
    run_service = RunService(run_repo, clock=clock)
    tool_runtime = RunToolRuntimeService(
        auth=auth_repo,
        sessions=session_repo,
        mcp_catalog=mcp_repo,
        mcp_executor=mcp_executor,
        knowledge=knowledge_service,
        project_knowledge=project_knowledge_service,
        preferences=preference_service,
        skills=skill_service,
        subagents=subagent_repo,
        access_policy=access_policy,
        clock=clock,
    )

    # 通过真实应用工厂创建完整 FastAPI 实例，保留前置鉴权中间件与全部路由。
    app = create_app(
        project_service=project_service,
        session_service=session_service,
        run_service=run_service,
        auth_service=auth_service,
        tool_manager=tool_manager,
        model_profile_service=profile_service,
        preference_service=preference_service,
        knowledge_service=knowledge_service,
        skill_service=skill_service,
        subagent_service=subagent_service,
        org_service=org_service,
    )
    # create_app 仅在自建 database 时挂载项目知识库路由，测试场景补挂以保持路由完整。
    from api.project_knowledge import build_project_knowledge_router

    app.include_router(build_project_knowledge_router(project_knowledge_service, project_service))

    return _Phase1E2EHarness(
        engine=engine,
        session_factory=session_factory,
        clock=clock,
        cipher=cipher,
        auth_repo=auth_repo,
        org_repo=org_repo,
        access_policy=access_policy,
        auth_service=auth_service,
        org_service=org_service,
        preference_service=preference_service,
        knowledge_service=knowledge_service,
        project_repo=project_repo,
        project_service=project_service,
        project_knowledge_service=project_knowledge_service,
        session_repo=session_repo,
        session_service=session_service,
        skill_service=skill_service,
        profile_repo=profile_repo,
        profile_service=profile_service,
        mcp_repo=mcp_repo,
        mcp_discovery=mcp_discovery,
        mcp_executor=mcp_executor,
        tool_manager=tool_manager,
        subagent_repo=subagent_repo,
        subagent_service=subagent_service,
        system_prompt_repo=system_prompt_repo,
        run_repo=run_repo,
        run_service=run_service,
        tool_runtime=tool_runtime,
        app=app,
    )


def _mock_login(app: FastAPI, user_id: str) -> TestClient:
    """以指定 Mock 账号登录并返回携带真实 HttpOnly Cookie 的客户端。"""
    client = TestClient(app)
    resp = client.post("/api/v1/auth/mock/login", json={"user_id": user_id})
    assert resp.status_code == 200, resp.text
    assert "httponly" in resp.headers["set-cookie"].lower()
    return client


def _create_model(
    admin_client: TestClient,
    *,
    name: str,
    model: str,
    api_key: str,
    visible_to_users: bool = True,
    scenario_label: str | None = None,
    scenario_description: str | None = None,
    supports_parallel_tool_calls: bool = True,
) -> dict[str, Any]:
    """通过管理后台接口创建模型配置版本并返回响应载荷。"""
    resp = admin_client.post(
        "/api/v1/admin/llm/model-profiles",
        json={
            "name": name,
            "base_url": "https://llm.example.com/v1",
            "model": model,
            "api_key": api_key,
            "context_window_tokens": 128000,
            "tokenizer_encoding": "o200k_base",
            "supports_stream": True,
            "supports_tools": True,
            "supports_parallel_tool_calls": supports_parallel_tool_calls,
            "max_output_tokens": 4096,
            "visible_to_users": visible_to_users,
            "scenario_label": scenario_label,
            "scenario_description": scenario_description,
        },
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


# ---------------------------------------------------------------------------
# 闭环 1：Mock 多部门登录与安全护栏闭环
# ---------------------------------------------------------------------------


def test_e2e_1_mock_multi_dept_login_and_security_guardrails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 Mock 组织幂等预置、多角色真实 Cookie 登录、/auth/me 投影、关闭开放注册与生产护栏。"""
    h = _run(_create_harness(monkeypatch, tmp_path / "e2e1.db", mock_enabled=True))
    try:
        # 1. 连续两次执行 Mock 组织与账号预置，确保幂等不产生重复部门或重复账号。
        _run(seed_mock_organization(h.org_repo, clock=h.clock))
        _run(seed_mock_organization(h.org_repo, clock=h.clock))

        with TestClient(h.app) as anon:
            # 2. 校验认证能力声明与 Mock 账号清单。
            providers = anon.get("/api/v1/auth/providers")
            assert providers.status_code == 200
            assert providers.json() == {
                "feishu_enabled": False,
                "local_login_enabled": True,
                "mock_enabled": True,
            }

            mock_users = anon.get("/api/v1/auth/mock/users")
            assert mock_users.status_code == 200
            mock_ids = {item["id"] for item in mock_users.json()["items"]}
            assert {
                "mock_super_admin",
                "mock_fin_admin",
                "mock_fin_member",
                "mock_rd_member",
            } <= mock_ids

            # 3. 校验开放自助注册端点已从公开白名单与路由表中彻底移除。
            reg_payload = {
                "email": "outsider@example.com",
                "password": "Password123456!",
                "display_name": "外部访客",
            }
            # 匿名请求被前置鉴权中间件直接拦截（不再位于公开白名单），已认证请求命中路由表返回 404。
            assert anon.post("/api/v1/auth/register", json=reg_payload).status_code == 401
            registered_paths = {getattr(route, "path", "") for route in h.app.routes}
            assert "/api/v1/auth/register" not in registered_paths

        # 4. 分别以超管、财务部管、财务员工、研发员工登录，校验真实 Cookie 与 /auth/me 组织投影。
        expected_profiles: list[tuple[str, str, str, list[str]]] = [
            ("mock_super_admin", "super_admin", MOCK_DEPT_ROOT_ID, []),
            ("mock_fin_admin", "dept_admin", MOCK_DEPT_FIN_ID, [MOCK_DEPT_FIN_ID]),
            ("mock_fin_member", "member", MOCK_DEPT_FIN_ID, []),
            ("mock_rd_member", "member", MOCK_DEPT_PLATFORM_ID, []),
        ]
        for user_id, expected_role, expected_dept_id, expected_managed in expected_profiles:
            with _mock_login(h.app, user_id) as client:
                if user_id == "mock_super_admin":
                    # 持有合法登录态访问 /api/v1/auth/register 明确返回 404，确认端点彻底下线。
                    assert client.post("/api/v1/auth/register", json=reg_payload).status_code == 404
                me_resp = client.get("/api/v1/auth/me")
                assert me_resp.status_code == 200
                me = me_resp.json()
                assert me["id"] == user_id
                assert me["role"] == expected_role
                assert me["managed_department_ids"] == expected_managed
                assert len(me["departments"]) == 1
                assert me["departments"][0]["id"] == expected_dept_id
                assert me["departments"][0]["is_primary"] is True
                assert expected_dept_id in me["departments"][0]["path"]
    finally:
        _run(h.aclose())

    # 5. 验证生产环境安全护栏：ATHENA_ENV=production 且 ATHENA_AUTH_MOCK_ENABLED=true 直接阻断启动。
    monkeypatch.setenv("ATHENA_ENV", "production")
    monkeypatch.setenv("ATHENA_AUTH_MOCK_ENABLED", "true")
    monkeypatch.setenv("ATHENA_REDIS_URL", "redis://127.0.0.1:6379/0")
    with pytest.raises(RuntimeError, match="auth_mock_forbidden_in_production"):
        build_api_dependencies()
    with pytest.raises(RuntimeError, match="auth_mock_forbidden_in_production"):
        create_app()


# ---------------------------------------------------------------------------
# 闭环 2：飞书通讯录同步离职吊销闭环
# ---------------------------------------------------------------------------


def test_e2e_2_feishu_org_sync_and_offboarding_revocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证飞书全量同步构建物化 path、员工登录持 Cookie 后离职同步立即置 disabled 并吊销 Cookie。"""
    initial_departments = (
        RemoteDepartment(
            external_id="od_hq",
            parent_external_id="0",
            name="星辰集团",
            order_index=1,
        ),
        RemoteDepartment(
            external_id="od_fin",
            parent_external_id="od_hq",
            name="财务中心",
            order_index=10,
        ),
        RemoteDepartment(
            external_id="od_tax",
            parent_external_id="od_fin",
            name="税务核算组",
            order_index=11,
        ),
    )
    leaver = RemoteEmployee(
        union_id="on_leaver",
        open_id="ou_leaver",
        name="张离职",
        email="leaver@corp.example",
        employee_no="E2001",
        job_title="税务专员",
        department_external_ids=("od_tax",),
        is_active=True,
    )
    stayer = RemoteEmployee(
        union_id="on_stayer",
        open_id="ou_stayer",
        name="李在职",
        email="stayer@corp.example",
        employee_no="E2002",
        job_title="财务经理",
        department_external_ids=("od_fin",),
        is_active=True,
    )
    fake_feishu = _FakeFeishuClient(
        departments=initial_departments,
        users_by_dept={
            "od_fin": (stayer,),
            "od_tax": (leaver,),
        },
        identities_by_code={
            "code-leaver": FeishuUserIdentity(
                union_id="on_leaver",
                open_id="ou_leaver",
                name="张离职",
                email="leaver@corp.example",
                employee_no="E2001",
                job_title="税务专员",
                avatar_url=None,
            ),
            "code-stayer": FeishuUserIdentity(
                union_id="on_stayer",
                open_id="ou_stayer",
                name="李在职",
                email="stayer@corp.example",
                employee_no="E2002",
                job_title="财务经理",
                avatar_url=None,
            ),
        },
    )

    h = _run(
        _create_harness(
            monkeypatch,
            tmp_path / "e2e2.db",
            mock_enabled=False,
            feishu_client=fake_feishu,
        )
    )
    try:
        sync_service = FeishuOrgSyncService(fake_feishu, h.org_repo, clock=h.clock)

        # 1. 首次全量同步：入职建立部门树（含物化 path）与在职员工。
        first_summary = _run(sync_service.sync())
        assert first_summary.departments_upserted == 3
        assert first_summary.users_upserted == 2
        assert first_summary.users_disabled == 0

        # 校验部门物化 path 层级正确。
        async def _load_dept_paths() -> dict[str, tuple[str, str]]:
            async with h.session_factory() as session:
                rows = (await session.scalars(select(DepartmentModel))).all()
                return {row.external_id: (row.id, row.path) for row in rows}

        dept_map = _run(_load_dept_paths())
        hq_id, hq_path = dept_map["od_hq"]
        fin_id, fin_path = dept_map["od_fin"]
        tax_id, tax_path = dept_map["od_tax"]
        assert hq_path == f"/{hq_id}/"
        assert fin_path == f"/{hq_id}/{fin_id}/"
        assert tax_path == f"/{hq_id}/{fin_id}/{tax_id}/"

        # 2. 两名飞书员工分别完成 OAuth 回调登录并持有真实 HttpOnly Cookie。
        def _feishu_login(client: TestClient, code: str) -> None:
            authz = client.get(
                "/api/v1/auth/feishu/authorize",
                params={"return_to": "/chat"},
                follow_redirects=False,
            )
            assert authz.status_code == 302
            state = parse_qs(urlsplit(authz.headers["location"]).query)["state"][0]
            cb = client.get(
                "/api/v1/auth/feishu/callback",
                params={"code": code, "state": state},
                follow_redirects=False,
            )
            assert cb.status_code == 302
            assert cb.headers["location"] == "/chat"
            assert "httponly" in cb.headers["set-cookie"].lower()

        with TestClient(h.app) as leaver_client, TestClient(h.app) as stayer_client:
            _feishu_login(leaver_client, "code-leaver")
            _feishu_login(stayer_client, "code-stayer")

            leaver_me = leaver_client.get("/api/v1/auth/me")
            assert leaver_me.status_code == 200
            assert leaver_me.json()["departments"][0]["path"] == tax_path
            assert stayer_client.get("/api/v1/auth/me").status_code == 200

            # 3. 模拟张离职从飞书在职花名册移除，再次执行全量同步。
            fake_feishu.users_by_dept = {"od_fin": [stayer], "od_tax": []}
            h.clock.advance(60)
            second_summary = _run(sync_service.sync())
            assert second_summary.users_disabled == 1
            assert second_summary.sessions_revoked >= 1

            # 4. 校验离职员工数据库状态立即为 disabled，且其历史 Cookie 立即 401 失效。
            async def _get_leaver_status() -> str:
                async with h.session_factory() as session:
                    row = await session.scalar(
                        select(UserModel).where(UserModel.external_open_id == "ou_leaver")
                    )
                    assert row is not None
                    return row.status

            assert _run(_get_leaver_status()) == UserStatus.DISABLED.value
            revoked_me = leaver_client.get("/api/v1/auth/me")
            assert revoked_me.status_code == 401
            assert revoked_me.json() == {"detail": "not_authenticated"}

            # 校验在职同事的历史 Cookie 丝毫不受影响。
            assert stayer_client.get("/api/v1/auth/me").status_code == 200
    finally:
        _run(h.aclose())


# ---------------------------------------------------------------------------
# 闭环 3：三层权限跨部门隔离与调岗实时失效闭环
# ---------------------------------------------------------------------------


def test_e2e_3_three_tier_access_isolation_and_transfer_revocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证公司/部门两级四大资产跨部门隔离、部管越权 403/404 以及调岗后同 Cookie 实时撤权。"""
    mcp_tools = {
        "https://mcp.company.example/mcp": (
            DiscoveredMcpTool(
                remote_name="co_tool",
                description="公司公共工具",
                input_schema={"type": "object", "properties": {}},
            ),
        ),
        "https://mcp.fin.example/mcp": (
            DiscoveredMcpTool(
                remote_name="fin_tool",
                description="财务部专属工具",
                input_schema={"type": "object", "properties": {}},
            ),
        ),
    }
    h = _run(
        _create_harness(
            monkeypatch,
            tmp_path / "e2e3.db",
            mock_enabled=True,
            mcp_tools_by_url=mcp_tools,
        )
    )
    try:
        _run(seed_mock_organization(h.org_repo, clock=h.clock))

        with (
            _mock_login(h.app, "mock_super_admin") as super_client,
            _mock_login(h.app, "mock_fin_admin") as fin_admin,
            _mock_login(h.app, "mock_rd_admin") as rd_admin,
            _mock_login(h.app, "mock_fin_member") as fin_member,
            _mock_login(h.app, "mock_rd_member") as rd_member,
        ):
            # 1. 创建四类企业资产：公司级（超管）+ 财务部级（财务部管/超管）+ 研发部级（研发部管）。
            co_kb = super_client.post(
                "/api/v1/knowledge-bases",
                json={"name": "公司制度库", "visibility_scope": "company"},
            )
            assert co_kb.status_code == 201
            co_kb_id: str = co_kb.json()["id"]

            fin_kb = fin_admin.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务核算知识库",
                    "visibility_scope": "department",
                    "owner_department_id": MOCK_DEPT_FIN_ID,
                },
            )
            assert fin_kb.status_code == 201
            fin_kb_id: str = fin_kb.json()["id"]
            fin_doc = fin_admin.post(
                f"/api/v1/knowledge-bases/{fin_kb_id}/documents/text",
                headers={"Idempotency-Key": "e2e3-fin-doc"},
                json={"title": "发票审核规范", "text": "财务增值税专用发票须三单匹配复核。"},
            )
            assert fin_doc.status_code == 202
            _run(
                h.mark_document_ready_with_chunk(
                    document_id=fin_doc.json()["document_id"],
                    content="财务增值税专用发票须三单匹配复核。",
                )
            )

            rd_kb = rd_admin.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "研发架构知识库",
                    "visibility_scope": "department",
                    "owner_department_id": MOCK_DEPT_RD_ID,
                },
            )
            assert rd_kb.status_code == 201
            rd_kb_id: str = rd_kb.json()["id"]

            # 导入并启用公司公共技能与财务部技能。
            def _import_enable_skill(
                client: TestClient,
                *,
                slug: str,
                name: str,
                scope: str,
                owner_department_id: str | None,
                idem_key: str,
            ) -> str:
                form_data: dict[str, str] = {"scope": scope}
                if owner_department_id is not None:
                    form_data["owner_department_id"] = owner_department_id
                imp = client.post(
                    "/api/v1/admin/skills/import",
                    headers={"Idempotency-Key": idem_key},
                    data=form_data,
                    files={
                        "package": (
                            f"{slug}.zip",
                            _make_skill_zip(
                                slug=slug,
                                name=name,
                                description=f"{name}说明",
                                guide=f"{name}执行指南",
                            ),
                            "application/zip",
                        )
                    },
                )
                assert imp.status_code == 201, imp.text
                body = imp.json()
                skill_id = str(body["id"])
                version_id = str(body["versions"][0]["id"])
                cfg = client.patch(
                    f"/api/v1/admin/skills/{skill_id}",
                    json={
                        "expected_revision": body["revision"],
                        "current_version_id": version_id,
                        "enabled": True,
                        "script_requires_approval": False,
                    },
                )
                assert cfg.status_code == 200, cfg.text
                return skill_id

            co_skill_id = _import_enable_skill(
                super_client,
                slug="co-office-skill",
                name="公司通用周报技能",
                scope="company",
                owner_department_id=None,
                idem_key="e2e3-co-skill",
            )
            fin_skill_id = _import_enable_skill(
                fin_admin,
                slug="fin-reconcile-skill",
                name="财务对账技能",
                scope="department",
                owner_department_id=MOCK_DEPT_FIN_ID,
                idem_key="e2e3-fin-skill",
            )

            # 注册公司公共 MCP 与财务部专属 MCP。
            co_mcp = super_client.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "CompanyMCP",
                    "transport": "streamable_http",
                    "url": "https://mcp.company.example/mcp",
                    "secrets": {},
                    "visibility_scope": "company",
                },
            )
            assert co_mcp.status_code == 201
            fin_mcp = super_client.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "FinanceMCP",
                    "transport": "streamable_http",
                    "url": "https://mcp.fin.example/mcp",
                    "secrets": {},
                    "visibility_scope": "department",
                    "owner_department_id": MOCK_DEPT_FIN_ID,
                },
            )
            assert fin_mcp.status_code == 201
            co_tool_id = str(
                super_client.get(f"/api/v1/admin/mcp/servers/{co_mcp.json()['id']}/tools").json()[
                    "items"
                ][0]["tool_id"]
            )
            fin_tool_id = str(
                super_client.get(f"/api/v1/admin/mcp/servers/{fin_mcp.json()['id']}/tools").json()[
                    "items"
                ][0]["tool_id"]
            )

            # 创建公司公共子智能体与财务专属子智能体。
            assert (
                super_client.post(
                    "/api/v1/admin/subagents",
                    json={
                        "slug": "co_agent",
                        "name": "公司公共助手",
                        "description": "处理公司通用事项",
                        "system_prompt": "协助完成公司通用问答。",
                        "visibility_scope": "company",
                    },
                ).status_code
                == 201
            )
            assert (
                super_client.post(
                    "/api/v1/admin/subagents",
                    json={
                        "slug": "fin_agent",
                        "name": "财务审核专员",
                        "description": "处理财务部内部核算",
                        "system_prompt": "协助完成财务核算审核。",
                        "visibility_scope": "department",
                        "owner_department_id": MOCK_DEPT_FIN_ID,
                    },
                ).status_code
                == 201
            )

            # 2. 财务员工正向校验：可见公司 + 财务四类资产，可检索财务知识库。
            fin_kb_ids = {
                item["id"] for item in fin_member.get("/api/v1/knowledge-bases").json()["items"]
            }
            assert {co_kb_id, fin_kb_id} <= fin_kb_ids
            assert rd_kb_id not in fin_kb_ids

            fin_search = fin_member.post(
                f"/api/v1/knowledge-bases/{fin_kb_id}/search",
                json={"query": "增值税", "limit": 5},
            )
            assert fin_search.status_code == 200
            assert len(fin_search.json()["hits"]) == 1
            assert "三单匹配" in fin_search.json()["hits"][0]["text"]

            fin_skill_ids = {
                item["id"] for item in fin_member.get("/api/v1/skills").json()["items"]
            }
            assert {co_skill_id, fin_skill_id} <= fin_skill_ids

            fin_sess_id = str(fin_member.post("/api/v1/sessions", json={}).json()["id"])
            rd_sess_id = str(rd_member.post("/api/v1/sessions", json={}).json()["id"])
            fin_auth_sid = _run(h.latest_auth_session_id("mock_fin_member"))
            rd_auth_sid = _run(h.latest_auth_session_id("mock_rd_member"))

            fin_auth = _run(
                h.tool_runtime.authorize("mock_fin_member", fin_sess_id, "run_fin_1", fin_auth_sid)
            )
            rd_auth = _run(
                h.tool_runtime.authorize("mock_rd_member", rd_sess_id, "run_rd_1", rd_auth_sid)
            )
            assert fin_auth is not None and rd_auth is not None

            fin_snap = _run(h.tool_runtime.build_snapshot("run-v2", fin_auth, depth=0))
            rd_snap = _run(h.tool_runtime.build_snapshot("run-v2", rd_auth, depth=0))

            assert {t.tool_id for t in fin_snap.tools if isinstance(t.source, McpToolSource)} == {
                co_tool_id,
                fin_tool_id,
            }
            fin_dispatch = next(t for t in fin_snap.tools if t.model_name == "dispatch_subagents")
            assert "co_agent" in fin_dispatch.description
            assert "fin_agent" in fin_dispatch.description

            # 3. 研发员工跨部门隔离：不可见财务资源、直调知识库 404、无法调用财务 MCP/子智能体。
            rd_kb_ids = {
                item["id"] for item in rd_member.get("/api/v1/knowledge-bases").json()["items"]
            }
            assert fin_kb_id not in rd_kb_ids
            assert rd_member.get(f"/api/v1/knowledge-bases/{fin_kb_id}").status_code == 404
            assert (
                rd_member.post(
                    f"/api/v1/knowledge-bases/{fin_kb_id}/search",
                    json={"query": "增值税", "limit": 5},
                ).status_code
                == 404
            )
            assert fin_skill_id not in {
                item["id"] for item in rd_member.get("/api/v1/skills").json()["items"]
            }
            assert {t.tool_id for t in rd_snap.tools if isinstance(t.source, McpToolSource)} == {
                co_tool_id
            }
            rd_dispatch = next(t for t in rd_snap.tools if t.model_name == "dispatch_subagents")
            assert "co_agent" in rd_dispatch.description
            assert "fin_agent" not in rd_dispatch.description

            # 研发员工伪造调用财务 MCP 工具被运行期实时鉴权拦截，不触达 MCP 执行器。
            fin_tool_def = next(t for t in fin_snap.tools if t.tool_id == fin_tool_id)
            rd_mcp_result = _run(
                h.tool_runtime.execute(
                    fin_tool_def,
                    fin_tool_def.model_name,
                    {},
                    ToolExecutionContext(
                        owner_id="mock_rd_member",
                        chat_session_id=rd_sess_id,
                        project_id=None,
                        run_id="run_rd_1",
                        execution_epoch=1,
                        auth_session_id=rd_auth_sid,
                        deadline_at=h.clock() + timedelta(seconds=10),
                        tool_call_record_id="tc_rd_forged",
                    ),
                    deadline_seconds=10.0,
                    run_remaining_seconds=60.0,
                )
            )
            assert rd_mcp_result.outcome.status is ToolOutcomeStatus.ERROR
            assert len(h.mcp_executor.calls) == 0

            # 研发员工伪造调度财务子智能体被运行时权限引擎拦截。
            executor = h.build_executor(_ScriptedLlmProvider())
            rd_run = Run(
                id="run_rd_1",
                session_id=rd_sess_id,
                owner_id="mock_rd_member",
                status=RunStatus.RUNNING,
                trigger_message_id="msg_rd_1",
                context_cutoff_sequence=1,
                retry_of_run_id=None,
                model_config_version_id="llm_dummy",
                model_snapshot=RunModelSnapshot(
                    id="llm_dummy",
                    name="E2E Dummy",
                    protocol="chat_completions",
                    base_url="https://api.example.com/v1",
                    model="gpt-4o-mini",
                    context_window_tokens=128000,
                    tokenizer_encoding="cl100k_base",
                    supports_stream=True,
                    supports_tools=True,
                    supports_parallel_tool_calls=False,
                    max_output_tokens=4096,
                    request_timeout_seconds=30,
                    execution_timeout_seconds=120,
                    max_concurrency=4,
                ),
                main_prompt_version_id="sp_main_e2e",
                compression_prompt_version_id="sp_comp_e2e",
                prompt_template_version="run-v2",
                event_sequence=1,
                execution_epoch=1,
                queued_at=h.clock(),
                first_started_at=h.clock(),
                execution_started_at=h.clock(),
                finished_at=None,
                created_at=h.clock(),
                updated_at=h.clock(),
                cancel_requested_at=None,
                worker_id="w1",
                lease_expires_at=h.clock() + timedelta(seconds=60),
                execution_deadline_at=h.clock() + timedelta(seconds=120),
                safe_error_code=None,
                usage=RunUsage(),
                auth_session_id=rd_auth_sid,
                tool_snapshot=rd_snap,
                skill_snapshot=RunSkillSnapshot(),
            )
            rd_state = cast(
                Any,
                SimpleNamespace(run=rd_run, authorized=rd_auth, snapshot=rd_snap, round=0),
            )
            with pytest.raises(LlmError, match="subagent_profile_unavailable"):
                _run(
                    executor._dispatch_subagents(
                        rd_state,
                        LlmToolCall(
                            call_id="tc_rd_sub",
                            name="dispatch_subagents",
                            arguments={
                                "tasks": [{"subagent_slug": "fin_agent", "task": "越权查询"}]
                            },
                        ),
                        "",
                        _llm_response(LlmText(text="")),
                    )
                )

            # 4. 财务部门管理员越权矩阵：改公司库返回 403，改研发库返回 404（零存在性泄漏）。
            assert (
                fin_admin.patch(
                    f"/api/v1/knowledge-bases/{co_kb_id}",
                    json={"name": "尝试篡改公司库"},
                ).status_code
                == 403
            )
            assert (
                fin_admin.patch(
                    f"/api/v1/knowledge-bases/{rd_kb_id}",
                    json={"name": "尝试篡改研发库"},
                ).status_code
                == 404
            )

            # 5. 调岗实时失效：将 mock_fin_member 从财务部调岗至平台架构组，保留同一 Cookie。
            _run(h.transfer_user_department("mock_fin_member", MOCK_DEPT_PLATFORM_ID))

            after_kb_ids = {
                item["id"] for item in fin_member.get("/api/v1/knowledge-bases").json()["items"]
            }
            assert fin_kb_id not in after_kb_ids
            assert rd_kb_id in after_kb_ids
            assert fin_member.get(f"/api/v1/knowledge-bases/{fin_kb_id}").status_code == 404
            assert (
                fin_member.post(
                    f"/api/v1/knowledge-bases/{fin_kb_id}/search",
                    json={"query": "增值税", "limit": 5},
                ).status_code
                == 404
            )
            assert fin_skill_id not in {
                item["id"] for item in fin_member.get("/api/v1/skills").json()["items"]
            }
            assert fin_tool_id not in {
                item["tool_id"] for item in fin_member.get("/api/v1/tools").json()["items"]
            }

            # 即使使用调岗前已冻结的工具定义尝试执行财务 MCP，运行期实时鉴权仍立即阻断。
            post_transfer_mcp = _run(
                h.tool_runtime.execute(
                    fin_tool_def,
                    fin_tool_def.model_name,
                    {},
                    ToolExecutionContext(
                        owner_id="mock_fin_member",
                        chat_session_id=fin_sess_id,
                        project_id=None,
                        run_id="run_fin_1",
                        execution_epoch=1,
                        auth_session_id=fin_auth_sid,
                        deadline_at=h.clock() + timedelta(seconds=10),
                        tool_call_record_id="tc_after_transfer",
                    ),
                    deadline_seconds=10.0,
                    run_remaining_seconds=60.0,
                )
            )
            assert post_transfer_mcp.outcome.status is ToolOutcomeStatus.ERROR
            assert len(h.mcp_executor.calls) == 0

            # 新 Run 快照亦不再包含财务 MCP 工具与财务子智能体。
            after_snap = _run(h.tool_runtime.build_snapshot("run-v2", fin_auth, depth=0))
            assert {t.tool_id for t in after_snap.tools if isinstance(t.source, McpToolSource)} == {
                co_tool_id
            }
            after_dispatch = next(
                t for t in after_snap.tools if t.model_name == "dispatch_subagents"
            )
            assert "fin_agent" not in after_dispatch.description
    finally:
        _run(h.aclose())


# ---------------------------------------------------------------------------
# 闭环 4：日常办公无项目对话 + 场景化模型 + 自动标题 + 轻量偏好与显式记忆闭环
# ---------------------------------------------------------------------------


def test_e2e_4_projectless_dialogue_scenario_models_auto_title_and_explicit_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证场景模型脱敏目录、无项目 Run 固化快照/检索知识库/免审批记忆、自动标题与手工标题保护。"""
    h = _run(_create_harness(monkeypatch, tmp_path / "e2e4.db", mock_enabled=True))
    try:
        _run(seed_mock_organization(h.org_repo, clock=h.clock))
        _run(h.seed_default_prompts("mock_super_admin"))

        with (
            _mock_login(h.app, "mock_super_admin") as super_client,
            _mock_login(h.app, "mock_fin_admin") as fin_admin,
            _mock_login(h.app, "mock_rd_admin") as rd_admin,
            _mock_login(h.app, "mock_fin_member") as fin_member,
        ):
            # 1. 超管配置 3 个模型：默认通用模型、深度思考场景模型、内部隐藏压缩模型。
            general_model = _create_model(
                super_client,
                name="通用对话模型",
                model="gpt-4o-mini",
                api_key="sk-general-secret-sentinel",
                visible_to_users=True,
                scenario_label="日常办公",
                scenario_description="适合常规文档撰写与日常问答",
            )
            deep_model = _create_model(
                super_client,
                name="深度思考模型",
                model="o3-mini",
                api_key="sk-deep-secret-sentinel",
                visible_to_users=True,
                scenario_label="深度思考",
                scenario_description="适合复杂财务分析与方案推演",
            )
            compress_model = _create_model(
                super_client,
                name="内部压缩小模型",
                model="gpt-4o-nano",
                api_key="sk-compress-secret-sentinel",
                visible_to_users=False,
                scenario_label="内部压缩",
            )
            assert (
                super_client.put(
                    "/api/v1/admin/llm/model-profiles/default",
                    json={"profile_id": general_model["id"]},
                ).status_code
                == 200
            )
            assert (
                super_client.put(
                    "/api/v1/admin/llm/compression-policy",
                    json={"compression_model_profile_id": compress_model["id"]},
                ).status_code
                == 200
            )

            # 2. 普通员工查询 GET /api/v1/models：仅返回可见可对话模型，严禁暴露 base_url 与凭据。
            catalog_resp = fin_member.get("/api/v1/models")
            assert catalog_resp.status_code == 200
            catalog = catalog_resp.json()
            assert catalog["default_model_id"] == general_model["id"]
            returned_ids = {item["id"] for item in catalog["items"]}
            assert returned_ids == {general_model["id"], deep_model["id"]}
            assert compress_model["id"] not in returned_ids
            for item in catalog["items"]:
                assert set(item.keys()) == {
                    "id",
                    "name",
                    "scenario_label",
                    "scenario_description",
                    "is_default",
                    "supports_vision",
                }
            assert "sk-" not in catalog_resp.text
            assert "llm.example.com" not in catalog_resp.text

            # 3. 准备公司公共知识库、财务部知识库与研发部知识库（验证无项目检索仅覆盖公司+本部门）。
            co_kb = super_client.post(
                "/api/v1/knowledge-bases",
                json={"name": "公司差旅总则", "visibility_scope": "company"},
            ).json()
            co_doc = super_client.post(
                f"/api/v1/knowledge-bases/{co_kb['id']}/documents/text",
                headers={"Idempotency-Key": "e2e4-co-doc"},
                json={"title": "差旅制度", "text": "一线城市差旅住宿标准为每晚 600 元。"},
            ).json()
            _run(
                h.mark_document_ready_with_chunk(
                    document_id=co_doc["document_id"],
                    content="一线城市差旅住宿标准为每晚 600 元。",
                )
            )

            fin_kb = fin_admin.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务报销细则",
                    "visibility_scope": "department",
                    "owner_department_id": MOCK_DEPT_FIN_ID,
                },
            ).json()
            fin_doc = fin_admin.post(
                f"/api/v1/knowledge-bases/{fin_kb['id']}/documents/text",
                headers={"Idempotency-Key": "e2e4-fin-doc"},
                json={"title": "报销审核细则", "text": "差旅报销单须附电子行程单并在 7 日内提交。"},
            ).json()
            _run(
                h.mark_document_ready_with_chunk(
                    document_id=fin_doc["document_id"],
                    content="差旅报销单须附电子行程单并在 7 日内提交。",
                )
            )

            rd_kb = rd_admin.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "研发机密库",
                    "visibility_scope": "department",
                    "owner_department_id": MOCK_DEPT_RD_ID,
                },
            ).json()
            rd_doc = rd_admin.post(
                f"/api/v1/knowledge-bases/{rd_kb['id']}/documents/text",
                headers={"Idempotency-Key": "e2e4-rd-doc"},
                json={"title": "研发差旅加密脚本", "text": "研发差旅内部密钥 RD-ONLY-SECRET。"},
            ).json()
            _run(
                h.mark_document_ready_with_chunk(
                    document_id=rd_doc["document_id"],
                    content="研发差旅内部密钥 RD-ONLY-SECRET。",
                )
            )

            # 4. 读写个人工作偏好接口（GET/PUT /api/v1/me/preferences）。
            pref_default = fin_member.get("/api/v1/me/preferences")
            assert pref_default.status_code == 200
            assert pref_default.json()["memory_enabled"] is True

            pref_put = fin_member.put(
                "/api/v1/me/preferences",
                json={
                    "custom_instructions": "我是财务专员，请先给出结论再列制度依据。",
                    "memory_enabled": True,
                },
            )
            assert pref_put.status_code == 200
            assert (
                pref_put.json()["custom_instructions"] == "我是财务专员，请先给出结论再列制度依据。"
            )

            # 5. 创建无项目会话（project_id=None, title=None），指定深度思考场景模型提交 Run。
            sess_resp = fin_member.post("/api/v1/sessions", json={})
            assert sess_resp.status_code == 201
            session_id = str(sess_resp.json()["id"])
            assert sess_resp.json()["project_id"] is None
            assert sess_resp.json()["title"] is None

            submit_resp = fin_member.post(
                f"/api/v1/sessions/{session_id}/runs",
                json={
                    "request_id": "req-e2e4-run1",
                    "text": "帮我查一下一线城市差旅报销标准，并记住我后续周报都用万元为单位。",
                    "model_profile_id": deep_model["id"],
                },
            )
            assert submit_resp.status_code == 201, submit_resp.text
            run_body = submit_resp.json()
            run_id = str(run_body["id"])

            # 校验 Run 固化 model_snapshot 且严禁包含密钥或供应商 URL。
            assert run_body["model_config_version_id"] == deep_model["id"]
            assert run_body["model_snapshot"] == {
                "id": deep_model["id"],
                "name": "深度思考模型",
                "model": "o3-mini",
                "scenario_label": "深度思考",
                "supports_vision": False,
                "version": 1,
            }

            async def _get_raw_snapshot() -> dict[str, Any]:
                async with h.session_factory() as session:
                    row = await session.scalar(select(RunModel).where(RunModel.id == run_id))
                    assert row is not None and row.model_snapshot_json is not None
                    return cast(dict[str, Any], row.model_snapshot_json)

            # 校验 HTTP 模型快照隐藏供应商地址与凭据，库内 model_snapshot_json 亦严禁包含 api_key。
            assert "base_url" not in run_body["model_snapshot"]
            assert "api_key" not in run_body["model_snapshot"]
            persisted_snapshot = _run(_get_raw_snapshot())
            serialized_snap = json.dumps(persisted_snapshot, ensure_ascii=False)
            assert "api_key" not in serialized_snap
            assert "encrypted_api_key" not in serialized_snap
            assert "sk-deep-secret-sentinel" not in serialized_snap

            # 6. 运行真实 RunExecutor：验证暴露 knowledge_search 与 save_user_memory 及免审批落库。
            provider = _ScriptedLlmProvider(
                stream_responses=[
                    _llm_response(
                        LlmToolCall(
                            call_id="tc_kb_search",
                            name="knowledge_search",
                            arguments={"query": "差旅", "limit": 5},
                        ),
                        LlmToolCall(
                            call_id="tc_save_mem",
                            name="save_user_memory",
                            arguments={"content": "周报金额统一使用万元为单位"},
                        ),
                        model="o3-mini",
                    ),
                    _llm_response(
                        LlmText(
                            text=(
                                "一线城市差旅住宿标准每晚 600 元，"
                                "报销须附电子行程单并在 7 日内提交；已记住周报单位偏好。"
                            )
                        ),
                        model="o3-mini",
                    ),
                ],
                complete_responses=[
                    _llm_response(LlmText(text='  "《差旅报销标准与周报单位偏好》。"\n')),
                ],
            )
            executor = h.build_executor(provider)

            async def _claim_and_execute() -> None:
                leases = await h.run_repo.claim(
                    "w-e2e4", lease_seconds=60, execution_seconds=120, limit=1
                )
                assert len(leases) == 1
                await executor.execute(leases[0])
                await executor.wait_background_tasks()

            _run(_claim_and_execute())

            # 校验第 1 轮传给模型的工具包含 knowledge_search 与 save_user_memory，且注入偏好指令。
            assert len(provider.stream_requests) == 2
            first_model_id, first_req = provider.stream_requests[0]
            assert first_model_id == deep_model["id"]
            tool_names = {tool.name for tool in first_req.tools}
            assert "knowledge_search" in tool_names
            assert "save_user_memory" in tool_names
            first_messages_dump = "\n".join(msg.content or "" for msg in first_req.messages)
            assert "我是财务专员，请先给出结论再列制度依据。" in first_messages_dump

            # 校验第 2 轮工具结果中包含公司库与财务库内容，绝对不含研发库内容。
            second_req = provider.stream_requests[1]
            second_req_dump = repr(second_req[1].messages)
            assert "每晚 600 元" in second_req_dump
            assert "电子行程单" in second_req_dump
            assert "RD-ONLY-SECRET" not in second_req_dump

            # 校验主 Run 顺利完成，未触发任何审批阻塞。
            finished_run = fin_member.get(f"/api/v1/sessions/{session_id}/runs/{run_id}").json()
            assert finished_run["status"] == "completed"

            # 7. 校验显式记忆通过 GET /api/v1/me/memories 可见且可通过 DELETE 删除。
            memories_resp = fin_member.get("/api/v1/me/memories")
            assert memories_resp.status_code == 200
            mem_items = memories_resp.json()["items"]
            assert len(mem_items) == 1
            assert mem_items[0]["content"] == "周报金额统一使用万元为单位"
            assert mem_items[0]["source"] == "chat_explicit"

            del_mem = fin_member.delete(f"/api/v1/me/memories/{mem_items[0]['id']}")
            assert del_mem.status_code == 204
            assert fin_member.get("/api/v1/me/memories").json()["items"] == []

            # 8. 校验自动标题优先选用压缩模型生成、预算/提示词兼容思考模型且经清洗后长度 <= 20 字。
            assert len(provider.complete_requests) == 1
            assert provider.complete_requests[0][0] == compress_model["id"]
            title_req = provider.complete_requests[0][1]
            assert title_req.max_output_tokens == AUTO_TITLE_MAX_OUTPUT_TOKENS
            assert title_req.timeout_seconds == AUTO_TITLE_TIMEOUT_SECONDS
            assert title_req.messages[1].content == build_auto_title_user_prompt(
                "帮我查一下一线城市差旅报销标准，并记住我后续周报都用万元为单位。"
            )
            titled_session = fin_member.get(f"/api/v1/sessions/{session_id}").json()
            assert titled_session["title"] == "差旅报销标准与周报单位偏好"
            assert len(titled_session["title"]) <= 20

            # 9. 校验手工标题绝对不被自动标题覆盖。
            manual_sess = fin_member.post("/api/v1/sessions", json={"title": "手工保留标题"}).json()
            manual_sid = str(manual_sess["id"])
            assert (
                fin_member.post(
                    f"/api/v1/sessions/{manual_sid}/runs",
                    json={"request_id": "req-e2e4-manual", "text": "测试不覆盖手工标题"},
                ).status_code
                == 201
            )
            provider2 = _ScriptedLlmProvider(
                stream_responses=[_llm_response(LlmText(text="已回复。"))],
                complete_responses=[_llm_response(LlmText(text="不应覆盖的标题"))],
            )
            executor2 = h.build_executor(provider2)

            async def _claim_and_execute_manual() -> None:
                leases = await h.run_repo.claim(
                    "w-e2e4", lease_seconds=60, execution_seconds=120, limit=1
                )
                assert len(leases) == 1
                await executor2.execute(leases[0])
                await executor2.wait_background_tasks()

            _run(_claim_and_execute_manual())
            assert len(provider2.complete_requests) == 0
            assert (
                fin_member.get(f"/api/v1/sessions/{manual_sid}").json()["title"] == "手工保留标题"
            )
    finally:
        _run(h.aclose())


# ---------------------------------------------------------------------------
# 闭环 5：MCP 保密局部更新 + 启停 + 独立重发现 + 批量审批 + 业务化待审批卡片闭环
# ---------------------------------------------------------------------------


def test_e2e_5_mcp_secret_patch_toggle_rediscover_batch_approval_and_business_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证模型保密更新与默认保护、MCP 保密更新、重发现、批量审批、业务审批卡片与停用下线。"""
    mcp_url = "https://erp.example.com/mcp"
    initial_tools = (
        DiscoveredMcpTool(
            remote_name="submit_expense",
            description="提交费用报销申请",
            input_schema={
                "type": "object",
                "properties": {
                    "applicant": {"type": "string"},
                    "amount": {"type": "number"},
                    "category": {"type": "string"},
                    "cost_center": {"type": "string"},
                },
                "required": ["applicant", "amount", "category"],
            },
        ),
        DiscoveredMcpTool(
            remote_name="approve_voucher",
            description="复核凭证",
            input_schema={
                "type": "object",
                "properties": {"voucher_no": {"type": "string"}},
            },
        ),
    )
    h = _run(
        _create_harness(
            monkeypatch,
            tmp_path / "e2e5.db",
            mock_enabled=True,
            mcp_tools_by_url={mcp_url: initial_tools},
        )
    )
    try:
        _run(seed_mock_organization(h.org_repo, clock=h.clock))
        _run(h.seed_default_prompts("mock_super_admin"))

        with (
            _mock_login(h.app, "mock_super_admin") as super_client,
            _mock_login(h.app, "mock_fin_member") as fin_member,
        ):
            # 1. 模型运维安全护栏：PATCH 不传 api_key 保留原密钥；删除默认模型返回 409。
            primary_model = _create_model(
                super_client,
                name="主用业务模型",
                model="gpt-4o",
                api_key="sk-keep-original-model-key",
            )
            spare_model = _create_model(
                super_client,
                name="闲置备用模型",
                model="gpt-4o-mini",
                api_key="sk-spare-model-key",
            )
            assert (
                super_client.put(
                    "/api/v1/admin/llm/model-profiles/default",
                    json={"profile_id": primary_model["id"]},
                ).status_code
                == 200
            )
            patched_model = super_client.patch(
                f"/api/v1/admin/llm/model-profiles/{primary_model['id']}",
                json={"expected_version": 1, "name": "主用业务模型-已重命名"},
            )
            assert patched_model.status_code == 200
            assert patched_model.json()["version"] == 2
            assert "has_api_key" not in patched_model.json()
            # 底层解密校验原 API Key 完整保留。
            runtime_model = _run(h.profile_repo.get_runtime(primary_model["id"]))
            assert runtime_model is not None
            assert runtime_model.api_key == "sk-keep-original-model-key"

            # 删除默认模型被强引用保护拦截（409），删除闲置模型成功（204）。
            del_default = super_client.delete(
                f"/api/v1/admin/llm/model-profiles/{primary_model['id']}"
            )
            assert del_default.status_code == 409
            del_spare = super_client.delete(f"/api/v1/admin/llm/model-profiles/{spare_model['id']}")
            assert del_spare.status_code == 204

            # 2. 注册 MCP Server 并执行保密局部更新（改名 + secrets_upsert + secrets_remove）。
            reg_resp = super_client.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "ErpExpenseMCP",
                    "transport": "streamable_http",
                    "url": mcp_url,
                    "secrets": {
                        "API_TOKEN": "tok-original-keep",
                        "SIGNING_KEY": "sig-to-remove",
                    },
                    "visibility_scope": "company",
                },
            )
            assert reg_resp.status_code == 201, reg_resp.text
            server_id = str(reg_resp.json()["id"])

            patch_mcp = super_client.patch(
                f"/api/v1/admin/mcp/servers/{server_id}",
                json={
                    "expected_config_version": 1,
                    "name": "ErpExpenseMCP-V2",
                    "secrets_upsert": {"AUDIT_KEY": "aud-added-secret"},
                    "secrets_remove": ["SIGNING_KEY"],
                },
            )
            assert patch_mcp.status_code == 200, patch_mcp.text
            patched_server = patch_mcp.json()
            assert patched_server["name"] == "ErpExpenseMCP-V2"
            assert patched_server["config_version"] == 2
            secret_names = {item["name"] for item in patched_server["secrets"]}
            assert secret_names == {"API_TOKEN", "AUDIT_KEY"}
            assert "tok-original-keep" not in patch_mcp.text
            assert "aud-added-secret" not in patch_mcp.text

            # 3. 为 submit_expense 配置中文展示信息，并批量开启两个工具的审批开关。
            tools_before = super_client.get(f"/api/v1/admin/mcp/servers/{server_id}/tools").json()[
                "items"
            ]
            submit_tool = next(
                item for item in tools_before if item["remote_name"] == "submit_expense"
            )
            approve_tool = next(
                item for item in tools_before if item["remote_name"] == "approve_voucher"
            )
            submit_tool_id = str(submit_tool["tool_id"])
            approve_tool_id = str(approve_tool["tool_id"])

            meta_resp = super_client.patch(
                f"/api/v1/admin/tools/{submit_tool_id}",
                json={
                    "display_name": "提交费用报销单",
                    "approval_summary_template": (
                        "为 {applicant} 提交 {amount} 元报销单（科目：{category}）"
                    ),
                    "parameter_labels": {
                        "applicant": "申请人",
                        "amount": "报销金额",
                        "category": "费用科目",
                    },
                },
            )
            assert meta_resp.status_code == 200

            batch_resp = super_client.patch(
                "/api/v1/admin/tools/batch-approval",
                json={
                    "tool_ids": [submit_tool_id, approve_tool_id],
                    "requires_approval": True,
                },
            )
            assert batch_resp.status_code == 200
            assert batch_resp.json()["updated_count"] == 2
            assert all(item["requires_approval"] is True for item in batch_resp.json()["items"])

            # 4. 远端工具变更并独立重发现：校验保留原密钥，存活工具 ID 与中文配置无损保留。
            h.mcp_discovery.tools_by_url[mcp_url] = (
                DiscoveredMcpTool(
                    remote_name="submit_expense",
                    description="提交费用报销申请（新版描述）",
                    input_schema=initial_tools[0].input_schema,
                ),
                DiscoveredMcpTool(
                    remote_name="export_ledger",
                    description="导出总账明细",
                    input_schema={"type": "object", "properties": {"month": {"type": "string"}}},
                ),
            )
            rediscover_resp = super_client.post(f"/api/v1/admin/mcp/servers/{server_id}/rediscover")
            assert rediscover_resp.status_code == 200, rediscover_resp.text
            # 校验重发现下发的解密凭据保留了 API_TOKEN、包含新加 AUDIT_KEY 且已移除 SIGNING_KEY。
            _, last_secrets = h.mcp_discovery.calls[-1]
            assert last_secrets == {
                "API_TOKEN": "tok-original-keep",
                "AUDIT_KEY": "aud-added-secret",
            }

            tools_after = super_client.get(f"/api/v1/admin/mcp/servers/{server_id}/tools").json()[
                "items"
            ]
            by_remote = {item["remote_name"]: item for item in tools_after}
            assert set(by_remote.keys()) == {"submit_expense", "export_ledger"}
            survived = by_remote["submit_expense"]
            assert survived["tool_id"] == submit_tool_id
            assert survived["requires_approval"] is True
            assert survived["display_name"] == "提交费用报销单"
            assert (
                survived["approval_summary_template"]
                == "为 {applicant} 提交 {amount} 元报销单（科目：{category}）"
            )
            assert survived["parameter_labels"] == {
                "applicant": "申请人",
                "amount": "报销金额",
                "category": "费用科目",
            }

            # 5. 触发 Run 调用需审批工具进入 waiting_for_approval，校验结构化中文业务待审批卡片。
            sess_id = str(fin_member.post("/api/v1/sessions", json={}).json()["id"])
            submit_run = fin_member.post(
                f"/api/v1/sessions/{sess_id}/runs",
                json={"request_id": "req-e2e5-approval", "text": "请帮我提交一笔 1280 元差旅报销"},
            )
            assert submit_run.status_code == 201
            run_id = str(submit_run.json()["id"])

            provider = _ScriptedLlmProvider(
                stream_responses=[
                    _llm_response(
                        LlmToolCall(
                            call_id="tc_expense_1",
                            name=str(survived["model_name"]),
                            arguments={
                                "applicant": "财务员工",
                                "amount": 1280,
                                "category": "差旅费",
                                "cost_center": "CC-2026",
                            },
                        )
                    )
                ]
            )
            executor = h.build_executor(provider)

            async def _execute_until_approval() -> None:
                leases = await h.run_repo.claim(
                    "w-e2e5", lease_seconds=60, execution_seconds=120, limit=1
                )
                assert len(leases) == 1
                await executor.execute(leases[0])

            _run(_execute_until_approval())
            assert len(h.mcp_executor.calls) == 0

            run_detail = fin_member.get(f"/api/v1/sessions/{sess_id}/runs/{run_id}")
            assert run_detail.status_code == 200
            detail = run_detail.json()
            assert detail["status"] == "waiting_for_approval"
            assert len(detail["pending_approvals"]) == 1
            card = detail["pending_approvals"][0]
            assert card["display_name"] == "提交费用报销单"
            assert card["approval_summary"] == "为 财务员工 提交 1280 元报销单（科目：差旅费）"
            formatted_args = card["formatted_arguments"]
            # 已映射字段优先展示中文标签，未映射字段 cost_center 优雅回退原字段名。
            labels_by_key = {item["key"]: (item["label"], item["value"]) for item in formatted_args}
            assert labels_by_key["applicant"] == ("申请人", "财务员工")
            assert labels_by_key["amount"] == ("报销金额", "1280")
            assert labels_by_key["category"] == ("费用科目", "差旅费")
            assert labels_by_key["cost_center"] == ("cost_center", "CC-2026")

            # 6. 停用 MCP Server 后工具即刻从用户目录与新 Run 快照下线。
            disable_resp = super_client.put(
                f"/api/v1/admin/mcp/servers/{server_id}/enabled",
                json={"enabled": False},
            )
            assert disable_resp.status_code == 200
            assert disable_resp.json()["enabled"] is False
            assert fin_member.get("/api/v1/tools").json()["items"] == []

            fin_auth_sid = _run(h.latest_auth_session_id("mock_fin_member"))
            fin_auth = _run(
                h.tool_runtime.authorize(
                    "mock_fin_member", sess_id, "run_after_disable", fin_auth_sid
                )
            )
            assert fin_auth is not None
            disabled_snap = _run(h.tool_runtime.build_snapshot("run-v2", fin_auth, depth=0))
            assert [t for t in disabled_snap.tools if isinstance(t.source, McpToolSource)] == []
    finally:
        _run(h.aclose())


# ---------------------------------------------------------------------------
# 闭环 6：全量 Alembic 迁移链验证（离线完整升级 / 第一期回滚 SQL + 索引核验）
# ---------------------------------------------------------------------------


def test_e2e_6_alembic_full_migration_chain_and_phase1_downgrade_sql() -> None:
    """校验唯一基线 head=0001_initial_schema、base->head 离线升级 SQL 与基线回滚 SQL。"""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://athena:athena@127.0.0.1:5432/athena_acceptance",
    )

    script_dir = ScriptDirectory.from_config(config)
    heads = script_dir.get_heads()
    assert heads == ["0001_initial_schema"]
    revision = script_dir.get_revision("0001_initial_schema")
    assert revision is not None
    assert revision.down_revision is None

    upgrade_buffer = StringIO()
    with redirect_stdout(upgrade_buffer):
        command.upgrade(config, "head", sql=True)
    upgrade_sql = upgrade_buffer.getvalue()
    assert len(upgrade_sql.strip()) > 0

    required_tokens = (
        "ix_knowledge_chunks_embedding_hnsw",
        "user_work_profiles",
        "user_memories",
        "departments",
        "ix_departments_path",
        "user_departments",
        "dept_admin_scopes",
        "resource_visibility_grants",
        "ix_resource_visibility_grants_lookup",
        "uq_mcp_servers_name_active",
        "model_snapshot_json",
        "parameter_labels_json",
    )
    for token in required_tokens:
        assert token in upgrade_sql, f"base->head 离线迁移 SQL 缺少关键对象: {token}"

    full_downgrade_buffer = StringIO()
    with redirect_stdout(full_downgrade_buffer):
        command.downgrade(config, "0001_initial_schema:base", sql=True)
    full_downgrade_sql = full_downgrade_buffer.getvalue()
    assert "DROP TABLE user_work_profiles" in full_downgrade_sql
    assert "DROP TABLE departments" in full_downgrade_sql
    assert "DROP TABLE resource_visibility_grants" in full_downgrade_sql
