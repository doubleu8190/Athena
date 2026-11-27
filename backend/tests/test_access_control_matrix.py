"""M1.4 统一三层权限引擎（RBAC + 组织范围）、外键重构与四条业务线全链路隔离矩阵测试。"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from auth_support import FakePasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_unit_harness import create_pg_unit_db
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access import ResourceVisibilityGrantModel, SqlAlchemyAccessControlRepository
from adapter.access.repository import load_user_access_snapshot_in_session
from adapter.auth.model import UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.knowledge.model import (
    KnowledgeBaseModel,
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    KnowledgeVersionModel,
)
from adapter.knowledge.repository import KnowledgeRepository
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.org.model import DepartmentModel, DeptAdminScopeModel, UserDepartmentModel
from adapter.org.repository import SqlAlchemyOrgRepository
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from adapter.skill.repository import SqlAlchemySkillRepository, build_run_skill_snapshot
from adapter.subagent.repository import SqlAlchemySubagentRepository
from api.auth import AuthCookieConfig, build_auth_router
from api.auth_middleware import AuthPrecheckMiddleware
from api.knowledge import build_knowledge_router
from api.mcp import build_mcp_router
from api.project_knowledge import build_project_knowledge_router
from api.projects import build_project_router
from api.security import DEFAULT_COOKIE_NAME
from api.skill import (
    build_skill_admin_router,
    build_skill_personal_router,
    build_skill_user_router,
)
from api.subagent import build_subagent_admin_router
from api.tools import build_tools_router
from app.access import AccessPolicyService
from app.auth import AuthService
from app.knowledge.service import KnowledgeService
from app.project.knowledge import ProjectKnowledgeService
from app.project.service import ProjectService
from app.run.executor import RunExecutor
from app.skill.service import SkillService
from app.subagent.service import SubagentService
from app.tool.manager import ToolManager
from app.tool.runtime import (
    KNOWLEDGE_SEARCH_DEFINITION,
    SKILL_EXECUTE_DEFINITION,
    SKILL_LOAD_DEFINITION,
    SKILL_READ_RESOURCE_DEFINITION,
    RunToolRuntimeService,
)
from domain.access import (
    AccessPermissionDeniedError,
    GrantTargetType,
    InvalidVisibilityConfigError,
    ResourceAccessTarget,
    ResourceType,
    VisibilityGrantInput,
    VisibilityScope,
    evaluate_can_manage,
)
from domain.auth import UserRole, UserStatus
from domain.knowledge.model import DocumentState
from domain.knowledge.ports import OriginalStore
from domain.llm.model import LlmError, LlmResponse, LlmToolCall
from domain.mcp import (
    DiscoveredMcpTool,
    McpCallResult,
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolTarget,
)
from domain.org import DepartmentStatus
from domain.run import Run, RunModelSnapshot, RunStatus, RunUsage
from domain.skill import (
    ActiveSkillGuide,
    RunSkillSnapshot,
    SkillConflictError,
    SkillScope,
)
from domain.tool import (
    McpToolSource,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolOutcome,
    ToolOutcomeStatus,
)
from infrastructure.knowledge.parsers import LocalDocumentParser
from infrastructure.security.secrets import SecretCipher

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
NOW = datetime.now(UTC)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步测试函数中运行协程。"""
    return asyncio.run(coroutine)


class _SimpleTokenizer:
    """按空白切分检索词，供知识库关键词检索使用。"""

    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(part for part in text.strip().split() if part)


class _MemoryOriginalStore(OriginalStore):
    """内存知识库原件存储，支持上传替换与原文下载验证。"""

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
        """空异步生成器实现，满足 OriginalStore 抽象接口约束。"""
        del older_than_seconds
        if False:
            yield ""


class _MemoryFileObjectStore:
    """内存 Skill 包对象存储，实现 FileObjectStore 所需方法。"""

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
        data = self.objects[key]
        return data[:maximum_bytes]

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        data = self.objects[key]
        return data[offset : offset + length]

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

    async def aclose(self) -> None:
        return None


class _FakeMcpDiscovery:
    """按注册名称返回固定远端工具列表，不触发真实网络 I/O。"""

    def __init__(self, tools_by_url: Mapping[str, tuple[DiscoveredMcpTool, ...]]) -> None:
        self._tools_by_url = tools_by_url

    async def discover(
        self,
        connection: McpConnectionInput,
    ) -> tuple[DiscoveredMcpTool, ...]:
        url = getattr(connection.config, "url", "")
        return self._tools_by_url.get(url, ())


class _FakeMcpExecutor:
    """可记录调用次数并支持在执行期间注入钩子（如调岗撤权）的 MCP 执行器。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_execute: Callable[[], Coroutine[Any, Any, None]] | None = None

    async def call(
        self,
        target: McpRuntimeToolTarget,
        arguments: dict[str, Any],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        del deadline_seconds
        self.calls.append((target.remote_name, dict(arguments)))
        if self.on_execute is not None:
            hook = self.on_execute
            self.on_execute = None
            await hook()
        return McpCallResult(
            status=McpCallStatus.OK,
            code="mcp_ok",
            message="ok",
            text=f"result-from-{target.remote_name}",
            structured={"secret_payload": f"result-from-{target.remote_name}"},
        )


class _FakeSkillExecutionService:
    """模拟脚本执行服务，用于验证执行前/交付前实时 Skill 鉴权。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        del context
        self.calls.append(str(arguments.get("skill_id", "")))
        return ToolExecutionResult(
            ToolOutcome(
                status=ToolOutcomeStatus.OK,
                code="skill_script_ok",
                message="executed",
                data={"stdout": "done"},
            )
        )


class _EmptySessionFiles:
    """最小会话文件读取替身，用于打通 skill_execute 审批前实时权限复核链路。"""

    async def get_many(
        self,
        chat_session_id: str,
        owner_id: str,
        file_ids: tuple[str, ...],
    ) -> tuple[Any, ...]:
        del chat_session_id, owner_id, file_ids
        return ()


class _AlwaysValidModelChecker:
    """Subagent 模型检查替身。"""

    async def is_model_available(self, model_profile_id: str) -> bool:
        del model_profile_id
        return True


class _SimpleToolCatalog:
    """从真实 MCP 仓储返回允许配置的全部工具 ID，供超管创建 Subagent 校验。"""

    def __init__(self, mcp_repo: SqlAlchemyMcpRepository) -> None:
        self._mcp_repo = mcp_repo

    async def list_available_tool_groups(self) -> tuple[Any, ...]:
        return ()

    async def list_available_tool_ids(self) -> frozenset[str]:
        page = await self._mcp_repo.list_admin(limit=100, offset=0)
        return frozenset(tool.tool_id for tool in page.items)


def _make_skill_zip(
    *,
    slug: str,
    name: str,
    description: str,
    guide: str,
    resource_content: str = "secret-finance-reference",
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
        f"    purpose: 返回测试结果。\n"
        f"    parameters:\n"
        f"      type: object\n"
        f"      additionalProperties: false\n"
        f"      properties: {{}}\n"
        f"---\n"
        f"{guide}\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("SKILL.md", markdown)
        archive.writestr("references/guide.txt", resource_content)
        archive.writestr("scripts/run.py", 'print("ok")\n')
    return buf.getvalue()


def _import_and_enable_skill(
    client: TestClient,
    *,
    idempotency_key: str,
    scope: str,
    slug: str,
    name: str,
    description: str,
    guide: str,
    owner_department_id: str | None = None,
    visibility_grants: list[dict[str, Any]] | None = None,
    resource_content: str = "secret-finance-reference",
) -> str:
    """导入管理员 Skill 包并立即启用，返回新建 Skill ID。"""
    form_data: dict[str, str] = {"scope": scope}
    if owner_department_id is not None:
        form_data["owner_department_id"] = owner_department_id
    if visibility_grants is not None:
        form_data["visibility_grants"] = json.dumps(visibility_grants)
    imp = client.post(
        "/api/v1/admin/skills/import",
        headers={"Idempotency-Key": idempotency_key},
        data=form_data,
        files={
            "package": (
                f"{slug}.zip",
                _make_skill_zip(
                    slug=slug,
                    name=name,
                    description=description,
                    guide=guide,
                    resource_content=resource_content,
                ),
                "application/zip",
            )
        },
    )
    assert imp.status_code == 201, imp.text
    payload = imp.json()
    skill_id = str(payload["id"])
    version_id = str(payload["versions"][0]["id"])
    cfg = client.patch(
        f"/api/v1/admin/skills/{skill_id}",
        json={
            "expected_revision": payload["revision"],
            "enabled": True,
            "current_version_id": version_id,
            "script_requires_approval": False,
        },
    )
    assert cfg.status_code == 200, cfg.text
    return skill_id


async def _create_pg_db() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """获取隔离 PostgreSQL 单元测试数据库，真实校验全部外键约束与全文检索。"""
    return await create_pg_unit_db()


async def _seed_org_and_users(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """初始化标准多级组织树与 7 个典型角色账号（含归档部门测试节点）。"""
    async with session_factory() as session, session.begin():
        departments = [
            DepartmentModel(
                id="dept_root",
                external_id="ext_root",
                name="总部",
                parent_id=None,
                path="/dept_root/",
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
                status=DepartmentStatus.ACTIVE.value,
                created_at=NOW,
                updated_at=NOW,
            ),
            DepartmentModel(
                id="dept_tax",
                external_id="ext_tax",
                name="税务组",
                parent_id="dept_fin",
                path="/dept_root/dept_fin/dept_tax/",
                status=DepartmentStatus.ACTIVE.value,
                created_at=NOW,
                updated_at=NOW,
            ),
            DepartmentModel(
                id="dept_rd",
                external_id="ext_rd",
                name="研发部",
                parent_id="dept_root",
                path="/dept_root/dept_rd/",
                status=DepartmentStatus.ACTIVE.value,
                created_at=NOW,
                updated_at=NOW,
            ),
            DepartmentModel(
                id="dept_platform",
                external_id="ext_platform",
                name="平台组",
                parent_id="dept_rd",
                path="/dept_root/dept_rd/dept_platform/",
                status=DepartmentStatus.ACTIVE.value,
                created_at=NOW,
                updated_at=NOW,
            ),
            DepartmentModel(
                id="dept_archived",
                external_id="ext_archived",
                name="已撤销历史部门",
                parent_id="dept_fin",
                path="/dept_root/dept_fin/dept_archived/",
                status=DepartmentStatus.ARCHIVED.value,
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
        session.add_all(departments)
        await session.flush()

        users = [
            ("u_super", "super@acme.com", UserRole.SUPER_ADMIN, UserStatus.ACTIVE),
            ("u_fin_admin_a", "fin.admin.a@acme.com", UserRole.DEPT_ADMIN, UserStatus.ACTIVE),
            ("u_fin_admin_b", "fin.admin.b@acme.com", UserRole.DEPT_ADMIN, UserStatus.ACTIVE),
            ("u_fin_member", "fin.member@acme.com", UserRole.MEMBER, UserStatus.ACTIVE),
            ("u_tax_member", "tax.member@acme.com", UserRole.MEMBER, UserStatus.ACTIVE),
            ("u_rd_admin", "rd.admin@acme.com", UserRole.DEPT_ADMIN, UserStatus.ACTIVE),
            ("u_rd_member", "rd.member@acme.com", UserRole.MEMBER, UserStatus.ACTIVE),
            ("u_disabled", "disabled@acme.com", UserRole.SUPER_ADMIN, UserStatus.DISABLED),
        ]
        for uid, email, role, status in users:
            session.add(
                UserModel(
                    id=uid,
                    email=email,
                    password_hash="fake:pass1234",
                    display_name=uid,
                    status=status.value,
                    role=role.value,
                    auth_provider="local",
                    created_at=NOW,
                    updated_at=NOW,
                    deleted_time=None,
                )
            )
        await session.flush()

        memberships = [
            ("u_super", "dept_root"),
            ("u_fin_admin_a", "dept_fin"),
            ("u_fin_admin_b", "dept_fin"),
            ("u_fin_member", "dept_fin"),
            ("u_tax_member", "dept_tax"),
            ("u_rd_admin", "dept_rd"),
            ("u_rd_member", "dept_platform"),
        ]
        for uid, dept_id in memberships:
            session.add(
                UserDepartmentModel(
                    user_id=uid,
                    department_id=dept_id,
                    is_primary=True,
                )
            )

        scopes = [
            ("u_fin_admin_a", "dept_fin", True),
            ("u_fin_admin_b", "dept_fin", True),
            ("u_rd_admin", "dept_rd", True),
        ]
        for uid, dept_id, include_sub in scopes:
            session.add(
                DeptAdminScopeModel(
                    user_id=uid,
                    department_id=dept_id,
                    include_subdepartments=include_sub,
                    created_at=NOW,
                )
            )


class _Harness:
    """整合全部真实仓储、服务、中间件和 HTTP 路由的端到端测试台。"""

    def __init__(
        self,
        engine: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.access_repo = SqlAlchemyAccessControlRepository(session_factory)
        self.access_policy = AccessPolicyService(self.access_repo, clock=lambda: NOW)
        self.auth_repo = SqlAlchemyAuthRepository(session_factory)
        self.org_repo = SqlAlchemyOrgRepository(session_factory)
        self.auth_service = AuthService(
            self.auth_repo,
            FakePasswordHasher(),
            org_reader=self.org_repo,
            clock=lambda: NOW,
        )
        self.originals = _MemoryOriginalStore()
        self.tokenizer = _SimpleTokenizer()
        self.kb_repo = KnowledgeRepository(session_factory)
        self.knowledge_service = KnowledgeService(
            self.kb_repo,
            self.tokenizer,
            profile_id="bge-m3-default",
            originals=self.originals,
            access_policy=self.access_policy,
            clock=lambda: NOW,
        )
        self.project_repo = SqlAlchemyProjectRepository(session_factory)
        self.project_service = ProjectService(self.project_repo, clock=lambda: NOW)
        self.project_knowledge_service = ProjectKnowledgeService(
            self.project_service,
            self.kb_repo,
            access_policy=self.access_policy,
            clock=lambda: NOW,
        )
        self.session_repo = SqlAlchemySessionRepository(session_factory)
        self.file_store = _MemoryFileObjectStore()
        self.skill_repo = SqlAlchemySkillRepository(session_factory)
        self.skill_service = SkillService(
            self.skill_repo,
            cast(Any, self.file_store),
            self.auth_repo,
            access_policy=self.access_policy,
            clock=lambda: NOW,
        )
        self.mcp_repo = SqlAlchemyMcpRepository(
            session_factory,
            SecretCipher(b"k" * 32),
            clock=lambda: NOW,
        )
        self.mcp_discovery = _FakeMcpDiscovery(
            {
                "https://mcp.company.example/mcp": (
                    DiscoveredMcpTool(
                        remote_name="co_tool",
                        description="公司级公共工具",
                        input_schema={"type": "object", "properties": {}},
                    ),
                ),
                "https://mcp.fin.example/mcp": (
                    DiscoveredMcpTool(
                        remote_name="fin_tool",
                        description="财务部专用工具",
                        input_schema={"type": "object", "properties": {}},
                    ),
                ),
                "https://mcp.fin-strict.example/mcp": (
                    DiscoveredMcpTool(
                        remote_name="fin_strict_tool",
                        description="财务部直属工具（不含子部门）",
                        input_schema={"type": "object", "properties": {}},
                    ),
                ),
            }
        )
        self.tool_manager = ToolManager(
            self.mcp_repo,
            self.mcp_discovery,
            self.auth_repo,
            access_policy=self.access_policy,
            clock=lambda: NOW,
        )
        self.subagent_repo = SqlAlchemySubagentRepository(session_factory)
        self.subagent_service = SubagentService(
            self.subagent_repo,
            self.auth_repo,
            access_policy=self.access_policy,
            model_checker=_AlwaysValidModelChecker(),  # type: ignore[arg-type]
            tool_catalog=_SimpleToolCatalog(self.mcp_repo),  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        self.mcp_executor = _FakeMcpExecutor()
        self.skill_execution = _FakeSkillExecutionService()
        self.tool_runtime = RunToolRuntimeService(
            auth=self.auth_repo,
            sessions=self.session_repo,
            mcp_catalog=self.mcp_repo,
            mcp_executor=cast(Any, self.mcp_executor),
            knowledge=self.knowledge_service,
            project_knowledge=self.project_knowledge_service,
            skills=self.skill_service,
            session_files=cast(Any, _EmptySessionFiles()),
            skill_execution=cast(Any, self.skill_execution),
            subagents=self.subagent_repo,
            access_policy=self.access_policy,
            clock=lambda: NOW,
        )
        self.app = self._build_app()

    def _build_app(self) -> FastAPI:
        app = FastAPI()
        app.add_middleware(AuthPrecheckMiddleware, fastapi_app=app)
        app.state.authenticator = self.auth_service
        app.state.auth_cookie_name = DEFAULT_COOKIE_NAME
        app.state.public_origin = None
        app.include_router(
            build_auth_router(
                self.auth_service,
                cookie=AuthCookieConfig(name=DEFAULT_COOKIE_NAME, secure=False),
            )
        )
        app.include_router(build_project_router(self.project_service))
        app.include_router(build_knowledge_router(self.knowledge_service, LocalDocumentParser()))
        app.include_router(
            build_project_knowledge_router(self.project_knowledge_service, self.project_service)
        )
        app.include_router(build_skill_admin_router(self.skill_service))
        app.include_router(build_skill_user_router(self.skill_service))
        app.include_router(build_skill_personal_router(self.skill_service))
        app.include_router(build_mcp_router(self.tool_manager))
        app.include_router(build_tools_router(self.tool_manager))
        app.include_router(build_subagent_admin_router(self.subagent_service))
        return app

    def login_client(self, email: str) -> TestClient:
        """以真实密码登录并返回持有 HttpOnly Cookie 的独立 TestClient。"""
        client = TestClient(self.app)
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "pass1234"},
        )
        assert resp.status_code == 200, resp.text
        return client

    async def mark_document_ready_with_chunk(
        self,
        *,
        document_id: str,
        content: str,
    ) -> str:
        """将文档最新版本直接置为 ready 并写入真实 PostgreSQL tsvector 分片以测试端到端检索。"""
        tokens = self.tokenizer.tokenize(content)
        tsvector_input = " ".join(tokens) if tokens else content
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
            version.completed_at = NOW
            doc.active_version_id = version.id
            doc.pending_version_id = None
            chunk_id = f"chk_{document_id}"
            session.add(
                KnowledgeChunkModel(
                    id=chunk_id,
                    document_id=document_id,
                    version_id=version.id,
                    ordinal=0,
                    content=content,
                    token_count=len(content.split()),
                    locator={"kind": "text"},
                    search_document=cast(str, func.to_tsvector("simple", tsvector_input)),
                    embedding=None,
                )
            )
            return version.id

    async def create_chat_session(
        self,
        *,
        session_id: str,
        owner_id: str,
        project_id: str | None,
    ) -> None:
        """写入聊天会话记录，供 RunToolRuntimeService.authorize 鉴权使用。"""
        async with self.session_factory() as session, session.begin():
            session.add(
                SessionModel(
                    id=session_id,
                    owner_id=owner_id,
                    project_id=project_id,
                    title="测试对话",
                    created_at=NOW,
                    updated_at=NOW,
                    deleted_time=None,
                )
            )

    async def auth_session_id_for_user(self, user_id: str) -> str:
        """读取测试用户的有效 AuthSession ID。"""
        from adapter.auth.model import AuthSessionModel

        async with self.session_factory() as session:
            row = await session.scalar(
                select(AuthSessionModel)
                .where(
                    AuthSessionModel.user_id == user_id,
                    AuthSessionModel.revoked_time.is_(None),
                )
                .order_by(AuthSessionModel.created_at.desc())
            )
            assert row is not None
            return row.id

    async def transfer_user_department(self, user_id: str, new_department_id: str) -> None:
        """模拟员工实时调岗：直接替换 user_departments（保留原登录 Session）。"""
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

    async def aclose(self) -> None:
        await self.engine.dispose()


async def _make_harness(db_path: Path | None = None) -> _Harness:
    del db_path
    engine, session_factory = await _create_pg_db()
    await _seed_org_and_users(session_factory)
    return _Harness(engine, session_factory)


def _make_run(
    *,
    run_id: str,
    session_id: str,
    owner_id: str,
    auth_session_id: str,
    tool_snapshot: Any,
    skill_snapshot: RunSkillSnapshot,
) -> Run:
    """构造最小合法主 Run 领域实体，供运行期权限与 Subagent 调度复核测试使用。"""
    return Run(
        id=run_id,
        session_id=session_id,
        owner_id=owner_id,
        status=RunStatus.RUNNING,
        trigger_message_id="msg_1",
        context_cutoff_sequence=0,
        retry_of_run_id=None,
        model_config_version_id="model_1",
        model_snapshot=RunModelSnapshot(
            id="model_1",
            name="测试模型",
            protocol="chat_completions",
            base_url="https://api.example.com/v1",
            model="gpt-test",
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
        main_prompt_version_id="prompt_main",
        compression_prompt_version_id="prompt_comp",
        prompt_template_version="run-v2",
        event_sequence=0,
        execution_epoch=1,
        worker_id="worker_1",
        lease_expires_at=NOW + timedelta(minutes=1),
        execution_started_at=NOW,
        execution_deadline_at=NOW + timedelta(minutes=5),
        queued_at=NOW,
        first_started_at=NOW,
        finished_at=None,
        created_at=NOW,
        updated_at=NOW,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
        tool_snapshot=tool_snapshot,
        auth_session_id=auth_session_id,
        skill_snapshot=skill_snapshot,
    )


def _make_llm_response() -> LlmResponse:
    """构造最小 LLM 响应对象。"""
    return LlmResponse(
        output=(),
        status="completed",
        incomplete_reason=None,
        model="gpt-test",
        response_id="resp_1",
        request_id="req_1",
        usage=None,
    )


# ---------------------------------------------------------------------------
# 1. 统一权限引擎核心规则与边界测试
# ---------------------------------------------------------------------------


def test_access_policy_engine_rules_and_edge_cases(tmp_path: Path) -> None:
    """验证停用用户、个人隐私保护、归档部门屏蔽、部门管理员子树展开与角色授权规则。"""

    async def _scenario() -> None:
        h = await _make_harness(tmp_path / "policy.db")
        try:
            policy = h.access_policy
            company_kb = ResourceAccessTarget(
                resource_type=ResourceType.KNOWLEDGE_BASE,
                resource_id="kb_co",
                visibility_scope=VisibilityScope.COMPANY,
                owner_id="u_super",
                owner_department_id=None,
            )
            personal_kb = ResourceAccessTarget(
                resource_type=ResourceType.KNOWLEDGE_BASE,
                resource_id="kb_priv",
                visibility_scope=VisibilityScope.PERSONAL,
                owner_id="u_fin_member",
                owner_department_id=None,
            )
            dept_kb = ResourceAccessTarget(
                resource_type=ResourceType.KNOWLEDGE_BASE,
                resource_id="kb_fin_role",
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_id="u_super",
                owner_department_id="dept_fin",
            )

            # 1) 停用账号与不存在账号连 company 资源也不可读、不可创建。
            assert await policy.can_read_target("u_disabled", company_kb) is False
            assert await policy.can_read_target("u_ghost", company_kb) is False
            assert (
                await policy.can_create_resource(
                    "u_disabled",
                    ResourceType.KNOWLEDGE_BASE,
                    VisibilityScope.PERSONAL,
                )
                is False
            )

            # 2) personal 资源强隐私保护：即使 super_admin 也不可读取或管理他人私有资产。
            assert await policy.can_read_target("u_fin_member", personal_kb) is True
            assert await policy.can_manage_resource("u_fin_member", personal_kb) is True
            assert await policy.can_read_target("u_super", personal_kb) is False
            assert await policy.can_manage_resource("u_super", personal_kb) is False

            # 3) dept_admin 管辖子树展开：include_subdepartments=True 含税务组且排除已归档子部门。
            fin_admin_snap = await policy.get_user_snapshot("u_fin_admin_a")
            assert fin_admin_snap is not None
            assert fin_admin_snap.managed_department_ids == frozenset({"dept_fin", "dept_tax"})
            assert "dept_archived" not in fin_admin_snap.managed_department_ids

            # 将 u_fin_admin_b 的管辖范围改为 include_subdepartments=False，仅管辖财务部本级。
            async with h.session_factory() as session, session.begin():
                scope_row = await session.scalar(
                    select(DeptAdminScopeModel).where(
                        DeptAdminScopeModel.user_id == "u_fin_admin_b",
                        DeptAdminScopeModel.department_id == "dept_fin",
                    )
                )
                assert scope_row is not None
                scope_row.include_subdepartments = False
            fin_admin_b_snap = await policy.get_user_snapshot("u_fin_admin_b")
            assert fin_admin_b_snap is not None
            assert fin_admin_b_snap.managed_department_ids == frozenset({"dept_fin"})

            # 4) 默认授权落库与角色授权匹配。
            (
                resolved_scope,
                resolved_dept,
                default_grants,
            ) = await policy.resolve_and_validate_visibility(
                user_id="u_fin_admin_a",
                resource_type=ResourceType.KNOWLEDGE_BASE,
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_department_id="dept_fin",
                visibility_grants=None,
                for_create=True,
            )
            assert resolved_scope is VisibilityScope.DEPARTMENT
            assert resolved_dept == "dept_fin"
            assert default_grants == (
                VisibilityGrantInput(
                    target_type=GrantTargetType.DEPARTMENT,
                    target_id="dept_fin",
                    include_subdepartments=True,
                ),
            )

            # 超管为 dept_kb 配置 role='dept_admin' 授权：研发部管理员可读，研发部普通成员不可读。
            await policy.replace_resource_grants(
                ResourceType.KNOWLEDGE_BASE,
                dept_kb.resource_id,
                (
                    VisibilityGrantInput(
                        target_type=GrantTargetType.ROLE,
                        target_id="dept_admin",
                        include_subdepartments=False,
                    ),
                ),
            )
            assert await policy.can_read_target("u_rd_admin", dept_kb) is True
            assert await policy.can_read_target("u_rd_member", dept_kb) is False

            # 5) 校验非法授权配置：归档部门、不存在部门、非法角色、部门管理员越权授权均被拒绝。
            with pytest.raises(InvalidVisibilityConfigError):
                await policy.resolve_and_validate_visibility(
                    user_id="u_super",
                    resource_type=ResourceType.KNOWLEDGE_BASE,
                    visibility_scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_archived",
                    visibility_grants=None,
                    for_create=True,
                )
            with pytest.raises(InvalidVisibilityConfigError):
                await policy.resolve_and_validate_visibility(
                    user_id="u_super",
                    resource_type=ResourceType.KNOWLEDGE_BASE,
                    visibility_scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_fin",
                    visibility_grants=(
                        VisibilityGrantInput(GrantTargetType.ROLE, "invalid_role", False),
                    ),
                    for_create=True,
                )
            with pytest.raises(AccessPermissionDeniedError):
                await policy.resolve_and_validate_visibility(
                    user_id="u_fin_admin_a",
                    resource_type=ResourceType.KNOWLEDGE_BASE,
                    visibility_scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_fin",
                    visibility_grants=(
                        VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_rd", True),
                    ),
                    for_create=True,
                )
            with pytest.raises(AccessPermissionDeniedError):
                await policy.resolve_and_validate_visibility(
                    user_id="u_fin_admin_a",
                    resource_type=ResourceType.KNOWLEDGE_BASE,
                    visibility_scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_fin",
                    visibility_grants=(
                        VisibilityGrantInput(GrantTargetType.ROLE, "member", False),
                    ),
                    for_create=True,
                )

            # 6) 上级部门 dept_fin 归档后，税务组成员的 ancestor_department_ids 不含 dept_fin。
            async with h.session_factory() as session, session.begin():
                fin_dept = await session.get(DepartmentModel, "dept_fin")
                assert fin_dept is not None
                fin_dept.status = DepartmentStatus.ARCHIVED.value
            tax_snap = await policy.get_user_snapshot("u_tax_member")
            assert tax_snap is not None
            assert "dept_tax" in tax_snap.ancestor_department_ids
            assert "dept_root" in tax_snap.ancestor_department_ids
            assert "dept_fin" not in tax_snap.ancestor_department_ids
        finally:
            await h.aclose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 2. 跨部门隔离矩阵（四条业务线 + 子部门穿透 + Subagent × User 工具严格交集）
# ---------------------------------------------------------------------------


def test_cross_department_isolation_across_all_four_resources(tmp_path: Path) -> None:
    """验证知识库、Skill、MCP 工具、Subagent 跨部门隔离、子部门开关及子智能体工具交集。"""

    async def _scenario() -> None:
        h = await _make_harness(tmp_path / "isolation.db")
        try:
            super_c = h.login_client("super@acme.com")
            fin_admin_c = h.login_client("fin.admin.a@acme.com")
            fin_member_c = h.login_client("fin.member@acme.com")
            tax_member_c = h.login_client("tax.member@acme.com")
            rd_admin_c = h.login_client("rd.admin@acme.com")
            rd_member_c = h.login_client("rd.member@acme.com")

            # --- 构造四类三层资源 ---
            # A. 知识库：公司级 / 财务部(含子部门) / 财务部(不含子部门) / 研发部 / 财务成员个人
            co_kb_id = super_c.post(
                "/api/v1/knowledge-bases",
                json={"name": "公司制度库", "visibility_scope": "company"},
            ).json()["id"]
            fin_kb_id = fin_admin_c.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务共享库",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            ).json()["id"]
            fin_strict_kb_id = fin_admin_c.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务直属保密库",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                    "visibility_grants": [
                        {
                            "target_type": "department",
                            "target_id": "dept_fin",
                            "include_subdepartments": False,
                        }
                    ],
                },
            ).json()["id"]
            rd_kb_id = rd_admin_c.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "研发架构库",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_rd",
                },
            ).json()["id"]
            priv_kb_id = fin_member_c.post(
                "/api/v1/knowledge-bases",
                json={"name": "财务个人笔记"},
            ).json()["id"]

            # 往财务共享库写入一篇已就绪文档，供跨部门直连探测（列表/检索/下载原文）验证。
            fin_doc_resp = fin_admin_c.post(
                f"/api/v1/knowledge-bases/{fin_kb_id}/documents/text",
                headers={"Idempotency-Key": "fin-doc-1"},
                json={"title": "Q4预算明细", "text": "核心预算数字 BUDGET-999"},
            )
            assert fin_doc_resp.status_code == 202, fin_doc_resp.text
            fin_doc_id = fin_doc_resp.json()["document_id"]
            fin_ver_id = await h.mark_document_ready_with_chunk(
                document_id=fin_doc_id,
                content="核心预算数字 BUDGET-999",
            )

            # B. Skill：公司级 / 财务部(含子部门) / 财务部(不含子部门) / 研发部 / 个人
            co_skill_id = _import_and_enable_skill(
                super_c,
                idempotency_key="imp-co-skill",
                scope="company",
                slug="company-helper",
                name="公司助手",
                description="全员可用",
                guide="公司通用指南",
            )
            fin_skill_id = _import_and_enable_skill(
                fin_admin_c,
                idempotency_key="imp-fin-skill",
                scope="department",
                owner_department_id="dept_fin",
                slug="finance-audit",
                name="财务审计",
                description="财务部与税务组可用",
                guide="财务内部审计口径",
            )
            fin_strict_skill_id = _import_and_enable_skill(
                fin_admin_c,
                idempotency_key="imp-fin-strict-skill",
                scope="department",
                owner_department_id="dept_fin",
                visibility_grants=[
                    {
                        "target_type": "department",
                        "target_id": "dept_fin",
                        "include_subdepartments": False,
                    }
                ],
                slug="finance-strict",
                name="财务直属口径",
                description="仅财务部直属可用",
                guide="仅直属可见",
            )
            rd_skill_id = _import_and_enable_skill(
                rd_admin_c,
                idempotency_key="imp-rd-skill",
                scope="department",
                owner_department_id="dept_rd",
                slug="rd-review",
                name="代码评审",
                description="研发部可用",
                guide="研发规范",
            )
            priv_skill_id = fin_member_c.post(
                "/api/v1/me/skills",
                headers={"Idempotency-Key": "me-fin-skill"},
                json={"name": "我的私有技能", "description": "私有", "guide": "私人模板"},
            ).json()["id"]

            # C. MCP Servers：公司级 / 财务部(含子部门) / 财务部(不含子部门)
            co_mcp = super_c.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "co-mcp",
                    "transport": "streamable_http",
                    "url": "https://mcp.company.example/mcp",
                    "secrets": {},
                    "visibility_scope": "company",
                },
            )
            assert co_mcp.status_code == 201, co_mcp.text
            fin_mcp = super_c.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "fin-mcp",
                    "transport": "streamable_http",
                    "url": "https://mcp.fin.example/mcp",
                    "secrets": {},
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            assert fin_mcp.status_code == 201, fin_mcp.text
            fin_strict_mcp = super_c.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "fin-strict-mcp",
                    "transport": "streamable_http",
                    "url": "https://mcp.fin-strict.example/mcp",
                    "secrets": {},
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                    "visibility_grants": [
                        {
                            "target_type": "department",
                            "target_id": "dept_fin",
                            "include_subdepartments": False,
                        }
                    ],
                },
            )
            assert fin_strict_mcp.status_code == 201, fin_strict_mcp.text

            all_tools = {
                item["description"]: item["tool_id"]
                for item in super_c.get("/api/v1/tools").json()["items"]
            }
            co_tool_id = all_tools["公司级公共工具"]
            fin_tool_id = all_tools["财务部专用工具"]
            fin_strict_tool_id = all_tools["财务部直属工具（不含子部门）"]

            # D. Subagents：公司级（绑定 co_tool + fin_tool 验证交集）/ 财务部 / 财务直属
            co_agent_resp = super_c.post(
                "/api/v1/admin/subagents",
                json={
                    "slug": "co_agent",
                    "name": "公司综合分析师",
                    "description": "面向全员的子智能体",
                    "system_prompt": "你是公司综合分析师。",
                    "allowed_tool_ids": [co_tool_id, fin_tool_id],
                    "visibility_scope": "company",
                },
            )
            assert co_agent_resp.status_code == 201, co_agent_resp.text
            fin_agent_resp = super_c.post(
                "/api/v1/admin/subagents",
                json={
                    "slug": "fin_agent",
                    "name": "财务专家",
                    "description": "仅财务线可见",
                    "system_prompt": "你是财务专家。",
                    "allowed_tool_ids": [fin_tool_id],
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            assert fin_agent_resp.status_code == 201, fin_agent_resp.text
            fin_strict_agent_resp = super_c.post(
                "/api/v1/admin/subagents",
                json={
                    "slug": "fin_strict_agent",
                    "name": "财务直属专员",
                    "description": "不含税务子组",
                    "system_prompt": "你是财务直属专员。",
                    "allowed_tool_ids": [fin_strict_tool_id],
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                    "visibility_grants": [
                        {
                            "target_type": "department",
                            "target_id": "dept_fin",
                            "include_subdepartments": False,
                        }
                    ],
                },
            )
            assert fin_strict_agent_resp.status_code == 201, fin_strict_agent_resp.text

            # --- 验证 1：财务直属员工可见性 ---
            fin_member_kbs = {
                item["id"] for item in fin_member_c.get("/api/v1/knowledge-bases").json()["items"]
            }
            assert fin_member_kbs == {co_kb_id, fin_kb_id, fin_strict_kb_id, priv_kb_id}
            fin_member_skills = {
                item["id"] for item in fin_member_c.get("/api/v1/skills").json()["items"]
            }
            assert fin_member_skills == {
                co_skill_id,
                fin_skill_id,
                fin_strict_skill_id,
                priv_skill_id,
            }
            fin_member_tools = {
                item["tool_id"] for item in fin_member_c.get("/api/v1/tools").json()["items"]
            }
            assert fin_member_tools == {co_tool_id, fin_tool_id, fin_strict_tool_id}

            # --- 验证 2：税务子组成员（include_subdepartments=True 穿透 / False 阻断） ---
            tax_kbs = {
                item["id"] for item in tax_member_c.get("/api/v1/knowledge-bases").json()["items"]
            }
            assert tax_kbs == {co_kb_id, fin_kb_id}
            assert (
                tax_member_c.get(f"/api/v1/knowledge-bases/{fin_strict_kb_id}").status_code == 404
            )
            assert tax_member_c.get(f"/api/v1/knowledge-bases/{priv_kb_id}").status_code == 404

            tax_skills = {item["id"] for item in tax_member_c.get("/api/v1/skills").json()["items"]}
            assert tax_skills == {co_skill_id, fin_skill_id}

            tax_tools = {
                item["tool_id"] for item in tax_member_c.get("/api/v1/tools").json()["items"]
            }
            assert tax_tools == {co_tool_id, fin_tool_id}
            assert tax_member_c.get(f"/api/v1/tools/{fin_strict_tool_id}").status_code == 404

            # --- 验证 3：研发成员彻底隔离财务线四类资产（无列表泄露 + 直连全 404 + 运行时拒绝） ---
            rd_kbs = {
                item["id"] for item in rd_member_c.get("/api/v1/knowledge-bases").json()["items"]
            }
            assert rd_kbs == {co_kb_id, rd_kb_id}
            assert rd_member_c.get(f"/api/v1/knowledge-bases/{fin_kb_id}").status_code == 404
            assert (
                rd_member_c.get(f"/api/v1/knowledge-bases/{fin_kb_id}/documents").status_code == 404
            )
            assert (
                rd_member_c.post(
                    f"/api/v1/knowledge-bases/{fin_kb_id}/search",
                    json={"query": "BUDGET-999"},
                ).status_code
                == 404
            )
            assert (
                rd_member_c.get(
                    f"/api/v1/knowledge-bases/{fin_kb_id}/documents/{fin_doc_id}/versions/{fin_ver_id}/source"
                ).status_code
                == 404
            )
            rd_proj_id = rd_member_c.post("/api/v1/projects", json={"name": "研发个人项目"}).json()[
                "id"
            ]
            assert (
                rd_member_c.put(
                    f"/api/v1/projects/{rd_proj_id}/knowledge-bases/{fin_kb_id}"
                ).status_code
                == 404
            )

            rd_skills = {item["id"] for item in rd_member_c.get("/api/v1/skills").json()["items"]}
            assert rd_skills == {co_skill_id, rd_skill_id}
            with pytest.raises(SkillConflictError, match="skill_selection_unavailable"):
                async with h.session_factory() as session:
                    rd_snapshot = await load_user_access_snapshot_in_session(session, "u_rd_member")
                    assert rd_snapshot is not None
                    await build_run_skill_snapshot(session, (fin_skill_id,), snapshot=rd_snapshot)

            rd_tools = {
                item["tool_id"] for item in rd_member_c.get("/api/v1/tools").json()["items"]
            }
            assert rd_tools == {co_tool_id}
            assert rd_member_c.get(f"/api/v1/tools/{fin_tool_id}").status_code == 404

            # --- 验证 4：Run 工具快照、Subagent 目录可见性与严格交集（Subagent × User） ---
            await h.create_chat_session(
                session_id="sess_rd", owner_id="u_rd_member", project_id=rd_proj_id
            )
            await h.create_chat_session(
                session_id="sess_fin", owner_id="u_fin_member", project_id=None
            )
            await h.create_chat_session(
                session_id="sess_tax", owner_id="u_tax_member", project_id=None
            )

            rd_auth_sid = await h.auth_session_id_for_user("u_rd_member")
            fin_auth_sid = await h.auth_session_id_for_user("u_fin_member")
            tax_auth_sid = await h.auth_session_id_for_user("u_tax_member")

            rd_auth = await h.tool_runtime.authorize(
                "u_rd_member", "sess_rd", "run_rd", rd_auth_sid
            )
            fin_auth = await h.tool_runtime.authorize(
                "u_fin_member", "sess_fin", "run_fin", fin_auth_sid
            )
            tax_auth = await h.tool_runtime.authorize(
                "u_tax_member", "sess_tax", "run_tax", tax_auth_sid
            )
            assert rd_auth is not None and fin_auth is not None and tax_auth is not None

            rd_snap = await h.tool_runtime.build_snapshot("run-v2", rd_auth, depth=0)
            fin_snap = await h.tool_runtime.build_snapshot("run-v2", fin_auth, depth=0)
            tax_snap = await h.tool_runtime.build_snapshot("run-v2", tax_auth, depth=0)

            assert {t.tool_id for t in rd_snap.tools if isinstance(t.source, McpToolSource)} == {
                co_tool_id
            }
            assert {t.tool_id for t in fin_snap.tools if isinstance(t.source, McpToolSource)} == {
                co_tool_id,
                fin_tool_id,
                fin_strict_tool_id,
            }
            assert {t.tool_id for t in tax_snap.tools if isinstance(t.source, McpToolSource)} == {
                co_tool_id,
                fin_tool_id,
            }

            # dispatch_subagents 描述仅包含用户可读的子智能体。
            rd_dispatch = next(t for t in rd_snap.tools if t.model_name == "dispatch_subagents")
            fin_dispatch = next(t for t in fin_snap.tools if t.model_name == "dispatch_subagents")
            tax_dispatch = next(t for t in tax_snap.tools if t.model_name == "dispatch_subagents")
            assert "co_agent" in rd_dispatch.description
            assert "fin_agent" not in rd_dispatch.description
            assert "fin_strict_agent" not in rd_dispatch.description
            assert "fin_agent" in fin_dispatch.description
            assert "fin_strict_agent" in fin_dispatch.description
            assert "fin_agent" in tax_dispatch.description
            assert "fin_strict_agent" not in tax_dispatch.description

            # 子 Run 工具快照严格交集：co_agent 配置了 [co_tool, fin_tool]
            rd_child_snap = await h.tool_runtime.build_snapshot(
                "run-v2",
                rd_auth,
                depth=1,
                allowed_tool_ids=(co_tool_id, fin_tool_id),
            )
            fin_child_snap = await h.tool_runtime.build_snapshot(
                "run-v2",
                fin_auth,
                depth=1,
                allowed_tool_ids=(co_tool_id, fin_tool_id),
            )
            assert {
                t.tool_id for t in rd_child_snap.tools if isinstance(t.source, McpToolSource)
            } == {co_tool_id}
            assert {
                t.tool_id for t in fin_child_snap.tools if isinstance(t.source, McpToolSource)
            } == {
                co_tool_id,
                fin_tool_id,
            }

            # --- 验证 5：研发成员强行伪造 fin_agent slug 调度与直接调用 fin_tool 均被运行时拦截 ---
            executor = RunExecutor(
                repository=cast(Any, object()),
                profiles=cast(Any, object()),
                provider=cast(Any, object()),
                system_prompts=cast(Any, object()),
                tool_runtime=h.tool_runtime,
                subagents=h.subagent_repo,
                access_policy=h.access_policy,
            )
            rd_run = _make_run(
                run_id="run_rd_1",
                session_id="sess_rd",
                owner_id="u_rd_member",
                auth_session_id=rd_auth_sid,
                tool_snapshot=rd_snap,
                skill_snapshot=RunSkillSnapshot(),
            )
            rd_state = cast(
                Any,
                SimpleNamespace(run=rd_run, authorized=rd_auth, snapshot=rd_snap, round=0),
            )
            with pytest.raises(LlmError, match="subagent_profile_unavailable"):
                await executor._dispatch_subagents(
                    rd_state,
                    LlmToolCall(
                        call_id="tc_1",
                        name="dispatch_subagents",
                        arguments={
                            "tasks": [{"subagent_slug": "fin_agent", "task": "窃取财务报表"}]
                        },
                    ),
                    "",
                    _make_llm_response(),
                )

            fin_tool_def = next(t for t in fin_snap.tools if t.tool_id == fin_tool_id)
            rd_exec_ctx = ToolExecutionContext(
                owner_id="u_rd_member",
                chat_session_id="sess_rd",
                project_id=rd_proj_id,
                run_id="run_rd_1",
                execution_epoch=1,
                auth_session_id=rd_auth_sid,
                deadline_at=NOW + timedelta(seconds=30),
                tool_call_record_id="tc_mcp_1",
                skill_snapshot=RunSkillSnapshot(),
            )
            prep = await h.tool_runtime.prepare_call(
                fin_tool_def,
                fin_tool_def.model_name,
                {},
                rd_run,
                rd_auth,
                call_id="tc_mcp_1",
            )
            assert prep.outcome is not None
            assert prep.outcome.code == "tool_permission_denied"
            exec_res = await h.tool_runtime.execute(
                fin_tool_def,
                fin_tool_def.model_name,
                {},
                rd_exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert exec_res.outcome.status is ToolOutcomeStatus.ERROR
            assert exec_res.outcome.code == "tool_permission_denied"
            assert h.mcp_executor.calls == []
        finally:
            await h.aclose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 3. 部门管理员分权协作、外键重构验证与防越权提权
# ---------------------------------------------------------------------------


def test_dept_admin_delegation_fk_collaboration_and_anti_escalation(tmp_path: Path) -> None:
    """验证多部门管理员协同维护文档与技能（证明复合外键已拆除）、个人项目跨主绑定及防越权边界。"""

    async def _scenario() -> None:
        h = await _make_harness(tmp_path / "delegation.db")
        try:
            super_c = h.login_client("super@acme.com")
            fin_admin_a = h.login_client("fin.admin.a@acme.com")
            fin_admin_b = h.login_client("fin.admin.b@acme.com")
            fin_member = h.login_client("fin.member@acme.com")
            rd_admin = h.login_client("rd.admin@acme.com")

            # --- 1. 多部门管理员协同管理同一财务部知识库（PostgreSQL 外键约束实测） ---
            kb_resp = fin_admin_a.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务协作知识库",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            assert kb_resp.status_code == 201, kb_resp.text
            fin_kb_id = kb_resp.json()["id"]
            grants_json = kb_resp.json()["visibility_grants"]
            assert len(grants_json) == 1
            assert grants_json[0]["target_type"] == "department"
            assert grants_json[0]["target_id"] == "dept_fin"
            assert grants_json[0]["include_subdepartments"] is True

            # 管理员 B 在管理员 A 的库内新建文档 -> A 替换版本 -> B 改名与删除，
            # 全程在真实 PostgreSQL 外键约束下实测单列外键已解除单人私有复合外键限制。
            upload_b = fin_admin_b.post(
                f"/api/v1/knowledge-bases/{fin_kb_id}/documents/text",
                headers={"Idempotency-Key": "upload-by-admin-b"},
                json={"title": "旧版报销规范", "text": "v1 by admin B"},
            )
            assert upload_b.status_code == 202, upload_b.text
            doc_id = upload_b.json()["document_id"]
            await h.mark_document_ready_with_chunk(document_id=doc_id, content="v1 by admin B")

            # 管理员 A 读取并替换管理员 B 上传的文档（跨 owner_id 写 knowledge_document_versions）
            list_a = fin_admin_a.get(f"/api/v1/knowledge-bases/{fin_kb_id}/documents")
            assert list_a.status_code == 200
            assert [item["id"] for item in list_a.json()["items"]] == [doc_id]

            replace_a = fin_admin_a.post(
                f"/api/v1/knowledge-bases/{fin_kb_id}/documents/{doc_id}/versions/text",
                headers={"Idempotency-Key": "replace-by-admin-a"},
                json={"title": "旧版报销规范", "text": "v2 by admin A"},
            )
            assert replace_a.status_code == 202, replace_a.text
            v2_id = replace_a.json()["version_id"]

            # 验证 knowledge_document_versions.source_id 单列外键允许跨 owner_id 引用文件原件。
            from adapter.knowledge.model import KnowledgeSourceModel

            await h.originals.put("kb/cross-owner/policy.txt", b"file-bytes-by-admin-b")
            async with h.session_factory() as session, session.begin():
                session.add(
                    KnowledgeSourceModel(
                        id="ksrc_cross_owner",
                        owner_id="u_fin_admin_b",
                        filename="policy.txt",
                        media_type="text/plain",
                        size_bytes=21,
                        sha256="a" * 64,
                        storage_key="kb/cross-owner/policy.txt",
                        created_at=NOW,
                    )
                )
                await session.flush()
                v2_row = await session.get(KnowledgeVersionModel, v2_id)
                assert v2_row is not None
                v2_row.source_text = None
                v2_row.source_id = "ksrc_cross_owner"

            rename_b = fin_admin_b.patch(
                f"/api/v1/knowledge-bases/{fin_kb_id}/documents/{doc_id}",
                json={"title": "新版报销规范"},
            )
            assert rename_b.status_code == 200
            assert rename_b.json()["title"] == "新版报销规范"

            # 普通成员可读并下载跨管理员协作生成的原件，但修改/上传/删除必须 403。
            download_member = fin_member.get(
                f"/api/v1/knowledge-bases/{fin_kb_id}/documents/{doc_id}/versions/{v2_id}/source"
            )
            assert download_member.status_code == 200
            assert download_member.content == b"file-bytes-by-admin-b"
            assert (
                fin_member.patch(
                    f"/api/v1/knowledge-bases/{fin_kb_id}",
                    json={"name": "越权改名"},
                ).status_code
                == 403
            )
            assert (
                fin_member.post(
                    f"/api/v1/knowledge-bases/{fin_kb_id}/documents/text",
                    headers={"Idempotency-Key": "member-upload-denied"},
                    json={"title": "越权文档", "text": "forbidden"},
                ).status_code
                == 403
            )
            assert (
                fin_member.post(
                    "/api/v1/knowledge-bases",
                    json={
                        "name": "普通员工试图创建部门库",
                        "visibility_scope": "department",
                        "owner_department_id": "dept_fin",
                    },
                ).status_code
                == 403
            )

            # 普通成员绑定公司库与财务库到个人项目（验证 project_knowledge_bases 复合外键已拆除）。
            co_kb_id = super_c.post(
                "/api/v1/knowledge-bases",
                json={"name": "公司总则", "visibility_scope": "company"},
            ).json()["id"]
            fin_proj_id = fin_member.post("/api/v1/projects", json={"name": "我的报销项目"}).json()[
                "id"
            ]
            assert (
                fin_member.put(
                    f"/api/v1/projects/{fin_proj_id}/knowledge-bases/{co_kb_id}"
                ).status_code
                == 204
            )
            assert (
                fin_member.put(
                    f"/api/v1/projects/{fin_proj_id}/knowledge-bases/{fin_kb_id}"
                ).status_code
                == 204
            )
            bound_ids = {
                item["id"]
                for item in fin_member.get(f"/api/v1/projects/{fin_proj_id}/knowledge-bases").json()
            }
            assert bound_ids == {co_kb_id, fin_kb_id}

            # 管理员 B 可删除管理员 A 上传的文档，且可对管辖子部门（dept_tax）授权。
            assert (
                fin_admin_b.delete(
                    f"/api/v1/knowledge-bases/{fin_kb_id}/documents/{doc_id}"
                ).status_code
                == 204
            )
            patch_tax = fin_admin_b.patch(
                f"/api/v1/knowledge-bases/{fin_kb_id}",
                json={
                    "visibility_grants": [
                        {
                            "target_type": "department",
                            "target_id": "dept_tax",
                            "include_subdepartments": True,
                        }
                    ]
                },
            )
            assert patch_tax.status_code == 200, patch_tax.text

            # --- 2. 部门管理员分权协同管理 Skill ---
            imp_a = fin_admin_a.post(
                "/api/v1/admin/skills/import",
                headers={"Idempotency-Key": "fin-skill-a"},
                data={"scope": "department", "owner_department_id": "dept_fin"},
                files={
                    "package": (
                        "fin.zip",
                        _make_skill_zip(
                            slug="fin-reimburse",
                            name="报销审核",
                            description="财务部门技能",
                            guide="v1 审核指南",
                        ),
                        "application/zip",
                    )
                },
            )
            assert imp_a.status_code == 201, imp_a.text
            fin_skill_id = imp_a.json()["id"]

            # 管理员 B 在 /api/v1/admin/skills 可看到并发布 v2、启停配置与软删除。
            admin_b_list = fin_admin_b.get("/api/v1/admin/skills")
            assert admin_b_list.status_code == 200
            assert [item["id"] for item in admin_b_list.json()["items"]] == [fin_skill_id]

            v2_resp = fin_admin_b.post(
                f"/api/v1/admin/skills/{fin_skill_id}/versions",
                headers={"Idempotency-Key": "fin-skill-b-v2"},
                params={"label": "v2"},
                files={
                    "package": (
                        "fin_v2.zip",
                        _make_skill_zip(
                            slug="fin-reimburse",
                            name="报销审核",
                            description="财务部门技能 v2",
                            guide="v2 审核指南",
                        ),
                        "application/zip",
                    )
                },
            )
            assert v2_resp.status_code == 201, v2_resp.text
            v2_skill_ver_id = v2_resp.json()["id"]

            cfg_b = fin_admin_b.patch(
                f"/api/v1/admin/skills/{fin_skill_id}",
                json={
                    "expected_revision": 1,
                    "enabled": True,
                    "current_version_id": v2_skill_ver_id,
                    "script_requires_approval": True,
                },
            )
            assert cfg_b.status_code == 200, cfg_b.text
            assert cfg_b.json()["revision"] == 2

            # --- 3. 全面防越权提权校验 ---
            co_skill_id = super_c.post(
                "/api/v1/admin/skills/import",
                headers={"Idempotency-Key": "co-skill-super"},
                data={"scope": "company"},
                files={
                    "package": (
                        "co.zip",
                        _make_skill_zip(
                            slug="co-global",
                            name="公司公共技能",
                            description="超管维护",
                            guide="公共指南",
                        ),
                        "application/zip",
                    )
                },
            ).json()["id"]

            # 部门管理员不得创建/修改/删除 company 级知识库或 Skill（403）。
            assert (
                fin_admin_a.post(
                    "/api/v1/knowledge-bases",
                    json={"name": "越权公司库", "visibility_scope": "company"},
                ).status_code
                == 403
            )
            assert (
                fin_admin_a.patch(
                    f"/api/v1/knowledge-bases/{co_kb_id}",
                    json={"name": "篡改公司库"},
                ).status_code
                == 403
            )
            assert (
                fin_admin_a.post(
                    "/api/v1/admin/skills/import",
                    headers={"Idempotency-Key": "fin-co-denied"},
                    data={"scope": "company"},
                    files={
                        "package": (
                            "co2.zip",
                            _make_skill_zip(slug="bad-co", name="越权", description="d", guide="g"),
                            "application/zip",
                        )
                    },
                ).status_code
                == 403
            )
            assert (
                fin_admin_a.patch(
                    f"/api/v1/admin/skills/{co_skill_id}",
                    json={
                        "expected_revision": 1,
                        "enabled": False,
                        "current_version_id": super_c.get(
                            f"/api/v1/admin/skills/{co_skill_id}"
                        ).json()["versions"][0]["id"],
                        "script_requires_approval": False,
                    },
                ).status_code
                == 403
            )

            # 部门管理员不得跨部门或按 role 授权（403）；超管允许跨部门/按角色授权。
            assert (
                fin_admin_a.patch(
                    f"/api/v1/knowledge-bases/{fin_kb_id}",
                    json={
                        "visibility_grants": [
                            {
                                "target_type": "department",
                                "target_id": "dept_rd",
                                "include_subdepartments": True,
                            }
                        ]
                    },
                ).status_code
                == 403
            )
            assert (
                fin_admin_a.patch(
                    f"/api/v1/knowledge-bases/{fin_kb_id}",
                    json={
                        "visibility_grants": [
                            {
                                "target_type": "role",
                                "target_id": "member",
                                "include_subdepartments": False,
                            }
                        ]
                    },
                ).status_code
                == 403
            )
            assert (
                super_c.patch(
                    f"/api/v1/knowledge-bases/{fin_kb_id}",
                    json={
                        "visibility_grants": [
                            {
                                "target_type": "department",
                                "target_id": "dept_fin",
                                "include_subdepartments": True,
                            },
                            {
                                "target_type": "department",
                                "target_id": "dept_rd",
                                "include_subdepartments": True,
                            },
                            {
                                "target_type": "role",
                                "target_id": "dept_admin",
                                "include_subdepartments": False,
                            },
                        ]
                    },
                ).status_code
                == 200
            )
            assert (
                fin_admin_a.patch(
                    f"/api/v1/admin/skills/{fin_skill_id}",
                    json={
                        "expected_revision": 2,
                        "enabled": True,
                        "current_version_id": v2_skill_ver_id,
                        "script_requires_approval": False,
                        "visibility_grants": [
                            {
                                "target_type": "department",
                                "target_id": "dept_rd",
                                "include_subdepartments": True,
                            }
                        ],
                    },
                ).status_code
                == 403
            )

            # 研发管理员无法查看财务部门 Skill（404），也无法进入 MCP/Subagent 管理路由（403）。
            assert rd_admin.get(f"/api/v1/admin/skills/{fin_skill_id}").status_code == 404
            assert fin_admin_a.get("/api/v1/admin/mcp/servers").status_code == 403
            assert fin_admin_a.get("/api/v1/admin/subagents").status_code == 403
            assert fin_member.get("/api/v1/admin/skills").status_code == 403

            # 删除部门 Skill 后同步清理 resource_visibility_grants，且不再出现在目录中。
            assert (
                fin_admin_b.delete(
                    f"/api/v1/admin/skills/{fin_skill_id}",
                    params={"expected_revision": 2},
                ).status_code
                == 204
            )
            async with h.session_factory() as session:
                remaining_grants = (
                    await session.scalars(
                        select(ResourceVisibilityGrantModel).where(
                            ResourceVisibilityGrantModel.resource_type == ResourceType.SKILL.value,
                            ResourceVisibilityGrantModel.resource_id == fin_skill_id,
                        )
                    )
                ).all()
            assert remaining_grants == []

            # 超管不得查看普通员工的个人私有知识库（404）。
            priv_kb_id = fin_member.post(
                "/api/v1/knowledge-bases", json={"name": "私人日记"}
            ).json()["id"]
            assert super_c.get(f"/api/v1/knowledge-bases/{priv_kb_id}").status_code == 404

            # 删除知识库后同步清理其 visibility_grants。
            assert fin_admin_b.delete(f"/api/v1/knowledge-bases/{fin_kb_id}").status_code == 204
            async with h.session_factory() as session:
                kb_grants = (
                    await session.scalars(
                        select(ResourceVisibilityGrantModel).where(
                            ResourceVisibilityGrantModel.resource_type
                            == ResourceType.KNOWLEDGE_BASE.value,
                            ResourceVisibilityGrantModel.resource_id == fin_kb_id,
                        )
                    )
                ).all()
            assert kb_grants == []
        finally:
            await h.aclose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 4. 员工调岗实时撤权（同一 Cookie 会话不重新登录，四条业务线与运行中 Run 立即失效）
# ---------------------------------------------------------------------------


def test_realtime_revocation_after_department_transfer(tmp_path: Path) -> None:
    """员工从财务部调岗至研发部后，保留原登录 Cookie，财务线四类资产立即失效。"""

    async def _scenario() -> None:
        h = await _make_harness(tmp_path / "transfer.db")
        try:
            super_c = h.login_client("super@acme.com")
            fin_admin = h.login_client("fin.admin.a@acme.com")
            # 同一 Cookie 客户端贯穿调岗前后，绝不重新登录！
            emp = h.login_client("fin.member@acme.com")

            # 创建公司级 + 财务部级四类资源。
            co_kb_id = super_c.post(
                "/api/v1/knowledge-bases",
                json={"name": "公司手册", "visibility_scope": "company"},
            ).json()["id"]
            fin_kb_id = fin_admin.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务机密库",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            ).json()["id"]
            co_doc_id = super_c.post(
                f"/api/v1/knowledge-bases/{co_kb_id}/documents/text",
                headers={"Idempotency-Key": "co-doc-transfer"},
                json={"title": "公司公告", "text": "SHARED-TOKEN 公司通用条款"},
            ).json()["document_id"]
            fin_doc_id = fin_admin.post(
                f"/api/v1/knowledge-bases/{fin_kb_id}/documents/text",
                headers={"Idempotency-Key": "fin-doc-transfer"},
                json={"title": "财务薪资表", "text": "SHARED-TOKEN 财务机密薪资"},
            ).json()["document_id"]
            await h.mark_document_ready_with_chunk(
                document_id=co_doc_id, content="SHARED-TOKEN 公司通用条款"
            )
            await h.mark_document_ready_with_chunk(
                document_id=fin_doc_id, content="SHARED-TOKEN 财务机密薪资"
            )

            fin_skill_id = _import_and_enable_skill(
                fin_admin,
                idempotency_key="fin-skill-transfer",
                scope="department",
                owner_department_id="dept_fin",
                slug="fin-secret-skill",
                name="财务机密技能",
                description="财务核算口径",
                guide="机密财务口径正文",
                resource_content="TOP-SECRET-RESOURCE",
            )

            super_c.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "fin-mcp-transfer",
                    "transport": "streamable_http",
                    "url": "https://mcp.fin.example/mcp",
                    "secrets": {},
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            fin_tool_id = super_c.get("/api/v1/tools").json()["items"][0]["tool_id"]

            super_c.post(
                "/api/v1/admin/subagents",
                json={
                    "slug": "fin_agent_transfer",
                    "name": "财务专员",
                    "description": "财务部专用",
                    "system_prompt": "财务专员。",
                    "allowed_tool_ids": [fin_tool_id],
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )

            # 调岗前：绑定两库到个人项目，并冻结运行中 Run 快照（含财务 Skill 与 MCP 工具）。
            proj_id = emp.post("/api/v1/projects", json={"name": "员工项目"}).json()["id"]
            assert (
                emp.put(f"/api/v1/projects/{proj_id}/knowledge-bases/{co_kb_id}").status_code == 204
            )
            assert (
                emp.put(f"/api/v1/projects/{proj_id}/knowledge-bases/{fin_kb_id}").status_code
                == 204
            )
            assert {
                item["id"] for item in emp.get(f"/api/v1/projects/{proj_id}/knowledge-bases").json()
            } == {co_kb_id, fin_kb_id}
            assert emp.get(f"/api/v1/knowledge-bases/{fin_kb_id}").status_code == 200
            assert fin_skill_id in {
                item["id"] for item in emp.get("/api/v1/skills").json()["items"]
            }
            assert fin_tool_id in {
                item["tool_id"] for item in emp.get("/api/v1/tools").json()["items"]
            }

            await h.create_chat_session(
                session_id="sess_emp", owner_id="u_fin_member", project_id=proj_id
            )
            auth_sid = await h.auth_session_id_for_user("u_fin_member")
            emp_auth = await h.tool_runtime.authorize(
                "u_fin_member", "sess_emp", "run_emp", auth_sid
            )
            assert emp_auth is not None

            # 调岗前冻结的 Run 工具快照与 Skill 快照均包含财务资源。
            frozen_tool_snap = await h.tool_runtime.build_snapshot("run-v2", emp_auth, depth=0)
            fin_tool_def = next(t for t in frozen_tool_snap.tools if t.tool_id == fin_tool_id)
            async with h.session_factory() as session:
                fin_snapshot = await load_user_access_snapshot_in_session(session, "u_fin_member")
                assert fin_snapshot is not None
                frozen_skill_snap = await build_run_skill_snapshot(
                    session, (fin_skill_id,), snapshot=fin_snapshot
                )
            assert [s.skill_id for s in frozen_skill_snap.skills] == [fin_skill_id]

            emp_run = _make_run(
                run_id="run_emp",
                session_id="sess_emp",
                owner_id="u_fin_member",
                auth_session_id=auth_sid,
                tool_snapshot=frozen_tool_snap,
                skill_snapshot=frozen_skill_snap,
            )
            exec_ctx = ToolExecutionContext(
                owner_id="u_fin_member",
                chat_session_id="sess_emp",
                project_id=proj_id,
                run_id="run_emp",
                execution_epoch=1,
                auth_session_id=auth_sid,
                deadline_at=NOW + timedelta(seconds=30),
                tool_call_record_id="tc_pre",
                skill_snapshot=frozen_skill_snap,
            )

            # 调岗前 skill_load、knowledge_search、MCP 执行全部成功。
            pre_load = await h.tool_runtime.execute(
                SKILL_LOAD_DEFINITION,
                SKILL_LOAD_DEFINITION.model_name,
                {"skill_id": fin_skill_id},
                exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert pre_load.outcome.status is ToolOutcomeStatus.OK
            pre_search = await h.tool_runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                KNOWLEDGE_SEARCH_DEFINITION.model_name,
                {"query": "SHARED-TOKEN", "limit": 5},
                exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert pre_search.outcome.status is ToolOutcomeStatus.OK
            pre_hit_kbs = {hit["base_id"] for hit in pre_search.outcome.data["items"]}
            assert pre_hit_kbs == {co_kb_id, fin_kb_id}

            # 调岗前 MCP 调用成功，随后员工调岗至研发部（未重新登录，同一 Cookie 会话）。
            pre_mcp_res = await h.tool_runtime.execute(
                fin_tool_def,
                fin_tool_def.model_name,
                {},
                exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert pre_mcp_res.outcome.status is ToolOutcomeStatus.OK
            assert len(h.mcp_executor.calls) == 1

            await h.transfer_user_department("u_fin_member", "dept_rd")

            # --- 调岗已生效（未重新登录，同一 Cookie 会话） ---
            # 1) 知识库：列表与已绑定项目均隐藏财务库，直连/检索全 404，knowledge_search 自动剔除。
            assert {item["id"] for item in emp.get("/api/v1/knowledge-bases").json()["items"]} == {
                co_kb_id
            }
            assert {
                item["id"] for item in emp.get(f"/api/v1/projects/{proj_id}/knowledge-bases").json()
            } == {co_kb_id}
            assert emp.get(f"/api/v1/knowledge-bases/{fin_kb_id}").status_code == 404
            assert (
                emp.post(
                    f"/api/v1/knowledge-bases/{fin_kb_id}/search",
                    json={"query": "SHARED-TOKEN"},
                ).status_code
                == 404
            )
            post_search = await h.tool_runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                KNOWLEDGE_SEARCH_DEFINITION.model_name,
                {"query": "SHARED-TOKEN", "limit": 5},
                exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert post_search.outcome.status is ToolOutcomeStatus.OK
            post_hit_kbs = {hit["base_id"] for hit in post_search.outcome.data["items"]}
            assert post_hit_kbs == {co_kb_id}

            # 2) Skill：目录消失、新快照拒绝，运行中 load/read/execute/context 全部实时拦截。
            assert emp.get("/api/v1/skills").json()["items"] == []
            with pytest.raises(SkillConflictError, match="skill_selection_unavailable"):
                async with h.session_factory() as session:
                    fin_post_snapshot = await load_user_access_snapshot_in_session(
                        session, "u_fin_member"
                    )
                    assert fin_post_snapshot is not None
                    await build_run_skill_snapshot(
                        session, (fin_skill_id,), snapshot=fin_post_snapshot
                    )

            post_load = await h.tool_runtime.execute(
                SKILL_LOAD_DEFINITION,
                SKILL_LOAD_DEFINITION.model_name,
                {"skill_id": fin_skill_id},
                exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert post_load.outcome.code == "tool_permission_denied"
            post_read = await h.tool_runtime.execute(
                SKILL_READ_RESOURCE_DEFINITION,
                SKILL_READ_RESOURCE_DEFINITION.model_name,
                {"skill_id": fin_skill_id, "path": "references/guide.txt"},
                exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert post_read.outcome.code == "tool_permission_denied"
            post_exec_prep = await h.tool_runtime.prepare_call(
                SKILL_EXECUTE_DEFINITION,
                SKILL_EXECUTE_DEFINITION.model_name,
                {"skill_id": fin_skill_id, "script_path": "scripts/run.py", "parameters": {}},
                emp_run,
                emp_auth,
                call_id="tc_skill_exec_1",
            )
            assert post_exec_prep.outcome is not None
            assert post_exec_prep.outcome.code == "tool_permission_denied"
            post_exec = await h.tool_runtime.execute(
                SKILL_EXECUTE_DEFINITION,
                SKILL_EXECUTE_DEFINITION.model_name,
                {"skill_id": fin_skill_id, "script_path": "scripts/run.py", "parameters": {}},
                exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert post_exec.outcome.code == "tool_permission_denied"
            assert h.skill_execution.calls == []

            # 下一轮 LLM 上下文不再重注已撤权 Skill 的完整指南。
            rebuilt_ctx = await h.tool_runtime.build_skill_context(
                emp_run,
                (fin_skill_id,),
            )
            assert rebuilt_ctx.catalog == ()
            assert rebuilt_ctx.active == ()

            # 3) MCP：目录消失、直连 404、执行前实时拦截（绝不再触发远端调用）。
            assert emp.get("/api/v1/tools").json()["items"] == []
            assert emp.get(f"/api/v1/tools/{fin_tool_id}").status_code == 404
            post_mcp_prep = await h.tool_runtime.prepare_call(
                fin_tool_def,
                fin_tool_def.model_name,
                {},
                emp_run,
                emp_auth,
                call_id="tc_mcp_post_1",
            )
            assert post_mcp_prep.outcome is not None
            assert post_mcp_prep.outcome.code == "tool_permission_denied"
            post_mcp_exec = await h.tool_runtime.execute(
                fin_tool_def,
                fin_tool_def.model_name,
                {},
                exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert post_mcp_exec.outcome.code == "tool_permission_denied"
            assert len(h.mcp_executor.calls) == 1

            # 4) Subagent：新快照中隐藏，且运行中 Run 强行调度被 RunExecutor 立即阻断。
            post_snap = await h.tool_runtime.build_snapshot("run-v2", emp_auth, depth=0)
            assert not any(t.model_name == "dispatch_subagents" for t in post_snap.tools)

            executor = RunExecutor(
                repository=cast(Any, object()),
                profiles=cast(Any, object()),
                provider=cast(Any, object()),
                system_prompts=cast(Any, object()),
                tool_runtime=h.tool_runtime,
                subagents=h.subagent_repo,
                access_policy=h.access_policy,
            )
            emp_state = cast(
                Any,
                SimpleNamespace(
                    run=emp_run,
                    authorized=emp_auth,
                    snapshot=frozen_tool_snap,
                    round=0,
                ),
            )
            with pytest.raises(LlmError, match="subagent_profile_unavailable"):
                await executor._dispatch_subagents(
                    emp_state,
                    LlmToolCall(
                        call_id="tc_sub_post",
                        name="dispatch_subagents",
                        arguments={
                            "tasks": [{"subagent_slug": "fin_agent_transfer", "task": "继续查财务"}]
                        },
                    ),
                    "",
                    _make_llm_response(),
                )
        finally:
            await h.aclose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 5. 范围切换授权清理、知识库列表筛选与管理路由前缀精确边界
# ---------------------------------------------------------------------------


def test_scope_cleanup_filters_and_admin_route_boundaries(tmp_path: Path) -> None:
    """验证 department->company 切换清理旧授权、删除清理授权、知识库筛选与管理前缀精确边界。"""

    async def _scenario() -> None:
        h = await _make_harness(tmp_path / "cleanup.db")
        try:
            super_c = h.login_client("super@acme.com")
            fin_admin = h.login_client("fin.admin.a@acme.com")

            async def _grant_count(resource_type: ResourceType, resource_id: str) -> int:
                async with h.session_factory() as session:
                    rows = (
                        await session.scalars(
                            select(ResourceVisibilityGrantModel).where(
                                ResourceVisibilityGrantModel.resource_type == resource_type.value,
                                ResourceVisibilityGrantModel.resource_id == resource_id,
                            )
                        )
                    ).all()
                return len(rows)

            # 1) 知识库从 department 升级为 company 后自动清理旧授权；列表筛选参数生效。
            kb_fin = super_c.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务库待升级",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            ).json()["id"]
            kb_rd = super_c.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "研发库",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_rd",
                },
            ).json()["id"]
            assert await _grant_count(ResourceType.KNOWLEDGE_BASE, kb_fin) == 1

            filtered = super_c.get(
                "/api/v1/knowledge-bases",
                params={"visibility_scope": "department", "owner_department_id": "dept_fin"},
            )
            assert filtered.status_code == 200
            assert [item["id"] for item in filtered.json()["items"]] == [kb_fin]

            promoted = super_c.patch(
                f"/api/v1/knowledge-bases/{kb_fin}",
                json={"visibility_scope": "company", "owner_department_id": None},
            )
            assert promoted.status_code == 200, promoted.text
            assert promoted.json()["visibility_grants"] == []
            assert await _grant_count(ResourceType.KNOWLEDGE_BASE, kb_fin) == 0

            # 2) MCP Server 切换为 company 清理旧授权，删除 department MCP Server 同步清理授权。
            mcp_fin = super_c.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "fin-mcp-cleanup",
                    "transport": "streamable_http",
                    "url": "https://mcp.fin.example/mcp",
                    "secrets": {},
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            assert mcp_fin.status_code == 201, mcp_fin.text
            mcp_id = mcp_fin.json()["id"]
            assert await _grant_count(ResourceType.MCP_SERVER, mcp_id) == 1

            mcp_promoted = super_c.put(
                f"/api/v1/admin/mcp/servers/{mcp_id}",
                json={
                    "name": "fin-mcp-cleanup",
                    "transport": "streamable_http",
                    "url": "https://mcp.fin.example/mcp",
                    "secrets": {},
                    "expected_config_version": 1,
                    "visibility_scope": "company",
                    "owner_department_id": None,
                },
            )
            assert mcp_promoted.status_code == 200, mcp_promoted.text
            assert await _grant_count(ResourceType.MCP_SERVER, mcp_id) == 0

            mcp_del = super_c.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "fin-mcp-del",
                    "transport": "streamable_http",
                    "url": "https://mcp.fin-strict.example/mcp",
                    "secrets": {},
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            assert mcp_del.status_code == 201, mcp_del.text
            mcp_del_id = mcp_del.json()["id"]
            assert await _grant_count(ResourceType.MCP_SERVER, mcp_del_id) == 1
            assert super_c.delete(f"/api/v1/admin/mcp/servers/{mcp_del_id}").status_code == 204
            assert await _grant_count(ResourceType.MCP_SERVER, mcp_del_id) == 0

            # 3) Subagent 切换为 company 与删除时同步清理授权。
            sub_fin = super_c.post(
                "/api/v1/admin/subagents",
                json={
                    "slug": "fin_cleanup_agent",
                    "name": "财务清理测试",
                    "description": "财务专用",
                    "system_prompt": "财务专用。",
                    "allowed_tool_ids": [],
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            assert sub_fin.status_code == 201, sub_fin.text
            sub_id = sub_fin.json()["id"]
            assert await _grant_count(ResourceType.SUBAGENT, sub_id) == 1
            sub_promoted = super_c.patch(
                f"/api/v1/admin/subagents/{sub_id}",
                json={
                    "version": 1,
                    "visibility_scope": "company",
                    "owner_department_id": None,
                },
            )
            assert sub_promoted.status_code == 200, sub_promoted.text
            assert await _grant_count(ResourceType.SUBAGENT, sub_id) == 0

            sub_del = super_c.post(
                "/api/v1/admin/subagents",
                json={
                    "slug": "fin_delete_agent",
                    "name": "财务删除测试",
                    "description": "财务专用",
                    "system_prompt": "财务专用。",
                    "allowed_tool_ids": [],
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            assert sub_del.status_code == 201, sub_del.text
            sub_del_id = sub_del.json()["id"]
            assert await _grant_count(ResourceType.SUBAGENT, sub_del_id) == 1
            assert super_c.delete(f"/api/v1/admin/subagents/{sub_del_id}").status_code == 204
            assert await _grant_count(ResourceType.SUBAGENT, sub_del_id) == 0

            # 4) company 级 Skill 不允许软删除（422）；dept_admin 不得访问 skill-executions。
            co_skill_id = _import_and_enable_skill(
                super_c,
                idempotency_key="co-skill-nodelete",
                scope="company",
                slug="co-nodelete",
                name="公司不可删技能",
                description="公司级",
                guide="指南",
            )
            assert (
                fin_admin.delete(
                    f"/api/v1/admin/skills/{co_skill_id}",
                    params={"expected_revision": 2},
                ).status_code
                == 403
            )
            assert (
                super_c.delete(
                    f"/api/v1/admin/skills/{co_skill_id}",
                    params={"expected_revision": 2},
                ).status_code
                == 422
            )
            assert fin_admin.get("/api/v1/admin/skill-executions/exec_1/log").status_code == 403
            assert kb_rd != kb_fin
        finally:
            await h.aclose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 6. Review Findings 专项回归（历史快照兼容、非子树管理员防越权、超管切 personal、幂等指纹）
# ---------------------------------------------------------------------------


def test_legacy_run_skill_snapshot_public_scope_is_rejected() -> None:
    """已移除历史 scope='public' 兼容转换：快照与激活指引必须拒绝非法 scope。"""
    with pytest.raises(ValidationError):
        RunSkillSnapshot.model_validate(
            {
                "version": 1,
                "skills": [
                    {
                        "skill_id": "skill_legacy_pub",
                        "slug": "legacy-pub",
                        "name": "历史公共技能",
                        "description": "迁移前持久化的公共技能快照",
                        "version_id": "skillver_legacy_1",
                        "version_label": "v1",
                        "requires_approval": False,
                        "scope": "public",
                    }
                ],
                "selected_skill_ids": ["skill_legacy_pub"],
            }
        )

    valid_snapshot = RunSkillSnapshot.model_validate(
        {
            "version": 1,
            "skills": [
                {
                    "skill_id": "skill_company",
                    "slug": "company-skill",
                    "name": "企业技能",
                    "description": "企业级技能快照",
                    "version_id": "skillver_company_1",
                    "version_label": "v1",
                    "requires_approval": False,
                    "scope": "company",
                },
                {
                    "skill_id": "skill_personal",
                    "slug": "personal-skill",
                    "name": "个人技能",
                    "description": "个人级技能快照",
                    "version_id": "skillver_personal_1",
                    "version_label": "v1",
                    "requires_approval": False,
                    "scope": "personal",
                },
            ],
            "selected_skill_ids": ["skill_company"],
        }
    )
    assert valid_snapshot.skills[0].scope is SkillScope.COMPANY
    assert valid_snapshot.skills[1].scope is SkillScope.PERSONAL

    with pytest.raises(ValidationError):
        ActiveSkillGuide.model_validate(
            {
                "skill_id": "skill_legacy_pub",
                "name": "历史公共技能",
                "version_id": "skillver_legacy_1",
                "version_label": "v1",
                "guide": "历史正文",
                "scope": "public",
            }
        )


def test_dept_admin_non_subtree_scope_cannot_escalate_to_subdepartments(
    tmp_path: Path,
) -> None:
    """include_subdepartments=False 的部门管理员默认仅授本部门，且显式传 True 必须返回 403。"""

    async def _scenario() -> None:
        h = await _make_harness(tmp_path / "non_subtree_admin.db")
        try:
            # 将 u_fin_admin_b 降级为仅管辖财务部本级（不含税务子组）。
            async with h.session_factory() as session, session.begin():
                scope_row = await session.scalar(
                    select(DeptAdminScopeModel).where(
                        DeptAdminScopeModel.user_id == "u_fin_admin_b",
                        DeptAdminScopeModel.department_id == "dept_fin",
                    )
                )
                assert scope_row is not None
                scope_row.include_subdepartments = False

            snap_b = await h.access_policy.get_user_snapshot("u_fin_admin_b")
            assert snap_b is not None
            assert snap_b.managed_department_ids == frozenset({"dept_fin"})
            assert snap_b.subtree_managed_department_ids == frozenset()

            fin_admin_b = h.login_client("fin.admin.b@acme.com")
            fin_member = h.login_client("fin.member@acme.com")
            tax_member = h.login_client("tax.member@acme.com")

            # 1) 创建部门知识库未传 visibility_grants 时，默认授权 include_subdepartments=False。
            kb_default = fin_admin_b.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务本级默认库",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            assert kb_default.status_code == 201, kb_default.text
            kb_id = kb_default.json()["id"]
            grants = kb_default.json()["visibility_grants"]
            assert len(grants) == 1
            assert grants[0]["target_id"] == "dept_fin"
            assert grants[0]["include_subdepartments"] is False
            # 财务直属员工可读，税务子组成员不可读（404）。
            assert fin_member.get(f"/api/v1/knowledge-bases/{kb_id}").status_code == 200
            assert tax_member.get(f"/api/v1/knowledge-bases/{kb_id}").status_code == 404

            # 显式传入 include_subdepartments=True（创建或更新）均被 403 拒绝。
            assert (
                fin_admin_b.post(
                    "/api/v1/knowledge-bases",
                    json={
                        "name": "越权穿透子部门库",
                        "visibility_scope": "department",
                        "owner_department_id": "dept_fin",
                        "visibility_grants": [
                            {
                                "target_type": "department",
                                "target_id": "dept_fin",
                                "include_subdepartments": True,
                            }
                        ],
                    },
                ).status_code
                == 403
            )
            assert (
                fin_admin_b.patch(
                    f"/api/v1/knowledge-bases/{kb_id}",
                    json={
                        "visibility_grants": [
                            {
                                "target_type": "department",
                                "target_id": "dept_fin",
                                "include_subdepartments": True,
                            }
                        ]
                    },
                ).status_code
                == 403
            )

            # 2) 创建部门 Skill 未传 visibility_grants 时同样默认 include_subdepartments=False。
            skill_imp = fin_admin_b.post(
                "/api/v1/admin/skills/import",
                headers={"Idempotency-Key": "non-subtree-skill-default"},
                data={"scope": "department", "owner_department_id": "dept_fin"},
                files={
                    "package": (
                        "fin-direct.zip",
                        _make_skill_zip(
                            slug="fin-direct-only",
                            name="财务本级技能",
                            description="仅财务部直属可见",
                            guide="本级指南",
                        ),
                        "application/zip",
                    )
                },
            )
            assert skill_imp.status_code == 201, skill_imp.text
            skill_payload = skill_imp.json()
            assert len(skill_payload["visibility_grants"]) == 1
            assert skill_payload["visibility_grants"][0]["include_subdepartments"] is False
            skill_id = skill_payload["id"]
            ver_id = skill_payload["versions"][0]["id"]
            assert (
                fin_admin_b.patch(
                    f"/api/v1/admin/skills/{skill_id}",
                    json={
                        "expected_revision": 1,
                        "enabled": True,
                        "current_version_id": ver_id,
                        "script_requires_approval": False,
                    },
                ).status_code
                == 200
            )
            assert skill_id in {
                item["id"] for item in fin_member.get("/api/v1/skills").json()["items"]
            }
            assert skill_id not in {
                item["id"] for item in tax_member.get("/api/v1/skills").json()["items"]
            }

            # 显式把 Skill 授权改为 include_subdepartments=True 同样 403 拒绝。
            assert (
                fin_admin_b.patch(
                    f"/api/v1/admin/skills/{skill_id}",
                    json={
                        "expected_revision": 2,
                        "enabled": True,
                        "current_version_id": ver_id,
                        "script_requires_approval": False,
                        "visibility_grants": [
                            {
                                "target_type": "department",
                                "target_id": "dept_fin",
                                "include_subdepartments": True,
                            }
                        ],
                    },
                ).status_code
                == 403
            )
            assert (
                fin_admin_b.post(
                    "/api/v1/admin/skills/import",
                    headers={"Idempotency-Key": "non-subtree-skill-escalate"},
                    data={
                        "scope": "department",
                        "owner_department_id": "dept_fin",
                        "visibility_grants": json.dumps(
                            [
                                {
                                    "target_type": "department",
                                    "target_id": "dept_fin",
                                    "include_subdepartments": True,
                                }
                            ]
                        ),
                    },
                    files={
                        "package": (
                            "fin-esc.zip",
                            _make_skill_zip(
                                slug="fin-esc",
                                name="越权技能",
                                description="越权穿透子部门",
                                guide="越权",
                            ),
                            "application/zip",
                        )
                    },
                ).status_code
                == 403
            )
        finally:
            await h.aclose()

    _run(_scenario())


def test_super_admin_cannot_convert_others_resource_to_personal_and_policy_fails_closed(
    tmp_path: Path,
) -> None:
    """super_admin 不得将他人部门/公司知识库改为 personal（403），且缺省 owner_id 保守拒访。"""

    async def _scenario() -> None:
        h = await _make_harness(tmp_path / "super_personal.db")
        try:
            super_c = h.login_client("super@acme.com")
            fin_admin = h.login_client("fin.admin.a@acme.com")

            # 1) 纯领域规则：owner_id=None 时对 PERSONAL 保守返回 False。
            super_snap = await h.access_policy.get_user_snapshot("u_super")
            assert super_snap is not None
            assert (
                evaluate_can_manage(
                    super_snap,
                    ResourceType.KNOWLEDGE_BASE,
                    VisibilityScope.PERSONAL,
                    owner_id=None,
                )
                is False
            )

            # 2) fin_admin 创建部门知识库 -> super_admin 试图切换为 personal 必须返回 403。
            dept_kb_id = fin_admin.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务管理员创建的部门库",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            ).json()["id"]
            assert (
                super_c.patch(
                    f"/api/v1/knowledge-bases/{dept_kb_id}",
                    json={"visibility_scope": "personal", "owner_department_id": None},
                ).status_code
                == 403
            )

            # 即使先由超管转为 company（owner_id 仍为 u_fin_admin_a），再改 personal 仍 403。
            assert (
                super_c.patch(
                    f"/api/v1/knowledge-bases/{dept_kb_id}",
                    json={"visibility_scope": "company", "owner_department_id": None},
                ).status_code
                == 200
            )
            assert (
                super_c.patch(
                    f"/api/v1/knowledge-bases/{dept_kb_id}",
                    json={"visibility_scope": "personal", "owner_department_id": None},
                ).status_code
                == 403
            )

            # 3) 超管本人创建的 company 知识库允许切回本人的 personal。
            own_co_kb_id = super_c.post(
                "/api/v1/knowledge-bases",
                json={"name": "超管自建公司库", "visibility_scope": "company"},
            ).json()["id"]
            assert (
                super_c.patch(
                    f"/api/v1/knowledge-bases/{own_co_kb_id}",
                    json={"visibility_scope": "personal", "owner_department_id": None},
                ).status_code
                == 200
            )
        finally:
            await h.aclose()

    _run(_scenario())


def test_skill_import_idempotency_fingerprint_covers_visibility_grants(
    tmp_path: Path,
) -> None:
    """相同 Idempotency-Key 与相同 ZIP 包，若 visibility_grants 变更必须返回 409 冲突。"""

    async def _scenario() -> None:
        h = await _make_harness(tmp_path / "skill_idem.db")
        try:
            fin_admin = h.login_client("fin.admin.a@acme.com")
            zip_bytes = _make_skill_zip(
                slug="idem-grant-check",
                name="幂等授权校验",
                description="验证授权变更触发幂等冲突",
                guide="正文",
            )
            first = fin_admin.post(
                "/api/v1/admin/skills/import",
                headers={"Idempotency-Key": "idem-grants-key"},
                data={
                    "scope": "department",
                    "owner_department_id": "dept_fin",
                    "visibility_grants": json.dumps(
                        [
                            {
                                "target_type": "department",
                                "target_id": "dept_fin",
                                "include_subdepartments": True,
                            }
                        ]
                    ),
                },
                files={"package": ("idem.zip", zip_bytes, "application/zip")},
            )
            assert first.status_code == 201, first.text

            # 同一 Idempotency-Key 但修改 include_subdepartments=False，必须返回 409 而非误重放。
            conflict = fin_admin.post(
                "/api/v1/admin/skills/import",
                headers={"Idempotency-Key": "idem-grants-key"},
                data={
                    "scope": "department",
                    "owner_department_id": "dept_fin",
                    "visibility_grants": json.dumps(
                        [
                            {
                                "target_type": "department",
                                "target_id": "dept_fin",
                                "include_subdepartments": False,
                            }
                        ]
                    ),
                },
                files={"package": ("idem.zip", zip_bytes, "application/zip")},
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"] == "idempotency_conflict"
        finally:
            await h.aclose()

    _run(_scenario())


def test_knowledge_base_rename_only_patch_preserves_department_visibility_and_grants(
    tmp_path: Path,
) -> None:
    """部门管理员仅改名 PATCH 部门知识库时，必须在行锁内原样保留可见范围、归属部门与全部授权。"""

    async def _scenario() -> None:
        h = await _make_harness(tmp_path / "kb_rename_only.db")
        try:
            fin_admin = h.login_client("fin.admin.a@acme.com")
            fin_member = h.login_client("fin.member@acme.com")
            tax_member = h.login_client("tax.member@acme.com")
            rd_member = h.login_client("rd.member@acme.com")

            created = fin_admin.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "财务制度库",
                    "description": "初始描述",
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                    "visibility_grants": [
                        {
                            "target_type": "department",
                            "target_id": "dept_fin",
                            "include_subdepartments": True,
                        }
                    ],
                },
            )
            assert created.status_code == 201, created.text
            fin_kb_id = created.json()["id"]
            grants_before = created.json()["visibility_grants"]
            assert len(grants_before) == 1
            assert grants_before[0]["target_id"] == "dept_fin"
            assert grants_before[0]["include_subdepartments"] is True

            # 仅修改名称与说明，不携带任何可见性字段。
            patched = fin_admin.patch(
                f"/api/v1/knowledge-bases/{fin_kb_id}",
                json={"name": "财务制度库（2026修订版）", "description": "仅更新元数据"},
            )
            assert patched.status_code == 200, patched.text
            payload = patched.json()
            assert payload["name"] == "财务制度库（2026修订版）"
            assert payload["description"] == "仅更新元数据"
            assert payload["visibility_scope"] == "department"
            assert payload["owner_department_id"] == "dept_fin"
            assert payload["visibility_grants"] == grants_before

            # 核验数据库持久化状态未发生任何可见性或授权覆写/重建。
            async with h.session_factory() as session:
                kb_row = await session.get(KnowledgeBaseModel, fin_kb_id)
                assert kb_row is not None
                assert kb_row.name == "财务制度库（2026修订版）"
                assert kb_row.description == "仅更新元数据"
                assert kb_row.visibility_scope == "department"
                assert kb_row.owner_department_id == "dept_fin"
                db_grants = (
                    await session.scalars(
                        select(ResourceVisibilityGrantModel).where(
                            ResourceVisibilityGrantModel.resource_type == "knowledge_base",
                            ResourceVisibilityGrantModel.resource_id == fin_kb_id,
                        )
                    )
                ).all()
                assert len(db_grants) == 1
                assert db_grants[0].id == grants_before[0]["id"]
                assert db_grants[0].target_type == "department"
                assert db_grants[0].target_id == "dept_fin"
                assert db_grants[0].include_subdepartments is True

            # 核验部门成员及子部门成员仍可读，非本部门成员仍被 404 隔离。
            assert fin_member.get(f"/api/v1/knowledge-bases/{fin_kb_id}").status_code == 200
            assert tax_member.get(f"/api/v1/knowledge-bases/{fin_kb_id}").status_code == 200
            assert rd_member.get(f"/api/v1/knowledge-bases/{fin_kb_id}").status_code == 404
        finally:
            await h.aclose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 7. Alembic 基线迁移 0001_initial_schema 权限与外键离线 SQL 验证
# ---------------------------------------------------------------------------


def test_alembic_initial_schema_contains_access_grants_and_fks() -> None:
    """验证 Alembic 基线 0001_initial_schema 单一 head 及权限表、外键、唯一索引离线 SQL。"""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["0001_initial_schema"]
    revision = script.get_revision("0001_initial_schema")
    assert revision is not None
    assert revision.down_revision is None

    up_buf = StringIO()
    with redirect_stdout(up_buf):
        command.upgrade(config, "head", sql=True)
    up_sql = up_buf.getvalue()
    for expected in (
        "CREATE TABLE resource_visibility_grants",
        "uq_resource_visibility_grant",
        "ix_resource_visibility_grants_lookup",
        "visibility_scope",
        "owner_department_id",
        "ix_knowledge_bases_visibility_dept",
        "fk_knowledge_documents_base",
        "fk_knowledge_versions_document",
        "fk_knowledge_versions_source",
        "fk_project_knowledge_bases_base",
        "uq_skills_company_slug",
        "uq_skills_department_slug",
    ):
        assert expected in up_sql, f"升级 SQL 缺少预期片段: {expected}"
    # 冗余前缀索引 ix_resource_visibility_grants_resource 已由 uq_resource_visibility_grant 覆盖。
    assert "ix_resource_visibility_grants_resource" not in up_sql

    down_buf = StringIO()
    with redirect_stdout(down_buf):
        command.downgrade(config, "0001_initial_schema:base", sql=True)
    down_sql = down_buf.getvalue()
    assert "DROP TABLE resource_visibility_grants" in down_sql
