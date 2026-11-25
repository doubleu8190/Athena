"""Phase 2-1 模块 A（权限引擎 SQL 下推与高频读接口会话合并）专项单元测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import DateTime, delete, event, func, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from adapter.access import (
    ResourceVisibilityGrantModel,
    SqlAlchemyAccessControlRepository,
    build_readable_resource_clause,
    load_grants_map_in_session,
)
from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.knowledge.model import KnowledgeBaseModel
from adapter.knowledge.repository import KnowledgeRepository
from adapter.llm.model.profile import DefaultModelProfileModel, ModelProfileVersionModel
from adapter.llm.repository.profile import SqlAlchemyModelProfileRepository
from adapter.mcp.model import McpServerModel, McpToolModel
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.models import Base
from adapter.org.model import DepartmentModel, DeptAdminScopeModel, UserDepartmentModel
from adapter.run.model import RunEventModel, RunInputRequestModel, RunModel, RunToolCallModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from adapter.skill.model import SkillModel, SkillVersionModel
from adapter.skill.repository import SqlAlchemySkillRepository
from adapter.subagent.model import SubagentModel
from adapter.subagent.repository import SqlAlchemySubagentRepository
from adapter.system_prompt.model import SystemPromptSettingModel, SystemPromptVersionModel
from app.access import AccessPolicyService
from app.knowledge.service import (
    KnowledgeNotFoundError,
    KnowledgePermissionDeniedError,
    KnowledgeService,
)
from app.run.service import RunService
from app.skill.service import SkillService
from app.tool.manager import ToolManager
from app.tool.runtime import KNOWLEDGE_SEARCH_DEFINITION, RunToolRuntimeService
from domain.access import (
    GrantTargetType,
    ResourceAccessTarget,
    ResourceType,
    UserAccessSnapshot,
    VisibilityScope,
    evaluate_can_manage,
    evaluate_can_read,
)
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.knowledge.model import ChunkLocator, SearchHit
from domain.knowledge.ports import KnowledgeRepositoryPort
from domain.llm.model.profile import ModelProfileInput
from domain.llm.model.profile_errors import ModelProfileAuthorizationError
from domain.mcp import McpCallResult, McpCallStatus, McpSecretPurpose
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
)
from domain.org import DepartmentStatus
from domain.run.model import (
    InputRequestStatus,
    Run,
    RunEvent,
    RunInputRequest,
    RunQueuedPayload,
    RunStatus,
    RunUsage,
    SubmitRunCommand,
)
from domain.run.model.errors import RunKnowledgeBaseNotFoundError, RunNotFoundError
from domain.run.ports import RunRepository
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    PackageResource,
    SkillConflictError,
    SkillPackageManifest,
    SkillScope,
)
from domain.tool import (
    AuthorizedRunContext,
    McpToolSource,
    ToolCallStatus,
    ToolExecutionContext,
    ToolKind,
    ToolOutcomeStatus,
)
from infrastructure.security.mcp_secrets import McpSecretCipher

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
SECRET_KEY = bytes(range(32))
_PG_DIALECT: Dialect = postgresql.dialect()  # type: ignore[no-untyped-call]


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步测试函数中运行异步协程。"""
    return asyncio.run(coroutine)


class _CountingSessionFactory:
    """包装 async_sessionmaker 以统计打开的数据库 Session 次数。"""

    def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
        self._inner = inner
        self.count = 0

    def reset(self) -> None:
        self.count = 0

    def __call__(self) -> Any:
        self.count += 1
        return self._inner()


class _SimpleTokenizer:
    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(part for part in text.strip().split() if part)


class _NoopFileStore:
    async def start(self) -> None:
        return None

    async def put_file(self, key: str, path: Any, *, content_type: str) -> None:
        del key, path, content_type

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        del key, content, content_type

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        del key, maximum_bytes
        return b""

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        del key, offset, length
        return b""

    async def delete(self, key: str) -> None:
        del key

    async def aclose(self) -> None:
        return None


class _NoopMcpDiscovery:
    async def discover(self, connection: Any) -> tuple[Any, ...]:
        del connection
        return ()


class _NoopMcpExecutor:
    async def call(
        self, target: Any, arguments: dict[str, Any], *, deadline_seconds: float
    ) -> McpCallResult:
        del target, arguments, deadline_seconds
        return McpCallResult(status=McpCallStatus.OK, code="ok", message="ok")


class _StubAuthReader:
    def __init__(self, users: dict[str, User]) -> None:
        self._users = users

    async def get_current_session_user(
        self, user_id: str, session_id: str, now: datetime
    ) -> User | None:
        del session_id, now
        return self._users.get(user_id)


async def _create_sqlite_db(
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """初始化内存 SQLite 数据库并按需注册 PostgreSQL clock_timestamp 兼容函数。"""
    if monkeypatch is not None:
        monkeypatch.setattr(
            func,
            "clock_timestamp",
            lambda: literal(NOW, type_=DateTime(timezone=True)),
        )
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _skill_manifest(slug: str, name: str) -> dict[str, object]:
    return SkillPackageManifest(
        slug=slug,
        name=name,
        description=f"{name} 描述",
        guide=f"# {name}",
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=16,
                sha256="a" * 64,
                text=True,
            ),
        ),
    ).model_dump(mode="json")


def _satisfied_dep_check() -> dict[str, object]:
    return DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.12.0",
        python_implementation="CPython",
        items=(),
        checked_at=NOW,
    ).model_dump(mode="json")


async def _seed_org_and_users(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, User]:
    """构建覆盖活跃/归档部门、子部门树与各类角色/边界用户的测试组织架构。"""
    depts = [
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
        ),
        DepartmentModel(
            id="dept_fin",
            external_id="ext_fin",
            name="财务部",
            parent_id="dept_root",
            path="/dept_root/dept_fin/",
            order_index=1,
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
            order_index=2,
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
            order_index=3,
            status=DepartmentStatus.ACTIVE.value,
            created_at=NOW,
            updated_at=NOW,
        ),
        DepartmentModel(
            id="dept_archived",
            external_id="ext_archived",
            name="已归档事业部",
            parent_id="dept_root",
            path="/dept_root/dept_archived/",
            order_index=4,
            status=DepartmentStatus.ARCHIVED.value,
            created_at=NOW,
            updated_at=NOW,
        ),
        DepartmentModel(
            id="dept_under_archived",
            external_id="ext_under_archived",
            name="归档父级下活跃子组",
            parent_id="dept_archived",
            path="/dept_root/dept_archived/dept_under_archived/",
            order_index=5,
            status=DepartmentStatus.ACTIVE.value,
            created_at=NOW,
            updated_at=NOW,
        ),
    ]
    user_specs: list[tuple[str, str, UserRole, UserStatus, list[str]]] = [
        ("u_super", "super@acme.test", UserRole.SUPER_ADMIN, UserStatus.ACTIVE, []),
        ("u_admin_sub", "admin.sub@acme.test", UserRole.DEPT_ADMIN, UserStatus.ACTIVE, ["dept_rd"]),
        ("u_admin_direct", "admin.dir@acme.test", UserRole.DEPT_ADMIN, UserStatus.ACTIVE, []),
        ("u_admin_empty", "admin.emp@acme.test", UserRole.DEPT_ADMIN, UserStatus.ACTIVE, []),
        ("u_fin", "fin@acme.test", UserRole.MEMBER, UserStatus.ACTIVE, ["dept_fin"]),
        ("u_tax", "tax@acme.test", UserRole.MEMBER, UserStatus.ACTIVE, ["dept_tax"]),
        ("u_rd", "rd@acme.test", UserRole.MEMBER, UserStatus.ACTIVE, ["dept_rd"]),
        (
            "u_under_archived",
            "arch@acme.test",
            UserRole.MEMBER,
            UserStatus.ACTIVE,
            ["dept_under_archived"],
        ),
        ("u_no_dept", "nodept@acme.test", UserRole.MEMBER, UserStatus.ACTIVE, []),
        ("u_disabled", "dis@acme.test", UserRole.SUPER_ADMIN, UserStatus.DISABLED, ["dept_fin"]),
    ]
    users: dict[str, User] = {}
    async with factory() as session, session.begin():
        session.add_all(depts)
        await session.flush()
        for uid, email, role, status, dept_ids in user_specs:
            session.add(
                UserModel(
                    id=uid,
                    email=email,
                    password_hash="hash",
                    display_name=uid,
                    status=status.value,
                    role=role.value,
                    created_at=NOW,
                    updated_at=NOW,
                    deleted_time=None,
                )
            )
            users[uid] = User(
                id=uid,
                email=email,
                password_hash="hash",
                display_name=uid,
                status=status,
                created_at=NOW,
                updated_at=NOW,
                role=role,
            )
            for idx, did in enumerate(dept_ids):
                session.add(
                    UserDepartmentModel(
                        user_id=uid,
                        department_id=did,
                        is_primary=(idx == 0),
                    )
                )
        await session.flush()
        session.add_all(
            [
                DeptAdminScopeModel(
                    user_id="u_admin_sub",
                    department_id="dept_fin",
                    include_subdepartments=True,
                    created_at=NOW,
                ),
                DeptAdminScopeModel(
                    user_id="u_admin_direct",
                    department_id="dept_fin",
                    include_subdepartments=False,
                    created_at=NOW,
                ),
            ]
        )
    return users


async def _seed_all_four_resources(factory: async_sessionmaker[AsyncSession]) -> None:
    """为四类资源表各自预置 company、personal、department（多授权组合）与软删除数据。"""

    def _grant(
        gid: str,
        rtype: ResourceType,
        rid: str,
        ttype: GrantTargetType,
        tid: str,
        include_sub: bool = True,
    ) -> ResourceVisibilityGrantModel:
        return ResourceVisibilityGrantModel(
            id=gid,
            resource_type=rtype.value,
            resource_id=rid,
            target_type=ttype.value,
            target_id=tid,
            include_subdepartments=include_sub,
            created_at=NOW,
        )

    async with factory() as session, session.begin():
        # 1. KnowledgeBase（visibility_scope / owner_id / deleted_time）
        session.add_all(
            [
                KnowledgeBaseModel(
                    id="kb_co",
                    owner_id="u_super",
                    name="公司库",
                    description=None,
                    visibility_scope=VisibilityScope.COMPANY.value,
                    owner_department_id=None,
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=10),
                ),
                KnowledgeBaseModel(
                    id="kb_pers_fin",
                    owner_id="u_fin",
                    name="财务个人库",
                    description=None,
                    visibility_scope=VisibilityScope.PERSONAL.value,
                    owner_department_id=None,
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=9),
                ),
                KnowledgeBaseModel(
                    id="kb_pers_super",
                    owner_id="u_super",
                    name="超管个人库",
                    description=None,
                    visibility_scope=VisibilityScope.PERSONAL.value,
                    owner_department_id=None,
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=8),
                ),
                KnowledgeBaseModel(
                    id="kb_dept_nogrant",
                    owner_id="u_super",
                    name="财务无授权库",
                    description=None,
                    visibility_scope=VisibilityScope.DEPARTMENT.value,
                    owner_department_id="dept_fin",
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=7),
                ),
                KnowledgeBaseModel(
                    id="kb_dept_tax_nogrant",
                    owner_id="u_super",
                    name="税务无授权库",
                    description=None,
                    visibility_scope=VisibilityScope.DEPARTMENT.value,
                    owner_department_id="dept_tax",
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=6),
                ),
                KnowledgeBaseModel(
                    id="kb_dept_fin_sub",
                    owner_id="u_super",
                    name="财务含子部门库",
                    description=None,
                    visibility_scope=VisibilityScope.DEPARTMENT.value,
                    owner_department_id="dept_fin",
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=5),
                ),
                KnowledgeBaseModel(
                    id="kb_dept_fin_direct",
                    owner_id="u_super",
                    name="财务仅直属库",
                    description=None,
                    visibility_scope=VisibilityScope.DEPARTMENT.value,
                    owner_department_id="dept_fin",
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=4),
                ),
                KnowledgeBaseModel(
                    id="kb_dept_arch_grant",
                    owner_id="u_super",
                    name="归档祖先授权库",
                    description=None,
                    visibility_scope=VisibilityScope.DEPARTMENT.value,
                    owner_department_id="dept_rd",
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=3),
                ),
                KnowledgeBaseModel(
                    id="kb_dept_role_member",
                    owner_id="u_super",
                    name="角色授权成员库",
                    description=None,
                    visibility_scope=VisibilityScope.DEPARTMENT.value,
                    owner_department_id="dept_rd",
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=2),
                ),
                KnowledgeBaseModel(
                    id="kb_dept_role_dadmin",
                    owner_id="u_super",
                    name="角色授权部管库",
                    description=None,
                    visibility_scope=VisibilityScope.DEPARTMENT.value,
                    owner_department_id="dept_rd",
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=1),
                ),
                KnowledgeBaseModel(
                    id="kb_deleted",
                    owner_id="u_super",
                    name="已删除库",
                    description=None,
                    visibility_scope=VisibilityScope.COMPANY.value,
                    owner_department_id=None,
                    created_at=NOW,
                    updated_at=NOW,
                    deleted_time=NOW,
                ),
            ]
        )

        # 2. Skill（scope / owner_id / deleted_at）+ SkillVersion
        skill_specs: list[tuple[str, str, str, str | None, str | None, datetime | None]] = [
            ("sk_co", "sk-co", SkillScope.COMPANY.value, None, None, None),
            ("sk_pers_fin", "sk-pers-fin", SkillScope.PERSONAL.value, "u_fin", None, None),
            ("sk_dept_nogrant", "sk-dept-ng", SkillScope.DEPARTMENT.value, None, "dept_fin", None),
            ("sk_dept_fin_sub", "sk-dept-sub", SkillScope.DEPARTMENT.value, None, "dept_fin", None),
            (
                "sk_dept_fin_direct",
                "sk-dept-dir",
                SkillScope.DEPARTMENT.value,
                None,
                "dept_fin",
                None,
            ),
            (
                "sk_dept_arch_grant",
                "sk-dept-arch",
                SkillScope.DEPARTMENT.value,
                None,
                "dept_rd",
                None,
            ),
            (
                "sk_dept_role_member",
                "sk-dept-rm",
                SkillScope.DEPARTMENT.value,
                None,
                "dept_rd",
                None,
            ),
            ("sk_deleted", "sk-del", SkillScope.DEPARTMENT.value, None, "dept_fin", NOW),
        ]
        for idx, (sid, slug, scope, owner_id, dept_id, deleted_at) in enumerate(skill_specs):
            ver_id = f"ver_{sid}"
            session.add(
                SkillModel(
                    id=sid,
                    slug=slug,
                    scope=scope,
                    owner_id=owner_id,
                    owner_department_id=dept_id,
                    deleted_at=deleted_at,
                    current_version_id=None,
                    enabled=True,
                    script_requires_approval=False,
                    format="markdown",
                    featured=False,
                    starter_prompt=None,
                    revision=1,
                    created_by="u_super",
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=idx),
                )
            )
            await session.flush()
            session.add(
                SkillVersionModel(
                    id=ver_id,
                    skill_id=sid,
                    ordinal=1,
                    label="v1",
                    guide=f"# {slug}",
                    manifest=_skill_manifest(slug, slug),
                    object_key=f"skills/aa/{sid}.zip",
                    package_sha256="b" * 64,
                    dependency_requirements=[],
                    dependency_check=_satisfied_dep_check(),
                    created_by="u_super",
                    request_operation="import",
                    idempotency_key=f"idem-{sid}",
                    request_fingerprint="c" * 64,
                    request_target_skill_id=None,
                    created_at=NOW,
                )
            )
            await session.flush()
            sk_row = await session.get(SkillModel, sid)
            assert sk_row is not None
            sk_row.current_version_id = ver_id

        # 3. McpServer（visibility_scope / 无 owner_id / deleted_at）+ McpTool
        mcp_specs: list[tuple[str, str, str, str | None, datetime | None]] = [
            ("mcp_co", "mcp-co", VisibilityScope.COMPANY.value, None, None),
            ("mcp_dept_nogrant", "mcp-ng", VisibilityScope.DEPARTMENT.value, "dept_fin", None),
            ("mcp_dept_fin_sub", "mcp-sub", VisibilityScope.DEPARTMENT.value, "dept_fin", None),
            ("mcp_dept_fin_direct", "mcp-dir", VisibilityScope.DEPARTMENT.value, "dept_fin", None),
            ("mcp_dept_arch_grant", "mcp-arch", VisibilityScope.DEPARTMENT.value, "dept_rd", None),
            ("mcp_dept_role_member", "mcp-rm", VisibilityScope.DEPARTMENT.value, "dept_rd", None),
            ("mcp_deleted", "mcp-del", VisibilityScope.COMPANY.value, None, NOW),
        ]
        for idx, (mid, name, scope, dept_id, deleted_at) in enumerate(mcp_specs):
            session.add(
                McpServerModel(
                    id=mid,
                    name=name,
                    transport="streamable_http",
                    config_json={"transport": "streamable_http", "url": f"https://{name}.test"},
                    secrets_ciphertext=None,
                    secrets_nonce=None,
                    key_version=None,
                    config_version=1,
                    enabled=True,
                    visibility_scope=scope,
                    owner_department_id=dept_id,
                    created_by="u_super",
                    deleted_at=deleted_at,
                    created_at=NOW,
                    updated_at=NOW,
                    tools_discovered_at=NOW,
                )
            )
            session.add(
                McpToolModel(
                    id=f"tool_{idx:032x}",
                    server_id=mid,
                    remote_name=f"op_{mid}",
                    model_name=f"mcp__{idx:032x}",
                    description=f"tool for {mid}",
                    input_schema_json={"type": "object", "properties": {}},
                    requires_approval=False,
                    created_at=NOW,
                )
            )

        # 4. Subagent（visibility_scope / 无 owner_id / deleted_at）
        sub_specs: list[tuple[str, str, str, str | None, datetime | None]] = [
            ("sub_co", "sub_co", VisibilityScope.COMPANY.value, None, None),
            ("sub_dept_nogrant", "sub_ng", VisibilityScope.DEPARTMENT.value, "dept_fin", None),
            ("sub_dept_fin_sub", "sub_sub", VisibilityScope.DEPARTMENT.value, "dept_fin", None),
            ("sub_dept_fin_direct", "sub_dir", VisibilityScope.DEPARTMENT.value, "dept_fin", None),
            ("sub_dept_arch_grant", "sub_arch", VisibilityScope.DEPARTMENT.value, "dept_rd", None),
            ("sub_dept_role_member", "sub_rm", VisibilityScope.DEPARTMENT.value, "dept_rd", None),
            ("sub_deleted", "sub_del", VisibilityScope.COMPANY.value, None, NOW),
        ]
        for sid, slug, scope, dept_id, deleted_at in sub_specs:
            session.add(
                SubagentModel(
                    id=sid,
                    slug=slug,
                    name=slug,
                    description=f"desc {slug}",
                    system_prompt="prompt",
                    model_profile_id=None,
                    allowed_tool_ids=[],
                    enabled=True,
                    visibility_scope=scope,
                    owner_department_id=dept_id,
                    version=1,
                    created_by="u_super",
                    deleted_at=deleted_at,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

        await session.flush()
        # 写入四类资源的可见性授权
        grants = [
            # KnowledgeBase
            _grant(
                "g_kb_1",
                ResourceType.KNOWLEDGE_BASE,
                "kb_dept_fin_sub",
                GrantTargetType.DEPARTMENT,
                "dept_fin",
                True,
            ),
            _grant(
                "g_kb_2",
                ResourceType.KNOWLEDGE_BASE,
                "kb_dept_fin_direct",
                GrantTargetType.DEPARTMENT,
                "dept_fin",
                False,
            ),
            _grant(
                "g_kb_3",
                ResourceType.KNOWLEDGE_BASE,
                "kb_dept_arch_grant",
                GrantTargetType.DEPARTMENT,
                "dept_archived",
                True,
            ),
            _grant(
                "g_kb_4",
                ResourceType.KNOWLEDGE_BASE,
                "kb_dept_role_member",
                GrantTargetType.ROLE,
                UserRole.MEMBER.value,
                False,
            ),
            _grant(
                "g_kb_5",
                ResourceType.KNOWLEDGE_BASE,
                "kb_dept_role_dadmin",
                GrantTargetType.ROLE,
                UserRole.DEPT_ADMIN.value,
                False,
            ),
            # Skill
            _grant(
                "g_sk_1",
                ResourceType.SKILL,
                "sk_dept_fin_sub",
                GrantTargetType.DEPARTMENT,
                "dept_fin",
                True,
            ),
            _grant(
                "g_sk_2",
                ResourceType.SKILL,
                "sk_dept_fin_direct",
                GrantTargetType.DEPARTMENT,
                "dept_fin",
                False,
            ),
            _grant(
                "g_sk_3",
                ResourceType.SKILL,
                "sk_dept_arch_grant",
                GrantTargetType.DEPARTMENT,
                "dept_archived",
                True,
            ),
            _grant(
                "g_sk_4",
                ResourceType.SKILL,
                "sk_dept_role_member",
                GrantTargetType.ROLE,
                UserRole.MEMBER.value,
                False,
            ),
            # McpServer
            _grant(
                "g_mcp_1",
                ResourceType.MCP_SERVER,
                "mcp_dept_fin_sub",
                GrantTargetType.DEPARTMENT,
                "dept_fin",
                True,
            ),
            _grant(
                "g_mcp_2",
                ResourceType.MCP_SERVER,
                "mcp_dept_fin_direct",
                GrantTargetType.DEPARTMENT,
                "dept_fin",
                False,
            ),
            _grant(
                "g_mcp_3",
                ResourceType.MCP_SERVER,
                "mcp_dept_arch_grant",
                GrantTargetType.DEPARTMENT,
                "dept_archived",
                True,
            ),
            _grant(
                "g_mcp_4",
                ResourceType.MCP_SERVER,
                "mcp_dept_role_member",
                GrantTargetType.ROLE,
                UserRole.MEMBER.value,
                False,
            ),
            # Subagent
            _grant(
                "g_sub_1",
                ResourceType.SUBAGENT,
                "sub_dept_fin_sub",
                GrantTargetType.DEPARTMENT,
                "dept_fin",
                True,
            ),
            _grant(
                "g_sub_2",
                ResourceType.SUBAGENT,
                "sub_dept_fin_direct",
                GrantTargetType.DEPARTMENT,
                "dept_fin",
                False,
            ),
            _grant(
                "g_sub_3",
                ResourceType.SUBAGENT,
                "sub_dept_arch_grant",
                GrantTargetType.DEPARTMENT,
                "dept_archived",
                True,
            ),
            _grant(
                "g_sub_4",
                ResourceType.SUBAGENT,
                "sub_dept_role_member",
                GrantTargetType.ROLE,
                UserRole.MEMBER.value,
                False,
            ),
        ]
        session.add_all(grants)


# ---------------------------------------------------------------------------
# 1. 模块 A.1.1：SQL 谓词下推与 domain.access.evaluate_can_read 100% 矩阵等价性
# ---------------------------------------------------------------------------


def test_sql_pushdown_matrix_matches_evaluate_can_read_across_all_four_resources() -> None:
    """验证四类资源表 × 11 类用户（含空集/归档祖先/停用用户）SQL 下推与领域规则完全等价。"""

    async def _scenario() -> None:
        engine, factory = await _create_sqlite_db()
        executed_sql: list[str] = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _capture_sql(
            conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: Any
        ) -> None:
            del conn, cursor, parameters, context, executemany
            executed_sql.append(statement)

        try:
            await _seed_org_and_users(factory)
            await _seed_all_four_resources(factory)
            repo = SqlAlchemyAccessControlRepository(factory)
            policy = AccessPolicyService(repo)

            user_ids = (
                "u_super",
                "u_admin_sub",
                "u_admin_direct",
                "u_admin_empty",
                "u_fin",
                "u_tax",
                "u_rd",
                "u_under_archived",
                "u_no_dept",
                "u_disabled",
                "u_missing",
            )
            active_dept_ids = frozenset(
                {"dept_root", "dept_fin", "dept_tax", "dept_rd", "dept_under_archived"}
            )

            executed_sql.clear()
            for resource_type in ResourceType:
                all_targets = await repo.list_resource_targets(resource_type)
                async with factory() as session:
                    grants_map = await load_grants_map_in_session(
                        session,
                        resource_type,
                        [t.resource_id for t in all_targets],
                    )
                for uid in user_ids:
                    snapshot = await repo.get_user_access_snapshot(uid)
                    expected_ids = {
                        t.resource_id
                        for t in all_targets
                        if evaluate_can_read(
                            snapshot,
                            t,
                            grants_map.get(t.resource_id, ()),
                            active_grant_department_ids=active_dept_ids,
                        )
                    }
                    # 1) filter_readable_targets（含不传 snapshot 与复用 snapshot 两条路径）
                    sql_targets = await policy.filter_readable_targets(uid, resource_type)
                    assert {t.resource_id for t in sql_targets} == expected_ids, (
                        f"mismatch for user={uid} resource_type={resource_type}"
                    )
                    sql_targets_reused = await policy.filter_readable_targets(
                        uid, resource_type, snapshot=snapshot
                    )
                    assert {t.resource_id for t in sql_targets_reused} == expected_ids

                    # 2) 逐资源校验 can_read_resource、can_read_target 与 evaluate_target_access
                    for target in all_targets:
                        expected_read = target.resource_id in expected_ids
                        expected_manage = evaluate_can_manage(
                            snapshot,
                            target.resource_type,
                            target.visibility_scope,
                            owner_id=target.owner_id,
                            owner_department_id=target.owner_department_id,
                        )
                        assert (
                            await policy.can_read_resource(uid, resource_type, target.resource_id)
                            is expected_read
                        )
                        assert await policy.can_read_target(uid, target) is expected_read
                        got_read, got_manage = await policy.evaluate_target_access(uid, target)
                        assert (got_read, got_manage) == (expected_read, expected_manage)

            # 特别验证：归档祖先部门授权不得匹配其活跃子部门员工
            assert (
                await policy.can_read_resource(
                    "u_under_archived", ResourceType.KNOWLEDGE_BASE, "kb_dept_arch_grant"
                )
                is False
            )
            # 无部门成员依然命中 role=member 授权，且绝不触发空集 .in_(())
            assert (
                await policy.can_read_resource(
                    "u_no_dept", ResourceType.KNOWLEDGE_BASE, "kb_dept_role_member"
                )
                is True
            )
            # 超管不可越权读取他人 personal 资产
            assert (
                await policy.can_read_resource(
                    "u_super", ResourceType.KNOWLEDGE_BASE, "kb_pers_fin"
                )
                is False
            )
            # 验证空部门集编译 SQL（PostgreSQL 方言）绝不包含空集 IN ()
            empty_snap = UserAccessSnapshot(
                user_id="u_no_dept",
                role=UserRole.DEPT_ADMIN,
                is_active=True,
                direct_department_ids=frozenset(),
                ancestor_department_ids=frozenset(),
                managed_department_ids=frozenset(),
            )
            clause = build_readable_resource_clause(
                empty_snap,
                ResourceType.KNOWLEDGE_BASE,
                id_col=KnowledgeBaseModel.id,
                scope_col=KnowledgeBaseModel.visibility_scope,
                owner_id_col=KnowledgeBaseModel.owner_id,
                owner_department_id_col=KnowledgeBaseModel.owner_department_id,
            )
            compiled = str(select(KnowledgeBaseModel.id).where(clause).compile(dialect=_PG_DIALECT))
            assert "IN ()" not in compiled
            assert "1 != 1" not in compiled
            assert all("IN ()" not in sql for sql in executed_sql)
        finally:
            await engine.dispose()

    _run(_scenario())


def test_access_policy_and_manageable_base_session_counts() -> None:
    """验证 AccessPolicyService 单 Session 收敛以及 _require_manageable_base 合并读写双校验。"""

    async def _scenario() -> None:
        engine, raw_factory = await _create_sqlite_db()
        try:
            await _seed_org_and_users(raw_factory)
            await _seed_all_four_resources(raw_factory)
            counting_factory = _CountingSessionFactory(raw_factory)
            access_repo = SqlAlchemyAccessControlRepository(cast(Any, counting_factory))
            policy = AccessPolicyService(access_repo)
            kb_repo = KnowledgeRepository(cast(Any, counting_factory))
            kb_service = KnowledgeService(
                cast(KnowledgeRepositoryPort, kb_repo),
                _SimpleTokenizer(),
                access_policy=policy,
            )

            # 1) filter_readable_targets：4 Session -> 1 Session
            counting_factory.reset()
            targets = await policy.filter_readable_targets("u_tax", ResourceType.KNOWLEDGE_BASE)
            assert counting_factory.count == 1
            assert "kb_dept_fin_sub" in {t.resource_id for t in targets}

            # 2) can_read_resource：4 Session -> 1 Session
            counting_factory.reset()
            assert (
                await policy.can_read_resource(
                    "u_tax", ResourceType.KNOWLEDGE_BASE, "kb_dept_fin_sub"
                )
                is True
            )
            assert counting_factory.count == 1

            # 3) can_read_target：未传快照 1 Session；传入快照且命中快速路径时 0 Session
            co_target = ResourceAccessTarget(
                resource_type=ResourceType.KNOWLEDGE_BASE,
                resource_id="kb_co",
                visibility_scope=VisibilityScope.COMPANY,
                owner_id="u_super",
                owner_department_id=None,
            )
            counting_factory.reset()
            assert await policy.can_read_target("u_tax", co_target) is True
            assert counting_factory.count == 1

            snap_tax = await policy.get_user_snapshot("u_tax")
            counting_factory.reset()
            assert await policy.can_read_target("u_tax", co_target, snapshot=snap_tax) is True
            assert counting_factory.count == 0

            # 4) _require_manageable_base：2 Session（读 KB + 单次 evaluate_target_access）
            counting_factory.reset()
            managed = await kb_service._require_manageable_base(
                "u_admin_sub", "kb_dept_tax_nogrant"
            )
            assert managed.id == "kb_dept_tax_nogrant"
            assert counting_factory.count == 2

            # 可读但无管理权 -> 403（KnowledgePermissionDeniedError）
            counting_factory.reset()
            with pytest.raises(KnowledgePermissionDeniedError):
                await kb_service._require_manageable_base("u_fin", "kb_dept_fin_sub")
            assert counting_factory.count == 2

            # 不可读 -> 404（KnowledgeNotFoundError）防探测
            counting_factory.reset()
            with pytest.raises(KnowledgeNotFoundError):
                await kb_service._require_manageable_base("u_rd", "kb_dept_fin_sub")
            assert counting_factory.count == 2
        finally:
            await engine.dispose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 2. 模块 A.1.3：四大高频入口单 Session 下推、MCP limit=201 修复与实时调岗回收
# ---------------------------------------------------------------------------


def test_high_frequency_list_endpoints_single_session_pushdown() -> None:
    """验证 GET /knowledge-bases、GET /skills 与 GET /tools 均压至单 Session SQL 下推。"""

    async def _scenario() -> None:
        engine, raw_factory = await _create_sqlite_db()
        try:
            users = await _seed_org_and_users(raw_factory)
            await _seed_all_four_resources(raw_factory)
            counting_factory = _CountingSessionFactory(raw_factory)
            cipher = McpSecretCipher(SECRET_KEY)

            access_policy = AccessPolicyService(
                SqlAlchemyAccessControlRepository(cast(Any, counting_factory))
            )
            kb_service = KnowledgeService(
                cast(KnowledgeRepositoryPort, KnowledgeRepository(cast(Any, counting_factory))),
                _SimpleTokenizer(),
                access_policy=access_policy,
            )
            skill_service = SkillService(
                SqlAlchemySkillRepository(cast(Any, counting_factory)),
                cast(Any, _NoopFileStore()),
                _StubAuthReader(users),
                access_policy=access_policy,
            )
            tool_manager = ToolManager(
                SqlAlchemyMcpRepository(cast(Any, counting_factory), cipher),
                cast(Any, _NoopMcpDiscovery()),
                _StubAuthReader(users),
                access_policy=access_policy,
            )

            # 1) GET /api/v1/knowledge-bases：5 Session -> 1 Session（含分页与筛选）
            counting_factory.reset()
            kbs = await kb_service.list_bases("u_fin", page=1, page_size=20)
            assert counting_factory.count == 1
            assert {b.id for b in kbs} == {
                "kb_co",
                "kb_pers_fin",
                "kb_dept_fin_sub",
                "kb_dept_fin_direct",
                "kb_dept_role_member",
            }
            # 带 grants 的部门库已一并加载授权
            fin_sub = next(b for b in kbs if b.id == "kb_dept_fin_sub")
            assert len(fin_sub.visibility_grants) == 1

            counting_factory.reset()
            dept_only = await kb_service.list_bases(
                "u_fin",
                page=1,
                page_size=2,
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_department_id="dept_fin",
            )
            assert counting_factory.count == 1
            assert {b.id for b in dept_only} == {"kb_dept_fin_sub", "kb_dept_fin_direct"}

            # 2) GET /api/v1/skills：5 Session -> 1 Session
            fin_actor = AuthContext(user=users["u_fin"], session_id="auth_fin")
            counting_factory.reset()
            skills = await skill_service.user_directory(fin_actor)
            assert counting_factory.count == 1
            assert {s.id for s in skills} == {
                "sk_co",
                "sk_pers_fin",
                "sk_dept_fin_sub",
                "sk_dept_fin_direct",
                "sk_dept_role_member",
            }

            # 3) GET /api/v1/tools：5 Session -> 1 Session（目录查库 1 次）
            counting_factory.reset()
            tool_page = await tool_manager.list_public_tools(fin_actor, offset=0, limit=20)
            assert counting_factory.count == 1
            assert tool_page.total == 4
            assert len(tool_page.items) == 4
        finally:
            await engine.dispose()

    _run(_scenario())


def test_tool_runtime_build_snapshot_reuses_snapshot_and_fixes_mcp_limit_201() -> None:
    """验证 build_snapshot 仅加载 1 次权限快照、压缩 Session 且无权 MCP 不占 limit=201。"""

    async def _scenario() -> None:
        engine, raw_factory = await _create_sqlite_db()
        try:
            users = await _seed_org_and_users(raw_factory)
            await _seed_all_four_resources(raw_factory)

            # 额外写入 205 个字典序靠前但归属研发部（u_fin 无权）的 MCP 工具，
            # 确认 SQL 下推后不会把 u_fin 有权的 201 额度挤掉。
            async with raw_factory() as session, session.begin():
                session.add(
                    McpServerModel(
                        id="mcp_000_rd_flood",
                        name="rd-flood-server",
                        transport="streamable_http",
                        config_json={
                            "transport": "streamable_http",
                            "url": "https://flood.test",
                        },
                        config_version=1,
                        enabled=True,
                        visibility_scope=VisibilityScope.DEPARTMENT.value,
                        owner_department_id="dept_rd",
                        created_by="u_super",
                        deleted_at=None,
                        created_at=NOW,
                        updated_at=NOW,
                        tools_discovered_at=NOW,
                    )
                )
                await session.flush()
                for idx in range(205):
                    session.add(
                        McpToolModel(
                            id=f"tool_flood_{idx:03d}",
                            server_id="mcp_000_rd_flood",
                            remote_name=f"flood_{idx:03d}",
                            model_name=f"mcp__flood_{idx:03d}",
                            description="unauthorized rd tool",
                            input_schema_json={"type": "object", "properties": {}},
                            requires_approval=False,
                            created_at=NOW,
                        )
                    )

            counting_factory = _CountingSessionFactory(raw_factory)
            cipher = McpSecretCipher(SECRET_KEY)
            access_policy = AccessPolicyService(
                SqlAlchemyAccessControlRepository(cast(Any, counting_factory))
            )
            kb_service = KnowledgeService(
                cast(KnowledgeRepositoryPort, KnowledgeRepository(cast(Any, counting_factory))),
                _SimpleTokenizer(),
                access_policy=access_policy,
            )
            runtime = RunToolRuntimeService(
                auth=_StubAuthReader(users),
                sessions=SqlAlchemySessionRepository(cast(Any, counting_factory)),
                mcp_catalog=SqlAlchemyMcpRepository(cast(Any, counting_factory), cipher),
                mcp_executor=_NoopMcpExecutor(),
                knowledge=kb_service,
                project_knowledge=None,
                subagents=SqlAlchemySubagentRepository(cast(Any, counting_factory)),
                access_policy=access_policy,
            )

            ctx_fin = AuthorizedRunContext(
                owner_id="u_fin",
                chat_session_id="sess_1",
                project_id=None,
                auth_session_id="auth_fin",
            )
            counting_factory.reset()
            snapshot = await runtime.build_snapshot("run-v2", ctx_fin, depth=0)

            # 1 次 get_user_snapshot + 1 次 KB + 1 次 MCP + 1 次 Subagent = 4 Sessions
            assert counting_factory.count == 4
            mcp_server_ids = {
                t.source.server_id for t in snapshot.tools if isinstance(t.source, McpToolSource)
            }
            assert "mcp_000_rd_flood" not in mcp_server_ids
            assert mcp_server_ids == {
                "mcp_co",
                "mcp_dept_fin_sub",
                "mcp_dept_fin_direct",
                "mcp_dept_role_member",
            }
            assert any(t.model_name == "knowledge_search" for t in snapshot.tools)
            dispatch_tool = next(t for t in snapshot.tools if t.model_name == "dispatch_subagents")
            assert "sub_co" in dispatch_tool.description
            assert "sub_sub" in dispatch_tool.description
            assert "sub_dir" in dispatch_tool.description
            assert "sub_arch" not in dispatch_tool.description

            # 停用用户仅需 1 次快照读取即短路跳过全部 KB/MCP/Subagent 查询。
            ctx_disabled = AuthorizedRunContext(
                owner_id="u_disabled",
                chat_session_id="sess_dis",
                project_id=None,
                auth_session_id="auth_dis",
            )
            counting_factory.reset()
            disabled_snap = await runtime.build_snapshot("run-v2", ctx_disabled, depth=0)
            assert counting_factory.count == 1
            assert tuple(t.model_name for t in disabled_snap.tools) == ("request_user_input",)
        finally:
            await engine.dispose()

    _run(_scenario())


def test_knowledge_search_skip_access_check_and_mid_search_transfer_revocation() -> None:
    """验证 skip_access_check、跳过冗余 validate_search_scope，以及检索中途调岗仍实时撤权。"""

    async def _scenario() -> None:
        engine, raw_factory = await _create_sqlite_db()
        try:
            users = await _seed_org_and_users(raw_factory)
            await _seed_all_four_resources(raw_factory)
            async with raw_factory() as session, session.begin():
                session.add(
                    SessionModel(
                        id="sess_fin",
                        owner_id="u_fin",
                        title="财务对话",
                        project_id=None,
                        last_message_sequence=0,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

            class _SpyKnowledgeRepo:
                def __init__(self) -> None:
                    self.validate_calls = 0
                    self.on_search: Any = None

                async def validate_search_scope(self, owner_id: str, base_ids: list[str]) -> None:
                    del owner_id, base_ids
                    self.validate_calls += 1

                async def search_keyword(
                    self,
                    owner_id: str,
                    base_ids: list[str],
                    tokens: tuple[str, ...],
                    limit: int,
                ) -> tuple[SearchHit, ...]:
                    del owner_id, tokens, limit
                    if self.on_search is not None:
                        hook = self.on_search
                        self.on_search = None
                        await hook()
                    return (
                        SearchHit(
                            chunk_id="chk_1",
                            document_id="doc_1",
                            version_id="ver_1",
                            title="财务制度",
                            text="差旅报销标准",
                            locators=(ChunkLocator(kind="text"),),
                            channels=("keyword",),
                            rank=1.0,
                            chunk_ordinal=0,
                            chunk_ids=("chk_1",),
                            knowledge_base_id=base_ids[0],
                        ),
                    )

                async def search_semantic(
                    self,
                    owner_id: str,
                    base_ids: list[str],
                    vector: list[float],
                    limit: int,
                    profile_id: str | None = None,
                ) -> tuple[SearchHit, ...]:
                    del owner_id, base_ids, vector, limit, profile_id
                    return ()

            counting_factory = _CountingSessionFactory(raw_factory)
            access_policy = AccessPolicyService(
                SqlAlchemyAccessControlRepository(cast(Any, counting_factory))
            )
            spy_repo = _SpyKnowledgeRepo()
            kb_service = KnowledgeService(
                cast(KnowledgeRepositoryPort, spy_repo),
                _SimpleTokenizer(),
                access_policy=access_policy,
            )

            # 1) HTTP search（access_policy 非空且 skip_access_check=False）：
            #    走 1 次 filter_readable_ids，跳过冗余 validate_search_scope。
            counting_factory.reset()
            res = await kb_service.search("u_fin", ["kb_dept_fin_sub"], "报销")
            assert len(res.hits) == 1
            assert counting_factory.count == 1
            assert spy_repo.validate_calls == 0

            # 2) skip_access_check=True：不再触发内部 filter_readable_ids（0 次权限 Session）
            counting_factory.reset()
            res_skip = await kb_service.search(
                "u_fin", ["kb_dept_fin_sub"], "报销", skip_access_check=True
            )
            assert len(res_skip.hits) == 1
            assert counting_factory.count == 0
            assert spy_repo.validate_calls == 0

            # 3) access_policy=None 旧路径：仍保留 validate_search_scope 调用
            legacy_kb = KnowledgeService(
                cast(KnowledgeRepositoryPort, spy_repo),
                _SimpleTokenizer(),
                access_policy=None,
            )
            await legacy_kb.search("u_fin", ["kb_dept_fin_sub"], "报销")
            assert spy_repo.validate_calls == 1

            # 4) Agent knowledge_search 强制安全契约：
            #    before_scope 通过后、检索中途将 u_fin 调岗至 dept_rd，
            #    after_scope 必须独立实时重读数据库并丢弃越权结果！
            async def _transfer_mid_search() -> None:
                async with raw_factory() as s, s.begin():
                    await s.execute(
                        delete(UserDepartmentModel).where(UserDepartmentModel.user_id == "u_fin")
                    )
                    s.add(
                        UserDepartmentModel(
                            user_id="u_fin",
                            department_id="dept_rd",
                            is_primary=True,
                        )
                    )

            spy_repo.on_search = _transfer_mid_search
            runtime = RunToolRuntimeService(
                auth=_StubAuthReader(users),
                sessions=SqlAlchemySessionRepository(raw_factory),
                mcp_catalog=SqlAlchemyMcpRepository(raw_factory, McpSecretCipher(SECRET_KEY)),
                mcp_executor=_NoopMcpExecutor(),
                knowledge=kb_service,
                project_knowledge=None,
                access_policy=access_policy,
            )
            exec_ctx = ToolExecutionContext(
                owner_id="u_fin",
                chat_session_id="sess_fin",
                project_id=None,
                run_id="run_fin_1",
                execution_epoch=1,
                auth_session_id="auth_fin",
                deadline_at=NOW + timedelta(seconds=30),
                knowledge_base_ids=("kb_dept_fin_sub",),
            )
            outcome = await runtime.execute(
                KNOWLEDGE_SEARCH_DEFINITION,
                "knowledge_search",
                {"query": "报销", "limit": 5},
                exec_ctx,
                deadline_seconds=5.0,
                run_remaining_seconds=30.0,
            )
            assert outcome.outcome.status is ToolOutcomeStatus.ERROR
            assert outcome.outcome.code == "knowledge_scope_unavailable"
            assert "items" not in outcome.outcome.data
        finally:
            await engine.dispose()

    _run(_scenario())


def test_run_submit_and_retry_reuse_single_access_snapshot_in_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 RunLifecycleMixin.submit/retry 同事务内仅加载 1 次用户权限快照并严格拦截越权资源。"""

    async def _scenario() -> None:
        engine, factory = await _create_sqlite_db(monkeypatch)
        executed_sql: list[str] = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _capture_sql(
            conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: Any
        ) -> None:
            del conn, cursor, parameters, context, executemany
            executed_sql.append(statement)

        try:
            await _seed_org_and_users(factory)
            await _seed_all_four_resources(factory)
            cipher = McpSecretCipher(SECRET_KEY)
            sealed = cipher.seal("prof_1", McpSecretPurpose.LLM_API_KEY, {"api_key": "sk-test"})
            async with factory() as session, session.begin():
                session.add(
                    ModelProfileVersionModel(
                        id="prof_1",
                        name="默认模型",
                        protocol="chat_completions",
                        base_url="https://llm.test/v1",
                        model="gpt-4o",
                        api_key_ciphertext=sealed.ciphertext,
                        api_key_nonce=sealed.nonce,
                        api_key_key_version=sealed.key_version,
                        context_window_tokens=16384,
                        tokenizer_encoding="cl100k_base",
                        supports_stream=True,
                        supports_tools=True,
                        supports_parallel_tool_calls=True,
                        max_output_tokens=1024,
                        request_timeout_seconds=60,
                        execution_timeout_seconds=300,
                        max_concurrency=4,
                        temperature=0.2,
                        enabled=True,
                        visible_to_users=True,
                        version=1,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                session.add(DefaultModelProfileModel(singleton_id=1, profile_id="prof_1"))
                session.add_all(
                    [
                        SystemPromptVersionModel(
                            id="sp_main",
                            kind="main",
                            content="main",
                            created_at=NOW,
                        ),
                        SystemPromptVersionModel(
                            id="sp_comp",
                            kind="compression",
                            content="comp",
                            created_at=NOW,
                        ),
                    ]
                )
                session.add_all(
                    [
                        SystemPromptSettingModel(
                            kind="main",
                            current_version_id="sp_main",
                        ),
                        SystemPromptSettingModel(
                            kind="compression",
                            current_version_id="sp_comp",
                        ),
                    ]
                )
                session.add(
                    SessionModel(
                        id="sess_submit",
                        owner_id="u_fin",
                        title="提交会话",
                        project_id=None,
                        last_message_sequence=0,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

            run_repo = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))

            # 1) 越权显式知识库必须被拦截
            with pytest.raises(RunKnowledgeBaseNotFoundError):
                await run_repo.submit(
                    "sess_submit",
                    "u_fin",
                    SubmitRunCommand(
                        run_id="run_bad_kb",
                        request_id="req_bad_kb",
                        fingerprint="fp_bad_kb",
                        prompt_template_version="run-v2",
                        message=ConversationMessageDraft(
                            id="msg_bad_kb",
                            session_id="sess_submit",
                            owner_id="u_fin",
                            role=ConversationRole.USER,
                            status=MessageStatus.COMPLETED,
                            parts=(TextPart(text="hello"),),
                            reply_to_message_id=None,
                            run_id="run_bad_kb",
                            client_message_id=None,
                            created_at=NOW,
                            completed_at=NOW,
                            metadata=MessageMetadata(),
                        ),
                        queued_at=NOW,
                        selected_skill_ids=("sk_dept_fin_sub",),
                        knowledge_base_ids=("kb_dept_nogrant",),
                    ),
                )

            # 2) 越权技能必须被拦截
            with pytest.raises(SkillConflictError):
                await run_repo.submit(
                    "sess_submit",
                    "u_fin",
                    SubmitRunCommand(
                        run_id="run_bad_sk",
                        request_id="req_bad_sk",
                        fingerprint="fp_bad_sk",
                        prompt_template_version="run-v2",
                        message=ConversationMessageDraft(
                            id="msg_bad_sk",
                            session_id="sess_submit",
                            owner_id="u_fin",
                            role=ConversationRole.USER,
                            status=MessageStatus.COMPLETED,
                            parts=(TextPart(text="hello"),),
                            reply_to_message_id=None,
                            run_id="run_bad_sk",
                            client_message_id=None,
                            created_at=NOW,
                            completed_at=NOW,
                            metadata=MessageMetadata(),
                        ),
                        queued_at=NOW,
                        selected_skill_ids=("sk_dept_nogrant",),
                        knowledge_base_ids=("kb_dept_fin_sub",),
                    ),
                )

            # 3) 合法 submit：同时校验 Skill 快照与显式 KB，users 权限查询只执行 1 次
            executed_sql.clear()
            submitted = await run_repo.submit(
                "sess_submit",
                "u_fin",
                SubmitRunCommand(
                    run_id="run_ok_1",
                    request_id="req_ok_1",
                    fingerprint="fp_ok_1",
                    prompt_template_version="run-v2",
                    message=ConversationMessageDraft(
                        id="msg_ok_1",
                        session_id="sess_submit",
                        owner_id="u_fin",
                        role=ConversationRole.USER,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart(text="hello"),),
                        reply_to_message_id=None,
                        run_id="run_ok_1",
                        client_message_id=None,
                        created_at=NOW,
                        completed_at=NOW,
                        metadata=MessageMetadata(),
                    ),
                    queued_at=NOW,
                    selected_skill_ids=("sk_dept_fin_sub",),
                    knowledge_base_ids=("kb_dept_fin_sub", "kb_co"),
                ),
            )
            assert submitted.run.id == "run_ok_1"
            assert submitted.run.knowledge_base_ids == ("kb_dept_fin_sub", "kb_co")
            user_selects = [s for s in executed_sql if "FROM users" in s]
            assert len(user_selects) == 1

            # 4) 合法 retry：先将 run_ok_1 置为 failed，验证 retry 同样仅查 1 次 users 权限快照
            async with factory() as session, session.begin():
                row = await session.get(RunModel, "run_ok_1")
                assert row is not None
                row.status = RunStatus.FAILED.value
                row.finished_at = NOW
            executed_sql.clear()
            retried = await run_repo.retry(
                session_id="sess_submit",
                owner_id="u_fin",
                source_run_id="run_ok_1",
                request_id="req_retry_1",
                fingerprint="fp_retry_1",
                new_run_id="run_ok_2",
                prompt_template_version="run-v2",
                now=NOW + timedelta(seconds=5),
            )
            assert retried is not None
            assert retried.id == "run_ok_2"
            retry_user_selects = [s for s in executed_sql if "FROM users" in s]
            assert len(retry_user_selects) == 1
        finally:
            await engine.dispose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 3. 模块 A.2.1：SqlAlchemyAuthRepository.get_active_session 单条 JOIN 验证
# ---------------------------------------------------------------------------


def test_auth_get_active_session_executes_single_join_query() -> None:
    """验证 get_active_session 合并为单条 JOIN 查询并保持有效/过期/撤销/禁用/删除语义一致。"""

    async def _scenario() -> None:
        engine, factory = await _create_sqlite_db()
        executed_sql: list[str] = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _capture_sql(
            conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: Any
        ) -> None:
            del conn, cursor, parameters, context, executemany
            executed_sql.append(statement)

        try:
            async with factory() as session, session.begin():
                session.add_all(
                    [
                        UserModel(
                            id="u_active",
                            email="a@test.com",
                            password_hash="h",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.MEMBER.value,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        UserModel(
                            id="u_disabled",
                            email="d@test.com",
                            password_hash="h",
                            status=UserStatus.DISABLED.value,
                            role=UserRole.MEMBER.value,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        UserModel(
                            id="u_deleted",
                            email="del@test.com",
                            password_hash="h",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.MEMBER.value,
                            created_at=NOW,
                            updated_at=NOW,
                            deleted_time=NOW,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        AuthSessionModel(
                            id="ses_ok",
                            user_id="u_active",
                            token_hash="t_ok",
                            expires_at=NOW + timedelta(hours=1),
                            revoked_time=None,
                            created_at=NOW,
                        ),
                        AuthSessionModel(
                            id="ses_expired",
                            user_id="u_active",
                            token_hash="t_expired",
                            expires_at=NOW,
                            revoked_time=None,
                            created_at=NOW,
                        ),
                        AuthSessionModel(
                            id="ses_revoked",
                            user_id="u_active",
                            token_hash="t_revoked",
                            expires_at=NOW + timedelta(hours=1),
                            revoked_time=NOW,
                            created_at=NOW,
                        ),
                        AuthSessionModel(
                            id="ses_disabled",
                            user_id="u_disabled",
                            token_hash="t_disabled",
                            expires_at=NOW + timedelta(hours=1),
                            revoked_time=None,
                            created_at=NOW,
                        ),
                        AuthSessionModel(
                            id="ses_deleted",
                            user_id="u_deleted",
                            token_hash="t_deleted",
                            expires_at=NOW + timedelta(hours=1),
                            revoked_time=None,
                            created_at=NOW,
                        ),
                    ]
                )

            repo = SqlAlchemyAuthRepository(factory)
            executed_sql.clear()
            ctx = await repo.get_active_session("t_ok", NOW)
            assert ctx is not None
            assert ctx.user.id == "u_active"
            assert ctx.session_id == "ses_ok"
            assert len(executed_sql) == 1
            assert "JOIN auth_sessions" in executed_sql[0]

            assert await repo.get_active_session("t_expired", NOW) is None
            assert await repo.get_active_session("t_revoked", NOW) is None
            assert await repo.get_active_session("t_disabled", NOW) is None
            assert await repo.get_active_session("t_deleted", NOW) is None
            assert await repo.get_active_session("t_missing", NOW) is None
        finally:
            await engine.dispose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 4. 模块 A.2.2：RunService.get() / events() 单会话聚合与内存替身回退
# ---------------------------------------------------------------------------


def test_run_service_get_details_and_list_visible_events_single_session_and_fallback() -> None:
    """验证 RunService.get()/events() 单 Session 合并、与旧接口等价、子 Run 问答保留及回退。"""

    async def _scenario() -> None:
        engine, raw_factory = await _create_sqlite_db()
        executed_sql: list[str] = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _capture_sql(
            conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: Any
        ) -> None:
            del conn, cursor, parameters, context, executemany
            executed_sql.append(statement)

        try:
            async with raw_factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="u_owner",
                        email="owner@test.com",
                        password_hash="h",
                        status=UserStatus.ACTIVE.value,
                        role=UserRole.MEMBER.value,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        SessionModel(
                            id="s_plain",
                            owner_id="u_owner",
                            title="单任务会话",
                            project_id=None,
                            last_message_sequence=10,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        SessionModel(
                            id="s_main",
                            owner_id="u_owner",
                            title="主会话",
                            project_id=None,
                            last_message_sequence=10,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        SessionModel(
                            id="s_deleted",
                            owner_id="u_owner",
                            title="已删会话",
                            project_id=None,
                            last_message_sequence=1,
                            created_at=NOW,
                            updated_at=NOW,
                            deleted_time=NOW,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        ConversationMessageModel(
                            id="msg_trig_plain",
                            session_id="s_plain",
                            role=ConversationRole.USER.value,
                            status=MessageStatus.COMPLETED.value,
                            sequence=1,
                            parts=[{"type": "text", "text": "单任务"}],
                            created_at=NOW,
                            completed_at=NOW,
                            metadata_json={},
                        ),
                        ConversationMessageModel(
                            id="msg_q_root",
                            session_id="s_plain",
                            role=ConversationRole.ASSISTANT.value,
                            status=MessageStatus.COMPLETED.value,
                            sequence=2,
                            parts=[{"type": "text", "text": "主 Run 请确认参数？"}],
                            created_at=NOW,
                            completed_at=NOW,
                            metadata_json={},
                        ),
                        ConversationMessageModel(
                            id="msg_trig",
                            session_id="s_main",
                            role=ConversationRole.USER.value,
                            status=MessageStatus.COMPLETED.value,
                            sequence=1,
                            parts=[{"type": "text", "text": "主任务"}],
                            created_at=NOW,
                            completed_at=NOW,
                            metadata_json={},
                        ),
                        ConversationMessageModel(
                            id="msg_q_sub",
                            session_id="s_main",
                            role=ConversationRole.ASSISTANT.value,
                            status=MessageStatus.COMPLETED.value,
                            sequence=3,
                            parts=[{"type": "text", "text": "子任务需要哪一年度？"}],
                            created_at=NOW,
                            completed_at=NOW,
                            metadata_json={},
                        ),
                    ]
                )
                await session.flush()

                def _make_run_model(
                    run_id: str,
                    *,
                    session_id: str = "s_main",
                    trigger_message_id: str = "msg_trig",
                    status: RunStatus = RunStatus.RUNNING,
                    depth: int = 0,
                    parent_run_id: str | None = None,
                    parent_tool_call_id: str | None = None,
                    subagent_slug: str | None = None,
                    subagent_name: str | None = None,
                    created_offset: int = 0,
                ) -> RunModel:
                    return RunModel(
                        id=run_id,
                        session_id=session_id,
                        owner_id="u_owner",
                        status=status.value,
                        trigger_message_id=trigger_message_id,
                        context_cutoff_sequence=1,
                        retry_of_run_id=None,
                        model_config_version_id="prof_1",
                        main_prompt_version_id="sp_main",
                        compression_prompt_version_id="sp_comp",
                        prompt_template_version="run-v2",
                        event_sequence=2,
                        execution_epoch=1,
                        queued_at=NOW + timedelta(seconds=created_offset),
                        created_at=NOW + timedelta(seconds=created_offset),
                        updated_at=NOW + timedelta(seconds=created_offset),
                        usage=RunUsage().model_dump(mode="json"),
                        depth=depth,
                        parent_run_id=parent_run_id,
                        root_run_id=(parent_run_id or run_id),
                        parent_tool_call_id=parent_tool_call_id,
                        subagent_slug=subagent_slug,
                        subagent_name=subagent_name,
                    )

                session.add_all(
                    [
                        # 1) 无子任务的普通主 Run（含 pending input）
                        _make_run_model(
                            "run_plain",
                            session_id="s_plain",
                            trigger_message_id="msg_trig_plain",
                            status=RunStatus.WAITING_FOR_INPUT,
                        ),
                        # 2) 带子任务的主 Run
                        _make_run_model(
                            "run_parent",
                            status=RunStatus.WAITING_FOR_SUBAGENTS,
                            created_offset=1,
                        ),
                        # 3) 已删会话中的 Run（不可见）
                        _make_run_model("run_hidden", session_id="s_deleted", created_offset=2),
                    ]
                )
                await session.flush()
                # 两个子 Run：一个待回答输入，一个待审批工具调用
                session.add_all(
                    [
                        _make_run_model(
                            "run_sub_input",
                            status=RunStatus.WAITING_FOR_INPUT,
                            depth=1,
                            parent_run_id="run_parent",
                            parent_tool_call_id="tc_dispatch_1",
                            subagent_slug="fin_agent",
                            subagent_name="财务专员",
                            created_offset=3,
                        ),
                        _make_run_model(
                            "run_sub_approval",
                            status=RunStatus.WAITING_FOR_APPROVAL,
                            depth=1,
                            parent_run_id="run_parent",
                            parent_tool_call_id="tc_dispatch_1",
                            subagent_slug="ops_agent",
                            subagent_name="运维专员",
                            created_offset=4,
                        ),
                    ]
                )
                await session.flush()
                # 初始任务执行消息与审批调用消息
                session.add_all(
                    [
                        ExecutionMessageModel(
                            id="exec_sub_1",
                            session_id="s_main",
                            run_id="run_sub_input",
                            thread_id="run_sub_input",
                            role=ExecutionRole.USER.value,
                            sequence=4,
                            parts=[{"type": "text", "text": "任务：\n核对 Q4 预算"}],
                            created_at=NOW,
                            metadata_json={},
                        ),
                        ExecutionMessageModel(
                            id="exec_sub_2",
                            session_id="s_main",
                            run_id="run_sub_approval",
                            thread_id="run_sub_approval",
                            role=ExecutionRole.USER.value,
                            sequence=5,
                            parts=[{"type": "text", "text": "任务：\n重启测试网关"}],
                            created_at=NOW,
                            metadata_json={},
                        ),
                        ExecutionMessageModel(
                            id="exec_sub_call",
                            session_id="s_main",
                            run_id="run_sub_approval",
                            thread_id="run_sub_approval",
                            role=ExecutionRole.ASSISTANT.value,
                            sequence=6,
                            parts=[
                                {
                                    "type": "tool_call",
                                    "tool_call_id": "call_sub_1",
                                    "name": "mcp__restart_gateway",
                                    "arguments": {"env": "staging"},
                                }
                            ],
                            created_at=NOW,
                            metadata_json={},
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        RunInputRequestModel(
                            id="req_plain",
                            run_id="run_plain",
                            session_id="s_plain",
                            question_message_id="msg_q_root",
                            status=InputRequestStatus.PENDING.value,
                            created_at=NOW,
                        ),
                        RunInputRequestModel(
                            id="req_sub",
                            run_id="run_sub_input",
                            session_id="s_main",
                            question_message_id="msg_q_sub",
                            status=InputRequestStatus.PENDING.value,
                            created_at=NOW,
                        ),
                        RunToolCallModel(
                            id="tc_sub_rec",
                            run_id="run_sub_approval",
                            session_id="s_main",
                            tool_call_id="call_sub_1",
                            ordinal=1,
                            tool_id="tool_sub_1",
                            tool_kind=ToolKind.MCP.value,
                            model_name="mcp__restart_gateway",
                            execution_epoch=1,
                            round=1,
                            status=ToolCallStatus.PENDING_APPROVAL.value,
                            call_message_id="exec_sub_call",
                            requires_approval=True,
                            requested_at=NOW,
                        ),
                        RunEventModel(
                            id="evt_1",
                            run_id="run_plain",
                            session_id="s_plain",
                            sequence=1,
                            type="queued",
                            created_at=NOW,
                            payload={"type": "queued", "trigger_message_id": "msg_trig_plain"},
                        ),
                        RunEventModel(
                            id="evt_2",
                            run_id="run_plain",
                            session_id="s_plain",
                            sequence=2,
                            type="waiting_for_input",
                            created_at=NOW + timedelta(seconds=1),
                            payload={
                                "type": "waiting_for_input",
                                "input_request_id": "req_plain",
                                "question_message_id": "msg_q_root",
                            },
                        ),
                    ]
                )

            counting_factory = _CountingSessionFactory(raw_factory)
            repo = SqlAlchemyRunRepository(
                cast(Any, counting_factory),
                SqlAlchemyMessageRepository(cast(Any, counting_factory)),
            )
            service = RunService(repo)

            # A.1 无子任务的主 Run：1 Session，且顺带提取 pending_input、跳过子任务输入/审批查询
            counting_factory.reset()
            executed_sql.clear()
            plain = await service.get("s_plain", "u_owner", "run_plain")
            assert counting_factory.count == 1
            assert plain.run.id == "run_plain"
            assert plain.pending_input is not None and plain.pending_input.id == "req_plain"
            assert len(plain.run.input_interactions) == 1
            assert plain.subagent_tasks == ()
            # 确认未发多余的 execution_messages 子任务摘要查询
            assert not any("FROM execution_messages" in s for s in executed_sql)

            # A.2 带子任务的主 Run：1 Session，且与旧 6 个独立仓储方法逐字段完全一致
            counting_factory.reset()
            parent = await service.get("s_main", "u_owner", "run_parent")
            assert counting_factory.count == 1
            assert parent.run == await repo.get("s_main", "u_owner", "run_parent")
            assert parent.pending_input == await repo.get_pending_input(
                "s_main", "u_owner", "run_parent"
            )
            assert parent.pending_approvals == await repo.list_pending_approvals(
                "s_main", "u_owner", "run_parent"
            )
            assert parent.subagent_tasks == await repo.list_subagent_tasks(
                "s_main", "u_owner", "run_parent"
            )
            assert parent.pending_subagent_inputs == await repo.list_pending_subagent_inputs(
                "s_main", "u_owner", "run_parent"
            )
            assert parent.pending_subagent_approvals == await repo.list_pending_subagent_approvals(
                "s_main", "u_owner", "run_parent"
            )
            assert len(parent.subagent_tasks) == 2
            assert len(parent.pending_subagent_inputs) == 1
            assert len(parent.pending_subagent_approvals) == 1

            # A.3 子 Run（depth == 1）：1 Session，完整保留 input_interactions
            counting_factory.reset()
            child = await service.get("s_main", "u_owner", "run_sub_input")
            assert counting_factory.count == 1
            assert len(child.run.input_interactions) == 1
            assert child.run.input_interactions[0].question_text == "子任务需要哪一年度？"
            assert child.pending_input is not None and child.pending_input.id == "req_sub"
            assert child.subagent_tasks == ()

            # A.4 不可见 Run（已删会话 / 他人访问）：1 Session 返回 RunNotFoundError
            counting_factory.reset()
            with pytest.raises(RunNotFoundError):
                await service.get("s_deleted", "u_owner", "run_hidden")
            assert counting_factory.count == 1

            # A.5 RunService.events()：1 Session 完成存在性校验与事件读取
            counting_factory.reset()
            evts = await service.events(
                "s_plain", "u_owner", "run_plain", after_sequence=0, limit=10
            )
            assert counting_factory.count == 1
            assert [e.sequence for e in evts] == [1, 2]

            counting_factory.reset()
            with pytest.raises(RunNotFoundError):
                await service.events(
                    "s_deleted", "u_owner", "run_hidden", after_sequence=0, limit=10
                )
            assert counting_factory.count == 1

            # B. 内存旧替身回退验证：events() 只调轻量 get()，绝不调重型 pending/subagent 接口
            class _LegacyRunRepo:
                def __init__(self) -> None:
                    self.calls: list[str] = []
                    self.run = Run(
                        id="r_legacy",
                        session_id="s_1",
                        owner_id="u_1",
                        status=RunStatus.QUEUED,
                        trigger_message_id="m_1",
                        context_cutoff_sequence=1,
                        retry_of_run_id=None,
                        model_config_version_id="prof_1",
                        main_prompt_version_id="sp_main",
                        compression_prompt_version_id="sp_comp",
                        prompt_template_version="run-v2",
                        event_sequence=1,
                        execution_epoch=0,
                        worker_id=None,
                        lease_expires_at=None,
                        execution_started_at=None,
                        execution_deadline_at=None,
                        queued_at=NOW,
                        first_started_at=None,
                        finished_at=None,
                        created_at=NOW,
                        updated_at=NOW,
                        cancel_requested_at=None,
                        safe_error_code=None,
                        usage=RunUsage(),
                    )

                async def get(self, session_id: str, owner_id: str, run_id: str) -> Run | None:
                    self.calls.append("get")
                    return self.run if run_id == "r_legacy" else None

                async def get_pending_input(
                    self, session_id: str, owner_id: str, run_id: str
                ) -> RunInputRequest | None:
                    self.calls.append("get_pending_input")
                    return None

                async def list_pending_approvals(
                    self, session_id: str, owner_id: str, run_id: str
                ) -> tuple[Any, ...] | None:
                    self.calls.append("list_pending_approvals")
                    return ()

                async def list_subagent_tasks(
                    self, session_id: str, owner_id: str, parent_run_id: str
                ) -> tuple[Any, ...]:
                    self.calls.append("list_subagent_tasks")
                    return ()

                async def list_pending_subagent_inputs(
                    self, session_id: str, owner_id: str, parent_run_id: str
                ) -> tuple[Any, ...]:
                    self.calls.append("list_pending_subagent_inputs")
                    return ()

                async def list_pending_subagent_approvals(
                    self, session_id: str, owner_id: str, parent_run_id: str
                ) -> tuple[Any, ...]:
                    self.calls.append("list_pending_subagent_approvals")
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
                    del session_id, owner_id, run_id, after_sequence, limit, through_sequence
                    self.calls.append("list_events")
                    return (
                        RunEvent(
                            id="e_1",
                            run_id="r_legacy",
                            sequence=1,
                            type="queued",
                            created_at=NOW,
                            payload=RunQueuedPayload(trigger_message_id="m_1"),
                        ),
                    )

            legacy_repo = _LegacyRunRepo()
            legacy_service = RunService(cast(RunRepository, legacy_repo))
            legacy_events = await legacy_service.events(
                "s_1", "u_1", "r_legacy", after_sequence=0, limit=10
            )
            assert len(legacy_events) == 1
            assert legacy_repo.calls == ["get", "list_events"]

            legacy_repo.calls.clear()
            legacy_details = await legacy_service.get("s_1", "u_1", "r_legacy")
            assert legacy_details.run.id == "r_legacy"
            assert legacy_repo.calls == [
                "get",
                "get_pending_input",
                "list_pending_approvals",
                "list_subagent_tasks",
                "list_pending_subagent_inputs",
                "list_pending_subagent_approvals",
            ]
        finally:
            await engine.dispose()

    _run(_scenario())


# ---------------------------------------------------------------------------
# 5. 模块 A.2.3：ModelProfileRepository 只读接口去 FOR SHARE 与鉴权边界
# ---------------------------------------------------------------------------


def test_model_profile_readonly_methods_drop_row_locks_and_enforce_auth() -> None:
    """验证 list/list_with_default/get_default_id 无 FOR SHARE/FOR UPDATE，写接口保锁且鉴权严格。"""

    async def _scenario() -> None:
        engine, factory = await _create_sqlite_db()
        cipher = McpSecretCipher(SECRET_KEY)
        try:
            async with factory() as session, session.begin():
                session.add_all(
                    [
                        UserModel(
                            id="u_admin",
                            email="admin@test.com",
                            password_hash="h",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.SUPER_ADMIN.value,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        UserModel(
                            id="u_demoted",
                            email="demoted@test.com",
                            password_hash="h",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.MEMBER.value,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        UserModel(
                            id="u_disabled",
                            email="disabled@test.com",
                            password_hash="h",
                            status=UserStatus.DISABLED.value,
                            role=UserRole.SUPER_ADMIN.value,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        UserModel(
                            id="u_deleted",
                            email="deleted@test.com",
                            password_hash="h",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.SUPER_ADMIN.value,
                            created_at=NOW,
                            updated_at=NOW,
                            deleted_time=NOW,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        AuthSessionModel(
                            id="ses_valid",
                            user_id="u_admin",
                            token_hash="h_valid",
                            expires_at=NOW + timedelta(hours=1),
                            revoked_time=None,
                            created_at=NOW,
                        ),
                        AuthSessionModel(
                            id="ses_revoked",
                            user_id="u_admin",
                            token_hash="h_revoked",
                            expires_at=NOW + timedelta(hours=1),
                            revoked_time=NOW,
                            created_at=NOW,
                        ),
                        AuthSessionModel(
                            id="ses_expired",
                            user_id="u_admin",
                            token_hash="h_expired",
                            expires_at=NOW,
                            revoked_time=None,
                            created_at=NOW,
                        ),
                        AuthSessionModel(
                            id="ses_demoted",
                            user_id="u_demoted",
                            token_hash="h_demoted",
                            expires_at=NOW + timedelta(hours=1),
                            revoked_time=None,
                            created_at=NOW,
                        ),
                        AuthSessionModel(
                            id="ses_disabled",
                            user_id="u_disabled",
                            token_hash="h_disabled",
                            expires_at=NOW + timedelta(hours=1),
                            revoked_time=None,
                            created_at=NOW,
                        ),
                        AuthSessionModel(
                            id="ses_deleted",
                            user_id="u_deleted",
                            token_hash="h_deleted",
                            expires_at=NOW + timedelta(hours=1),
                            revoked_time=None,
                            created_at=NOW,
                        ),
                    ]
                )

            def _actor(uid: str, sid: str) -> AuthContext:
                # 即使调用方伪造 AuthContext.user.role=SUPER_ADMIN，仓储也必须实时复核数据库。
                return AuthContext(
                    user=User(
                        id=uid,
                        email=f"{uid}@test.com",
                        password_hash="h",
                        display_name=None,
                        status=UserStatus.ACTIVE,
                        created_at=NOW,
                        updated_at=NOW,
                        role=UserRole.SUPER_ADMIN,
                    ),
                    session_id=sid,
                )

            # 包装 Session 以捕获 PostgreSQL 方言编译后的 SQL 及 session.begin() 调用次数
            pg_sqls: list[str] = []
            begin_calls = 0

            class _InspectingSession:
                def __init__(self, inner: AsyncSession) -> None:
                    self._inner = inner

                def begin(self) -> Any:
                    nonlocal begin_calls
                    begin_calls += 1
                    return self._inner.begin()

                async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                    if hasattr(statement, "compile"):
                        pg_sqls.append(str(statement.compile(dialect=_PG_DIALECT)))
                    return await self._inner.scalar(statement, *args, **kwargs)

                async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                    if hasattr(statement, "compile"):
                        pg_sqls.append(str(statement.compile(dialect=_PG_DIALECT)))
                    return await self._inner.scalars(statement, *args, **kwargs)

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

            @asynccontextmanager
            async def _inspecting_factory() -> Any:
                async with factory() as s:
                    yield _InspectingSession(s)

            repo = SqlAlchemyModelProfileRepository(
                cast(Any, _inspecting_factory),
                cipher,
                clock=lambda: NOW,
            )
            valid_actor = _actor("u_admin", "ses_valid")

            # 先通过写方法创建一条模型并置默认，验证写方法仍持 FOR SHARE + 显式事务
            pg_sqls.clear()
            begin_calls = 0
            created = await repo.create(
                valid_actor,
                ModelProfileInput(
                    name="测试模型",
                    base_url="https://llm.test/v1",
                    model="gpt-4o",
                    api_key="secret-key",
                    context_window_tokens=8192,
                    max_output_tokens=512,
                ),
            )
            assert begin_calls == 1
            assert any("FOR SHARE" in sql for sql in pg_sqls)
            async with factory() as session, session.begin():
                session.add(DefaultModelProfileModel(singleton_id=1, profile_id=created.id))

            # 验证三大只读接口均不开启 session.begin()，且 PostgreSQL SQL 无 FOR SHARE / FOR UPDATE
            for method_name in ("list", "list_with_default", "get_default_id"):
                pg_sqls.clear()
                begin_calls = 0
                if method_name == "list":
                    items = await repo.list(valid_actor)
                    assert [item.id for item in items] == [created.id]
                elif method_name == "list_with_default":
                    items, default_id = await repo.list_with_default(valid_actor)
                    assert [item.id for item in items] == [created.id]
                    assert default_id == created.id
                else:
                    assert await repo.get_default_id(valid_actor) == created.id
                assert begin_calls == 0, f"{method_name} 不应开启写事务"
                assert pg_sqls, f"{method_name} 应执行只读 SQL"
                assert all("FOR SHARE" not in sql and "FOR UPDATE" not in sql for sql in pg_sqls), (
                    f"{method_name} 不应申请行锁: {pg_sqls}"
                )

            # 验证全部异常鉴权场景在三大只读接口均抛出 ModelProfileAuthorizationError
            invalid_actors = (
                _actor("u_admin", "ses_revoked"),
                _actor("u_admin", "ses_expired"),
                _actor("u_admin", "ses_unknown"),
                _actor("u_demoted", "ses_demoted"),
                _actor("u_disabled", "ses_disabled"),
                _actor("u_deleted", "ses_deleted"),
            )
            for bad_actor in invalid_actors:
                with pytest.raises(ModelProfileAuthorizationError):
                    await repo.list(bad_actor)
                with pytest.raises(ModelProfileAuthorizationError):
                    await repo.list_with_default(bad_actor)
                with pytest.raises(ModelProfileAuthorizationError):
                    await repo.get_default_id(bad_actor)

            # 再次确认写方法（set_enabled）仍保留 FOR SHARE 锁守卫
            pg_sqls.clear()
            begin_calls = 0
            toggled = await repo.set_enabled(valid_actor, created.id, False)
            assert toggled.enabled is False
            assert begin_calls == 1
            assert any("FOR SHARE" in sql for sql in pg_sqls)
        finally:
            await engine.dispose()

    _run(_scenario())
