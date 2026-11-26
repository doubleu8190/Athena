"""M3.3 公共/部门技能在线 Markdown 双形态、首页推荐元数据与迁移验收测试。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import sqlite3
import zipfile
from collections.abc import AsyncIterator, Sequence
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapter.access.repository import SqlAlchemyAccessControlRepository
from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.models import Base
from adapter.org.model import DepartmentModel, DeptAdminScopeModel, UserDepartmentModel
from adapter.org.repository import SqlAlchemyOrgRepository
from adapter.skill.repository import SqlAlchemySkillRepository, build_run_skill_snapshot
from api.auth import AuthCookieConfig, build_auth_router
from api.auth_middleware import AuthPrecheckMiddleware
from api.security import DEFAULT_COOKIE_NAME
from api.skill import (
    build_skill_admin_router,
    build_skill_personal_router,
    build_skill_user_router,
)
from api.upload_middleware import UploadAdmissionMiddleware, match_upload_policy
from app.access import AccessPolicyService
from app.auth import AuthService
from app.skill.dependency import DependencyChecker
from app.skill.personal import (
    build_admin_markdown_slug,
    validate_optional_admin_slug,
    validate_starter_prompt,
    validate_version_label,
)
from app.skill.service import SkillService
from domain.auth import AuthProvider, UserRole, UserStatus
from domain.file import FileStorageError
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    PackageResource,
    RunSkillEntry,
    RunSkillSnapshot,
    SkillConflictError,
    SkillFormat,
    SkillPackageError,
    SkillPackageManifest,
    SkillScope,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
NOW = datetime(2026, 11, 24, 5, 30, tzinfo=UTC)


class _FakePasswordHasher:
    """测试专用轻量密码校验器，避免 Argon2 耗时。"""

    dummy_hash = "hash:dummy"

    def hash_password(self, password: str) -> str:
        return f"hash:{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        return password_hash == f"hash:{password}"


class _MemoryFileObjectStore:
    """记录对象写入与孤儿清理的内存私有对象存储。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted_keys: list[str] = []

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        del content_type
        self.objects[key] = path.read_bytes()

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        del content_type
        self.objects[key] = bytes(content)

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        data = self.objects[key]
        if len(data) > maximum_bytes:
            raise ValueError("object_too_large")
        return data

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        return self.objects[key][offset : offset + length]

    async def iter_range(
        self,
        key: str,
        *,
        offset: int = 0,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        data = self.objects[key][offset:]
        for index in range(0, len(data), chunk_size):
            yield data[index : index + chunk_size]

    async def delete(self, key: str) -> None:
        self.deleted_keys.append(key)
        self.objects.pop(key, None)


class _InstantSatisfiedChecker(DependencyChecker):
    """返回恒定满足的依赖检查器，避免测试频繁派生 Python 探测子进程。"""

    def __init__(self, clock: Any) -> None:
        self._test_clock = clock

    def check(
        self,
        requirements: Sequence[str],
        *,
        checked_at: datetime | None = None,
    ) -> DependencyCheck:
        del requirements
        return DependencyCheck(
            status=DependencyCheckStatus.SATISFIED,
            python_version="3.12.0",
            python_implementation="CPython",
            items=(),
            checked_at=checked_at or self._test_clock(),
        )

    def check_cached(self, requirements: Sequence[str]) -> DependencyCheck:
        return self.check(requirements, checked_at=self._test_clock())


def _build_zip_package(
    *,
    slug: str,
    name: str,
    description: str,
    guide: str = "请按规范执行。",
    with_script: bool = False,
) -> bytes:
    """构造可通过 SkillPackageValidator 校验的最小 ZIP 技能包（可选附带登记脚本）。"""
    frontmatter_lines = [
        "---",
        f"slug: {slug}",
        f"name: {name}",
        f"description: {description}",
    ]
    if with_script:
        frontmatter_lines.extend(
            [
                "scripts:",
                "  - path: scripts/calc.py",
                "    label: 汇总计算",
                "    purpose: 执行数据汇总脚本",
                "    parameters:",
                "      type: object",
                "      properties:",
                "        month:",
                "          type: string",
                "      required: [month]",
                "      additionalProperties: false",
            ]
        )
    frontmatter_lines.append("---")
    skill_md = "\n".join(frontmatter_lines) + "\n" + guide + "\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", skill_md.encode("utf-8"))
        if with_script:
            archive.writestr(
                "scripts/calc.py",
                b"def main():\n    return 'ok'\n",
            )
            archive.writestr(
                "references/template.txt",
                "标准模板说明".encode(),
            )
    return buf.getvalue()


class _M33Harness:
    """端到端 SQLite + FastAPI 测试环境，覆盖超管、部门管理员与普通员工三层权限。"""

    def __init__(self, tmp_path: Path, *, with_object_store: bool = True) -> None:
        self._now = NOW
        self.db_path = tmp_path / "m3_3_skills.sqlite"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")

        @event.listens_for(self.engine.sync_engine, "connect")
        def _enable_sqlite_fk(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.access_repo = SqlAlchemyAccessControlRepository(self.session_factory)
        self.access_policy = AccessPolicyService(self.access_repo, clock=self.clock)
        self.auth_repo = SqlAlchemyAuthRepository(self.session_factory)
        self.org_repo = SqlAlchemyOrgRepository(self.session_factory)
        self.auth_service = AuthService(
            self.auth_repo,
            _FakePasswordHasher(),
            org_reader=self.org_repo,
            clock=self.clock,
        )
        self.object_store = _MemoryFileObjectStore()
        self.skill_repo = SqlAlchemySkillRepository(self.session_factory)
        self.skill_service = SkillService(
            self.skill_repo,
            cast(Any, self.object_store) if with_object_store else None,
            self.auth_repo,
            access_policy=self.access_policy,
            dependency_checker=_InstantSatisfiedChecker(self.clock),
            clock=self.clock,
        )
        self.app = self._build_app()
        asyncio.run(self._bootstrap())

    def clock(self) -> datetime:
        return self._now

    def advance_clock(self, seconds: int = 10) -> None:
        self._now = self._now + timedelta(seconds=seconds)

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
        app.include_router(build_skill_user_router(self.skill_service))
        app.include_router(build_skill_personal_router(self.skill_service))
        app.include_router(build_skill_admin_router(self.skill_service))
        return app

    async def _bootstrap(self) -> None:
        """建表并写入组织树与 5 个典型角色账号（超管、财务主管、研发主管、财务员工、研发员工）。"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # 写入组织部门树：root -> dept_fin / dept_rd
        async with self.session_factory() as session, session.begin():
            session.add_all(
                [
                    DepartmentModel(
                        id="dept_root",
                        external_id="ext_root",
                        parent_id=None,
                        name="总部",
                        path="/dept_root/",
                        order_index=1,
                        status="active",
                        synced_at=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    DepartmentModel(
                        id="dept_fin",
                        external_id="ext_fin",
                        parent_id="dept_root",
                        name="财务部",
                        path="/dept_root/dept_fin/",
                        order_index=2,
                        status="active",
                        synced_at=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    DepartmentModel(
                        id="dept_rd",
                        external_id="ext_rd",
                        parent_id="dept_root",
                        name="研发部",
                        path="/dept_root/dept_rd/",
                        order_index=3,
                        status="active",
                        synced_at=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )

        users_spec = (
            ("usr_super", "super@acme.com", "超级管理员", UserRole.SUPER_ADMIN),
            ("usr_fin_admin", "fin_admin@acme.com", "财务主管", UserRole.DEPT_ADMIN),
            ("usr_rd_admin", "rd_admin@acme.com", "研发主管", UserRole.DEPT_ADMIN),
            ("usr_fin_member", "fin_member@acme.com", "财务员工", UserRole.MEMBER),
            ("usr_rd_member", "rd_member@acme.com", "研发员工", UserRole.MEMBER),
        )
        async with self.session_factory() as session, session.begin():
            for user_id, email, display_name, role in users_spec:
                session.add(
                    UserModel(
                        id=user_id,
                        email=email,
                        password_hash="hash:secret123",
                        display_name=display_name,
                        status=UserStatus.ACTIVE.value,
                        role=role.value,
                        auth_provider=AuthProvider.LOCAL.value,
                        created_at=NOW,
                        updated_at=NOW,
                        deleted_time=None,
                    )
                )
            await session.flush()
            session.add_all(
                [
                    UserDepartmentModel(
                        user_id="usr_fin_admin",
                        department_id="dept_fin",
                        is_primary=True,
                    ),
                    UserDepartmentModel(
                        user_id="usr_rd_admin",
                        department_id="dept_rd",
                        is_primary=True,
                    ),
                    UserDepartmentModel(
                        user_id="usr_fin_member",
                        department_id="dept_fin",
                        is_primary=True,
                    ),
                    UserDepartmentModel(
                        user_id="usr_rd_member",
                        department_id="dept_rd",
                        is_primary=True,
                    ),
                    DeptAdminScopeModel(
                        user_id="usr_fin_admin",
                        department_id="dept_fin",
                        include_subdepartments=True,
                        created_at=NOW,
                    ),
                    DeptAdminScopeModel(
                        user_id="usr_rd_admin",
                        department_id="dept_rd",
                        include_subdepartments=True,
                        created_at=NOW,
                    ),
                ]
            )

    def login_client(self, email: str) -> TestClient:
        """创建已完成真实 Cookie 登录的独立 TestClient。"""
        client = TestClient(self.app)
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "secret123", "remember_me": False},
        )
        assert resp.status_code == 200, resp.text
        return client

    def close(self) -> None:
        asyncio.run(self.engine.dispose())


# ---------------------------------------------------------------------------
# 1. Alembic 20261206_0039 迁移单一 head、PG 离线 DDL 与 SQLite 在线升级/约束/回滚
# ---------------------------------------------------------------------------


def test_alembic_migration_0039_single_head_pg_sql_and_sqlite_roundtrip(
    tmp_path: Path,
) -> None:
    """验证 20261206_0039 单一 head、PG 离线 SQL 及 SQLite 回填/CheckConstraint/降级。"""
    pg_cfg = Config(str(ALEMBIC_INI))
    pg_cfg.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    script = ScriptDirectory.from_config(pg_cfg)
    assert len(script.get_heads()) == 1
    rev = script.get_revision("20261206_0039")
    assert rev is not None
    assert rev.down_revision == "20261205_0038"

    # 1. PostgreSQL 离线 upgrade / downgrade SQL 校验
    up_buf = StringIO()
    with redirect_stdout(up_buf):
        command.upgrade(pg_cfg, "20261205_0038:20261206_0039", sql=True)
    up_sql = up_buf.getvalue()
    assert "ADD COLUMN format VARCHAR(16) DEFAULT 'package' NOT NULL" in up_sql
    assert "ADD COLUMN featured BOOLEAN DEFAULT false NOT NULL" in up_sql
    assert "ADD COLUMN starter_prompt VARCHAR(500)" in up_sql
    assert "UPDATE skills SET format = 'markdown' WHERE scope = 'personal'" in up_sql
    assert "ck_skills_format" in up_sql
    assert "ck_skills_personal_format_and_featured" in up_sql
    # 确保数据回填发生在施加个人组合约束之前
    assert up_sql.index("UPDATE skills SET format = 'markdown'") < up_sql.index(
        "ck_skills_personal_format_and_featured"
    )

    down_buf = StringIO()
    with redirect_stdout(down_buf):
        command.downgrade(pg_cfg, "20261206_0039:20261205_0038", sql=True)
    down_sql = down_buf.getvalue()
    assert "DROP CONSTRAINT ck_skills_personal_format_and_featured" in down_sql
    assert "DROP CONSTRAINT ck_skills_format" in down_sql
    assert "DROP COLUMN starter_prompt" in down_sql
    assert "DROP COLUMN featured" in down_sql
    assert "DROP COLUMN format" in down_sql

    # 2. SQLite 在线迁移：构造 20261205_0038 基线 skills 表并预置 company / personal 旧数据
    db_path = tmp_path / "m3_3_migration.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20261205_0038');
        CREATE TABLE users (id VARCHAR(128) NOT NULL PRIMARY KEY);
        CREATE TABLE departments (id VARCHAR(64) NOT NULL PRIMARY KEY);
        INSERT INTO users VALUES ('usr_1');
        INSERT INTO departments VALUES ('dept_1');
        CREATE TABLE skills (
            id VARCHAR(128) NOT NULL PRIMARY KEY,
            slug VARCHAR(64) NOT NULL,
            scope VARCHAR(16) NOT NULL DEFAULT 'company',
            owner_id VARCHAR(128),
            owner_department_id VARCHAR(64),
            deleted_at DATETIME,
            current_version_id VARCHAR(128),
            enabled BOOLEAN NOT NULL DEFAULT 0,
            script_requires_approval BOOLEAN NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1,
            created_by VARCHAR(128),
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_skills_revision CHECK (revision >= 1),
            CONSTRAINT ck_skills_scope CHECK (scope IN ('company', 'department', 'personal')),
            CONSTRAINT ck_skills_owner_scope CHECK (
                (scope = 'company' AND owner_id IS NULL AND owner_department_id IS NULL
                    AND deleted_at IS NULL) OR
                (scope = 'department' AND owner_id IS NULL AND owner_department_id IS NOT NULL) OR
                (scope = 'personal' AND owner_id IS NOT NULL AND owner_department_id IS NULL)
            )
        );
        INSERT INTO skills (
            id, slug, scope, owner_id, owner_department_id, deleted_at,
            enabled, script_requires_approval, revision, created_by, created_at, updated_at
        ) VALUES
            ('sk_company', 'legacy-co', 'company', NULL, NULL, NULL,
                1, 0, 1, 'usr_1', '2026-11-24 00:00:00', '2026-11-24 00:00:00'),
            ('sk_dept', 'legacy-dept', 'department', NULL, 'dept_1', NULL,
                1, 0, 1, 'usr_1', '2026-11-24 00:00:00', '2026-11-24 00:00:00'),
            ('sk_personal', 'legacy-me', 'personal', 'usr_1', NULL, NULL,
                1, 0, 1, 'usr_1', '2026-11-24 00:00:00', '2026-11-24 00:00:00');
        """
    )
    conn.close()

    sqlite_cfg = Config(str(ALEMBIC_INI))
    sqlite_cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(sqlite_cfg, "20261206_0039")

    conn = sqlite3.connect(db_path)
    rows = {
        row[0]: (row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT id, format, featured, starter_prompt FROM skills"
        ).fetchall()
    }
    assert rows["sk_company"] == ("package", 0, None)
    assert rows["sk_dept"] == ("package", 0, None)
    # 历史个人技能必须自动回填为 format='markdown', featured=0
    assert rows["sk_personal"] == ("markdown", 0, None)

    # 非法 format 值必须被 ck_skills_format 拦截
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE skills SET format = 'zip' WHERE id = 'sk_company'")
    # 个人技能修改为 format='package' 必须被 ck_skills_personal_format_and_featured 拦截
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE skills SET format = 'package' WHERE id = 'sk_personal'")
    # 个人技能设为 featured=1 必须被 ck_skills_personal_format_and_featured 拦截
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE skills SET featured = 1 WHERE id = 'sk_personal'")
    # 公司/部门技能允许设为 markdown + featured=1 + starter_prompt
    conn.execute(
        """
        UPDATE skills
        SET format = 'markdown', featured = 1, starter_prompt = '请上传财务凭证'
        WHERE id = 'sk_dept'
        """
    )
    conn.commit()
    conn.close()

    # 完整降级回滚到 20261205_0038
    command.downgrade(sqlite_cfg, "20261205_0038")
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(skills)").fetchall()}
    assert not cols.intersection({"format", "featured", "starter_prompt"})
    conn.close()
    assert str(text("SELECT 1")) == "SELECT 1"


# ---------------------------------------------------------------------------
# 2. 纯校验器边界单元测试
# ---------------------------------------------------------------------------


def test_m33_validators_starter_prompt_slug_and_version_label() -> None:
    """校验 starter_prompt、可选 slug 与版本标签的白名单与边界。"""
    assert validate_starter_prompt(None) is None
    assert validate_starter_prompt("") is None
    assert validate_starter_prompt("   \t\n  ") is None
    assert validate_starter_prompt("  请帮我核对\n本月报销单  ") == "请帮我核对\n本月报销单"
    assert validate_starter_prompt("中" * 500) == "中" * 500
    with pytest.raises(SkillPackageError, match="skill_starter_prompt_invalid"):
        validate_starter_prompt("中" * 501)
    with pytest.raises(SkillPackageError, match="skill_starter_prompt_invalid"):
        validate_starter_prompt("非法控制符\x00内容")
    with pytest.raises(SkillPackageError, match="skill_starter_prompt_invalid"):
        validate_starter_prompt("包含DEL\x7f内容")

    assert validate_optional_admin_slug(None) is None
    assert validate_optional_admin_slug("   ") is None
    assert validate_optional_admin_slug("  fin-audit-2026  ") == "fin-audit-2026"
    assert validate_optional_admin_slug("a") == "a"
    for bad_slug in ("UpperCase", "has_underscore", "-lead", "trail-", "a b", "a" * 65):
        with pytest.raises(SkillPackageError, match="skill_slug_invalid"):
            validate_optional_admin_slug(bad_slug)

    auto_slug = build_admin_markdown_slug("skill_01J9ABC123DEF456")
    assert auto_slug == "md-01j9abc123def456"
    long_auto_slug = build_admin_markdown_slug("skill_" + "x" * 100)
    assert len(long_auto_slug) <= 64
    assert validate_optional_admin_slug(long_auto_slug) == long_auto_slug

    assert validate_version_label(None) is None
    assert validate_version_label("   ") is None
    assert validate_version_label("  v2.1-Q4  ") == "v2.1-Q4"
    assert validate_version_label("x" * 64) == "x" * 64
    with pytest.raises(SkillPackageError, match="skill_version_label_invalid"):
        validate_version_label("x" * 65)
    with pytest.raises(SkillPackageError, match="skill_version_label_invalid"):
        validate_version_label("v2\nnewline")
    with pytest.raises(SkillPackageError, match="skill_version_label_invalid"):
        validate_version_label("v2\tbad")


# ---------------------------------------------------------------------------
# 3. super_admin 在线创建 company / department Markdown 技能 + 幂等与冲突
# ---------------------------------------------------------------------------


def test_super_admin_online_markdown_create_idempotency_and_slug_conflict(
    tmp_path: Path,
) -> None:
    """验证超管创建 company/department 在线 Markdown、自动/显式 slug、幂等重放与冲突。"""
    harness = _M33Harness(tmp_path)
    try:
        super_client = harness.login_client("super@acme.com")

        # 1) company 级技能：省略 slug 自动派生，带 enabled / featured / starter_prompt
        company_payload = {
            "name": "全员公文润色助手",
            "description": "统一全公司正式对外公文格式与措辞标准",
            "guide": "# 润色规范\n保持客观、简明、无口语化表达。",
            "scope": "company",
            "enabled": True,
            "featured": True,
            "starter_prompt": "  请帮我把下面草稿润色为正式公文：\n",
        }
        created_co = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "md-co-1"},
            json=company_payload,
        )
        assert created_co.status_code == 201, created_co.text
        co_body = created_co.json()
        assert co_body["scope"] == "company"
        assert co_body["format"] == "markdown"
        assert co_body["has_scripts"] is False
        assert co_body["enabled"] is True
        assert co_body["featured"] is True
        assert co_body["starter_prompt"] == "请帮我把下面草稿润色为正式公文："
        assert co_body["slug"].startswith("md-")
        assert co_body["revision"] == 1
        assert co_body["current_version_label"] == "v1"
        assert co_body["current_version_id"] == co_body["versions"][0]["id"]
        assert co_body["dependency_check"]["status"] == "satisfied"
        # 纯 Markdown 走纯数据库持久化，0 次触达 MinIO，且版本 object_key 为 None。
        assert len(harness.object_store.objects) == 0
        co_version = asyncio.run(harness.skill_repo.get_version(co_body["current_version_id"]))
        assert co_version is not None
        assert co_version.object_key is None
        assert co_version.package_sha256 == hashlib.sha256(
            str(company_payload["guide"]).encode("utf-8")
        ).hexdigest()

        # 2) 相同 Idempotency-Key + 相同请求体（含未传 slug）精准幂等重放，仍 0 次触达 MinIO
        replayed_co = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "md-co-1"},
            json=company_payload,
        )
        assert replayed_co.status_code == 201
        assert replayed_co.json()["id"] == co_body["id"]
        assert replayed_co.json()["slug"] == co_body["slug"]
        assert len(harness.object_store.objects) == 0

        # 3) 相同 Idempotency-Key 但修改任意字段（如 featured / starter_prompt）返回 409 幂等冲突
        conflict_idem = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "md-co-1"},
            json={**company_payload, "featured": False},
        )
        assert conflict_idem.status_code == 409
        assert conflict_idem.json() == {"detail": "idempotency_conflict"}

        # 4) department 级技能：显式 slug、初始停用 enabled=False、附带跨部门可见性授权
        dept_payload = {
            "name": "跨部门合同法务初审",
            "description": "财务部维护、同时授权研发部可读的合同审查指南",
            "guide": "# 合同初审清单\n1. 检查主体与签章\n2. 检查付款与违约条款",
            "scope": "department",
            "owner_department_id": "dept_fin",
            "visibility_grants": [
                {
                    "target_type": "department",
                    "target_id": "dept_fin",
                    "include_subdepartments": True,
                },
                {
                    "target_type": "department",
                    "target_id": "dept_rd",
                    "include_subdepartments": False,
                },
            ],
            "slug": "fin-contract-review",
            "enabled": False,
            "featured": False,
            "starter_prompt": "请贴出待审合同关键条款。",
        }
        created_dept = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "md-dept-1"},
            json=dept_payload,
        )
        assert created_dept.status_code == 201, created_dept.text
        dept_body = created_dept.json()
        assert dept_body["scope"] == "department"
        assert dept_body["owner_department_id"] == "dept_fin"
        assert dept_body["slug"] == "fin-contract-review"
        assert dept_body["enabled"] is False
        assert dept_body["format"] == "markdown"
        # 即便初始停用，在线 Markdown 仍绑定首版 current_version_id 便于管理列表展示名称与版本
        assert dept_body["current_version_id"] == dept_body["versions"][0]["id"]
        assert dept_body["current_version_label"] == "v1"
        assert {g["target_id"] for g in dept_body["visibility_grants"]} == {"dept_fin", "dept_rd"}

        # 5) 同部门重复显式 slug（新幂等键）返回 409 skill_slug_conflict 且全程 0 次触达对象存储
        slug_conflict = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "md-dept-slug-dup"},
            json=dept_payload,
        )
        assert slug_conflict.status_code == 409
        assert slug_conflict.json() == {"detail": "skill_slug_conflict"}
        assert len(harness.object_store.objects) == 0
        assert harness.object_store.deleted_keys == []

        # 6) 非法 slug / 非法 starter_prompt / extra 字段校验
        bad_slug_resp = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "md-bad-slug"},
            json={**company_payload, "slug": "Bad_Slug!"},
        )
        assert bad_slug_resp.status_code == 422
        assert bad_slug_resp.json() == {"detail": "skill_slug_invalid"}

        bad_prompt_resp = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "md-bad-prompt"},
            json={**company_payload, "starter_prompt": "a" * 501},
        )
        assert bad_prompt_resp.status_code == 422
        assert bad_prompt_resp.json() == {"detail": "skill_starter_prompt_invalid"}

        extra_field_resp = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "md-extra"},
            json={**company_payload, "scripts": []},
        )
        assert extra_field_resp.status_code == 422
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# 4. dept_admin 三层分权、.md 解析、版本发布（activate True/False + 乐观锁）与 member 拒绝
# ---------------------------------------------------------------------------


def test_dept_admin_markdown_lifecycle_permissions_and_member_denied(
    tmp_path: Path,
) -> None:
    """验证部门管理员解析 .md、创建部门技能、发布激活/非激活版本、乐观锁及越权隔离。"""
    harness = _M33Harness(tmp_path)
    try:
        super_client = harness.login_client("super@acme.com")
        fin_admin = harness.login_client("fin_admin@acme.com")
        rd_admin = harness.login_client("rd_admin@acme.com")
        fin_member = harness.login_client("fin_member@acme.com")

        # 1) 普通员工访问所有管理端新端点一律返回 403
        for method, url, kwargs in (
            (
                "POST",
                "/api/v1/admin/skills/parse-markdown",
                {"files": {"file": ("guide.md", b"# Guide\ncontent", "text/markdown")}},
            ),
            (
                "POST",
                "/api/v1/admin/skills/markdown",
                {
                    "headers": {"Idempotency-Key": "mem-1"},
                    "json": {
                        "name": "员工尝试",
                        "description": "无权",
                        "guide": "正文",
                        "scope": "department",
                        "owner_department_id": "dept_fin",
                    },
                },
            ),
        ):
            resp = fin_member.request(method, url, **cast(dict[str, Any], kwargs))
            assert resp.status_code == 403
            assert resp.json() == {"detail": "admin_required"}

        # 2) 部门管理员解析 .md 文件（含 YAML frontmatter）不落库
        md_doc = (
            "---\n"
            "name: 差旅报销核查\n"
            "description: 财务部差旅报销制度检查指南\n"
            "---\n"
            "# 核查步骤\n"
            "请核对机票、酒店发票与审批单一致。\n"
        ).encode()
        parsed = fin_admin.post(
            "/api/v1/admin/skills/parse-markdown",
            files={"file": ("travel.md", md_doc, "text/markdown")},
        )
        assert parsed.status_code == 200, parsed.text
        assert parsed.json() == {
            "name": "差旅报销核查",
            "description": "财务部差旅报销制度检查指南",
            "guide": "# 核查步骤\n请核对机票、酒店发票与审批单一致。\n",
        }
        assert len(harness.object_store.objects) == 0

        # 非法扩展名、frontmatter 非法字段与多文件均返回 422
        bad_ext = fin_admin.post(
            "/api/v1/admin/skills/parse-markdown",
            files={"file": ("bad.txt", b"hello", "text/plain")},
        )
        assert bad_ext.status_code == 422
        assert bad_ext.json() == {
            "detail": {"code": "skill_markdown_filename_invalid", "field": "file"}
        }
        multi_file = fin_admin.post(
            "/api/v1/admin/skills/parse-markdown",
            files=[
                ("file", ("a.md", b"a", "text/markdown")),
                ("file", ("b.md", b"b", "text/markdown")),
            ],
        )
        assert multi_file.status_code == 422
        assert multi_file.json() == {"detail": "skill_markdown_file_invalid"}
        empty_md = fin_admin.post(
            "/api/v1/admin/skills/parse-markdown",
            files={"file": ("empty.md", b"", "text/markdown")},
        )
        assert empty_md.status_code == 422
        assert empty_md.json() == {"detail": "skill_markdown_empty"}
        oversized_md = fin_admin.post(
            "/api/v1/admin/skills/parse-markdown",
            files={"file": ("big.md", b"x" * (1024 * 1024 + 1), "text/markdown")},
        )
        assert oversized_md.status_code == 413
        assert oversized_md.json() == {"detail": "skill_markdown_too_large"}

        # 3) 部门管理员越权创建 company 级或非管辖部门技能 -> 403
        deny_co = fin_admin.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "fin-try-co"},
            json={
                "name": "尝试公司级",
                "description": "越权",
                "guide": "正文",
                "scope": "company",
            },
        )
        assert deny_co.status_code == 403

        deny_rd = fin_admin.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "fin-try-rd"},
            json={
                "name": "尝试研发部门",
                "description": "越权",
                "guide": "正文",
                "scope": "department",
                "owner_department_id": "dept_rd",
            },
        )
        assert deny_rd.status_code == 403

        # 4) 财务主管合法创建财务部在线 Markdown 技能
        created = fin_admin.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "fin-create-1"},
            json={
                "name": "差旅报销核查",
                "description": "v1 制度描述",
                "guide": "# v1 指南\n核对基础发票。",
                "scope": "department",
                "owner_department_id": "dept_fin",
                "featured": True,
                "starter_prompt": "请上传你的差旅报销单，我来核对合规性。",
            },
        )
        assert created.status_code == 201, created.text
        sk = created.json()
        skill_id = sk["id"]
        v1_id = sk["current_version_id"]
        assert sk["revision"] == 1
        assert sk["format"] == "markdown"
        assert sk["has_scripts"] is False
        assert sk["featured"] is True

        # 5) 跨部门管理员（研发主管）无读权 -> 查看与编辑均返回 404（防存在性探测）
        assert rd_admin.get(f"/api/v1/admin/skills/{skill_id}").status_code == 404
        rd_edit = rd_admin.post(
            f"/api/v1/admin/skills/{skill_id}/versions/markdown",
            headers={"Idempotency-Key": "rd-edit-fin"},
            json={
                "name": "越权篡改",
                "description": "越权",
                "guide": "越权正文",
                "expected_revision": 1,
            },
        )
        assert rd_edit.status_code == 404
        assert rd_edit.json() == {"detail": "skill_not_found"}

        # 超管创建 company 级 Markdown 技能 -> 财务主管可读但无管理权，编辑返回 403
        co_sk = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "co-md-readable"},
            json={
                "name": "公司通用知识指南",
                "description": "全员可读",
                "guide": "全员指南正文。",
                "scope": "company",
            },
        ).json()
        fin_edit_co = fin_admin.post(
            f"/api/v1/admin/skills/{co_sk['id']}/versions/markdown",
            headers={"Idempotency-Key": "fin-edit-co"},
            json={
                "name": "试图修改公司级",
                "description": "无管理权",
                "guide": "正文",
                "expected_revision": 1,
            },
        )
        assert fin_edit_co.status_code == 403
        assert fin_edit_co.json() == {"detail": "admin_required"}

        # 6) 财务主管在线发布 v2（activate=True + 自定义 label）
        v2_payload = {
            "name": "差旅报销核查（2026新版）",
            "description": "v2 新增海外差旅标准",
            "guide": "# v2 指南\n新增海外差旅住宿上限核查。",
            "expected_revision": 1,
            "label": "v2-overseas",
            "activate": True,
        }
        pub_v2 = fin_admin.post(
            f"/api/v1/admin/skills/{skill_id}/versions/markdown",
            headers={"Idempotency-Key": "fin-pub-v2"},
            json=v2_payload,
        )
        assert pub_v2.status_code == 201, pub_v2.text
        v2_body = pub_v2.json()
        assert v2_body["revision"] == 2
        assert v2_body["name"] == "差旅报销核查（2026新版）"
        assert v2_body["description"] == "v2 新增海外差旅标准"
        assert v2_body["current_version_label"] == "v2-overseas"
        assert v2_body["current_version_id"] != v1_id
        v2_id = v2_body["current_version_id"]
        assert [v["ordinal"] for v in v2_body["versions"]] == [2, 1]

        # 同幂等键重放（当前 revision 已为 2，但 expected_revision=1 与原请求一致）应安全重放
        replay_v2 = fin_admin.post(
            f"/api/v1/admin/skills/{skill_id}/versions/markdown",
            headers={"Idempotency-Key": "fin-pub-v2"},
            json=v2_payload,
        )
        assert replay_v2.status_code == 201
        assert replay_v2.json()["current_version_id"] == v2_id
        assert len(replay_v2.json()["versions"]) == 2

        # 同幂等键异参返回 409 idempotency_conflict
        idem_conflict_v2 = fin_admin.post(
            f"/api/v1/admin/skills/{skill_id}/versions/markdown",
            headers={"Idempotency-Key": "fin-pub-v2"},
            json={**v2_payload, "label": "different-label"},
        )
        assert idem_conflict_v2.status_code == 409
        assert idem_conflict_v2.json() == {"detail": "idempotency_conflict"}

        # 7) 财务主管发布 v3（activate=False，省略 label 派生 v3）：不切指针但递增 revision
        v3_payload = {
            "name": "差旅报销核查（草稿 v3）",
            "description": "暂不激活的灰度版本",
            "guide": "# v3 灰度草稿\n等待审批后激活。",
            "expected_revision": 2,
            "activate": False,
        }
        pub_v3 = fin_admin.post(
            f"/api/v1/admin/skills/{skill_id}/versions/markdown",
            headers={"Idempotency-Key": "fin-pub-v3"},
            json=v3_payload,
        )
        assert pub_v3.status_code == 201, pub_v3.text
        v3_body = pub_v3.json()
        assert v3_body["revision"] == 3
        # current_version_id 及摘要名称/简介保持指向已激活的 v2
        assert v3_body["current_version_id"] == v2_id
        assert v3_body["current_version_label"] == "v2-overseas"
        assert v3_body["name"] == "差旅报销核查（2026新版）"
        assert [v["label"] for v in v3_body["versions"]] == ["v3", "v2-overseas", "v1"]

        # activate=False 的幂等重放也必须保持指向原激活版本 v2，不错误切换名称或版本指针
        replay_v3 = fin_admin.post(
            f"/api/v1/admin/skills/{skill_id}/versions/markdown",
            headers={"Idempotency-Key": "fin-pub-v3"},
            json=v3_payload,
        )
        assert replay_v3.status_code == 201
        assert replay_v3.json()["revision"] == 3
        assert replay_v3.json()["current_version_id"] == v2_id
        assert replay_v3.json()["name"] == "差旅报销核查（2026新版）"
        assert len(replay_v3.json()["versions"]) == 3

        # 仓储级复核：find_import_result 对 activate=False 的重放返回自洽的当前激活 skill/version，
        # 同时在 request_version 中精确保留本次请求创建的 v3 版本。
        v3_id = next(v["id"] for v in v3_body["versions"] if v["label"] == "v3")

        async def _verify_repo_replay_consistency() -> None:
            from app.skill.personal import validate_personal_content
            from app.skill.service import _admin_markdown_version_fingerprint

            actor = await _auth_context(
                harness.session_factory,
                harness.auth_repo,
                "usr_fin_admin",
            )
            fingerprint = _admin_markdown_version_fingerprint(
                actor=actor,
                target_skill_id=skill_id,
                expected_revision=2,
                draft=validate_personal_content(
                    str(v3_payload["name"]),
                    str(v3_payload["description"]),
                    str(v3_payload["guide"]),
                ),
                label=None,
                activate=False,
                scope="department",
                owner_department_id="dept_fin",
            )
            repo_replay = await harness.skill_repo.find_import_result(
                actor,
                operation="add_version",
                target_skill_id=skill_id,
                idempotency_key="fin-pub-v3",
                request_fingerprint=fingerprint,
            )
            assert repo_replay is not None
            assert repo_replay.skill.current_version_id == v2_id
            assert repo_replay.skill.name == "差旅报销核查（2026新版）"
            assert repo_replay.version.id == v2_id
            assert repo_replay.request_version is not None
            assert repo_replay.request_version.id == v3_id

        asyncio.run(_verify_repo_replay_consistency())

        # 8) 乐观锁冲突：持旧 expected_revision=2 提交新键 -> 409 且无孤儿对象
        objs_before_conflict = len(harness.object_store.objects)
        stale_pub = fin_admin.post(
            f"/api/v1/admin/skills/{skill_id}/versions/markdown",
            headers={"Idempotency-Key": "fin-pub-stale"},
            json={
                "name": "过期修订提交",
                "description": "应被拒绝",
                "guide": "过期正文",
                "expected_revision": 2,
            },
        )
        assert stale_pub.status_code == 409
        assert stale_pub.json() == {"detail": "skill_revision_conflict"}
        assert len(harness.object_store.objects) == objs_before_conflict

        # 非法版本标签返回 422 skill_version_label_invalid
        bad_label = fin_admin.post(
            f"/api/v1/admin/skills/{skill_id}/versions/markdown",
            headers={"Idempotency-Key": "fin-pub-bad-label"},
            json={
                "name": "差旅报销核查",
                "description": "描述",
                "guide": "正文",
                "expected_revision": 3,
                "label": "bad\nlabel",
            },
        )
        assert bad_label.status_code == 422
        assert bad_label.json() == {"detail": "skill_version_label_invalid"}
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# 5. 格式防覆盖底线：ZIP 导入的 format="package" 技能禁止在线 Markdown 编辑
# ---------------------------------------------------------------------------


def test_zip_package_skill_rejects_online_markdown_version_publish(
    tmp_path: Path,
) -> None:
    """对 ZIP 导入的 format='package' 技能调用在线 Markdown 发版必须返回 409 且不留孤儿。"""
    harness = _M33Harness(tmp_path)
    try:
        super_client = harness.login_client("super@acme.com")
        zip_bytes = _build_zip_package(
            slug="payroll-calc-zip",
            name="薪酬计算脚本包",
            description="包含 Python 脚本与模板的完整技能包",
            with_script=True,
        )
        imported = super_client.post(
            "/api/v1/admin/skills/import",
            headers={"Idempotency-Key": "zip-import-1"},
            files={"package": ("payroll.zip", zip_bytes, "application/zip")},
        )
        assert imported.status_code == 201, imported.text
        imp_body = imported.json()
        skill_id = imp_body["id"]
        v1_id = imp_body["versions"][0]["id"]
        assert imp_body["format"] == "package"
        assert imp_body["has_scripts"] is True

        # 激活 ZIP 技能包
        activated = super_client.patch(
            f"/api/v1/admin/skills/{skill_id}",
            json={
                "expected_revision": 1,
                "current_version_id": v1_id,
                "enabled": True,
                "script_requires_approval": True,
            },
        )
        assert activated.status_code == 200
        assert activated.json()["format"] == "package"
        assert activated.json()["has_scripts"] is True
        assert activated.json()["revision"] == 2

        # 尝试在线 Markdown 编辑 ZIP 技能包 -> 必须返回 409 skill_markdown_edit_unsupported
        obj_count_before = len(harness.object_store.objects)
        blocked = super_client.post(
            f"/api/v1/admin/skills/{skill_id}/versions/markdown",
            headers={"Idempotency-Key": "illegal-md-overwrite"},
            json={
                "name": "试图覆盖脚本包",
                "description": "危险覆盖",
                "guide": "如果放行将破坏 scripts/calc.py",
                "expected_revision": 2,
                "activate": True,
            },
        )
        assert blocked.status_code == 409
        assert blocked.json() == {"detail": "skill_markdown_edit_unsupported"}
        # 对象存储未新增残留对象，数据库版本与修订号毫发无损
        assert len(harness.object_store.objects) == obj_count_before
        details = super_client.get(f"/api/v1/admin/skills/{skill_id}").json()
        assert details["revision"] == 2
        assert len(details["versions"]) == 1
        assert details["has_scripts"] is True

        # 复用 ZIP import 的 Idempotency-Key 到 /admin/skills/markdown 也必须因指纹隔离返回 409
        cross_mode_conflict = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "zip-import-1"},
            json={
                "name": "跨模式重放测试",
                "description": "应报幂等冲突",
                "guide": "正文",
            },
        )
        assert cross_mode_conflict.status_code == 409
        assert cross_mode_conflict.json() == {"detail": "idempotency_conflict"}

        # 部门管理员导入的部门级 ZIP 技能包同样受格式锁保护，
        # 但允许通过 PATCH 配置 featured 与 starter_prompt
        fin_admin = harness.login_client("fin_admin@acme.com")
        dept_zip_bytes = _build_zip_package(
            slug="fin-reconcile-zip",
            name="财务对账脚本包",
            description="部门级脚本技能",
            with_script=True,
        )
        dept_imp = fin_admin.post(
            "/api/v1/admin/skills/import",
            headers={"Idempotency-Key": "dept-zip-import-1"},
            data={"scope": "department", "owner_department_id": "dept_fin"},
            files={"package": ("reconcile.zip", dept_zip_bytes, "application/zip")},
        )
        assert dept_imp.status_code == 201, dept_imp.text
        dept_zip_id = dept_imp.json()["id"]
        dept_zip_v1 = dept_imp.json()["versions"][0]["id"]

        dept_zip_blocked = fin_admin.post(
            f"/api/v1/admin/skills/{dept_zip_id}/versions/markdown",
            headers={"Idempotency-Key": "dept-zip-md-blocked"},
            json={
                "name": "非法覆盖部门 ZIP",
                "description": "应被拦截",
                "guide": "禁止覆盖",
                "expected_revision": 1,
            },
        )
        assert dept_zip_blocked.status_code == 409
        assert dept_zip_blocked.json() == {"detail": "skill_markdown_edit_unsupported"}

        dept_zip_patched = fin_admin.patch(
            f"/api/v1/admin/skills/{dept_zip_id}",
            json={
                "expected_revision": 1,
                "current_version_id": dept_zip_v1,
                "enabled": True,
                "script_requires_approval": True,
                "featured": True,
                "starter_prompt": "请上传银行流水与账簿 CSV 开始自动对账。",
            },
        )
        assert dept_zip_patched.status_code == 200
        assert dept_zip_patched.json()["format"] == "package"
        assert dept_zip_patched.json()["has_scripts"] is True
        assert dept_zip_patched.json()["featured"] is True
        assert dept_zip_patched.json()["starter_prompt"] == (
            "请上传银行流水与账簿 CSV 开始自动对账。"
        )
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# 6. PATCH /api/v1/admin/skills/{id} 局部更新 featured / starter_prompt 与清空语义
# ---------------------------------------------------------------------------


def test_patch_skill_configuration_featured_and_starter_prompt_semantics(
    tmp_path: Path,
) -> None:
    """验证 PATCH 向后兼容保留原值、更新 featured、更新/显式 null 清空 starter_prompt 及校验。"""
    harness = _M33Harness(tmp_path)
    try:
        super_client = harness.login_client("super@acme.com")
        created = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "patch-init"},
            json={
                "name": "会议纪要提炼",
                "description": "从会议录音稿提炼待办",
                "guide": "# 提炼规则\n输出议题、结论与负责人。",
                "enabled": True,
                "featured": True,
                "starter_prompt": "请粘贴会议记录全文。",
            },
        ).json()
        skill_id = created["id"]
        v1_id = created["current_version_id"]

        # 1) 旧格式 PATCH（不传 featured / starter_prompt）：保持原有值不变
        compat = super_client.patch(
            f"/api/v1/admin/skills/{skill_id}",
            json={
                "expected_revision": 1,
                "current_version_id": v1_id,
                "enabled": False,
                "script_requires_approval": False,
            },
        )
        assert compat.status_code == 200, compat.text
        assert compat.json()["revision"] == 2
        assert compat.json()["enabled"] is False
        assert compat.json()["featured"] is True
        assert compat.json()["starter_prompt"] == "请粘贴会议记录全文。"

        # 2) 更新 featured=False 并同步修改 starter_prompt（去首尾空白）
        updated = super_client.patch(
            f"/api/v1/admin/skills/{skill_id}",
            json={
                "expected_revision": 2,
                "current_version_id": v1_id,
                "enabled": True,
                "script_requires_approval": False,
                "featured": False,
                "starter_prompt": "  请上传会议纪要文件或贴入文本  ",
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == 3
        assert updated.json()["featured"] is False
        assert updated.json()["starter_prompt"] == "请上传会议纪要文件或贴入文本"

        # 3) 显式传入 starter_prompt: null 清空字段，featured 未传保持 False
        cleared = super_client.patch(
            f"/api/v1/admin/skills/{skill_id}",
            json={
                "expected_revision": 3,
                "current_version_id": v1_id,
                "enabled": True,
                "script_requires_approval": False,
                "starter_prompt": None,
            },
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["revision"] == 4
        assert cleared.json()["featured"] is False
        assert cleared.json()["starter_prompt"] is None

        # 4) 显式传入 featured: null 必须返回 422 skill_featured_invalid
        bad_featured = super_client.patch(
            f"/api/v1/admin/skills/{skill_id}",
            json={
                "expected_revision": 4,
                "current_version_id": v1_id,
                "enabled": True,
                "script_requires_approval": False,
                "featured": None,
            },
        )
        assert bad_featured.status_code == 422
        assert bad_featured.json() == {"detail": "skill_featured_invalid"}

        # 5) 传入非法 starter_prompt（超 500 码点或含控制字符）返回 422 skill_starter_prompt_invalid
        bad_prompt = super_client.patch(
            f"/api/v1/admin/skills/{skill_id}",
            json={
                "expected_revision": 4,
                "current_version_id": v1_id,
                "enabled": True,
                "script_requires_approval": False,
                "starter_prompt": "含控制字符\x07提示",
            },
        )
        assert bad_prompt.status_code == 422
        assert bad_prompt.json() == {"detail": "skill_starter_prompt_invalid"}
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# 7. 目录/列表投影（format / has_scripts / featured / starter_prompt）、排序与 Run 挂载执行
# ---------------------------------------------------------------------------


def test_directory_projection_sorting_and_run_snapshot_execution(
    tmp_path: Path,
) -> None:
    """验证 /skills 与 /admin/skills 完整字段投影、目录排序及普通员工直接挂载在线部门技能。"""
    harness = _M33Harness(tmp_path)
    try:
        super_client = harness.login_client("super@acme.com")
        fin_admin = harness.login_client("fin_admin@acme.com")
        fin_member = harness.login_client("fin_member@acme.com")

        # 1) 公司级 ZIP 含脚本（非推荐）
        zip_with_script = super_client.post(
            "/api/v1/admin/skills/import",
            headers={"Idempotency-Key": "dir-zip-script"},
            files={
                "package": (
                    "script.zip",
                    _build_zip_package(
                        slug="co-zip-script",
                        name="公司脚本工具包",
                        description="含脚本",
                        with_script=True,
                    ),
                    "application/zip",
                )
            },
        ).json()
        super_client.patch(
            f"/api/v1/admin/skills/{zip_with_script['id']}",
            json={
                "expected_revision": 1,
                "current_version_id": zip_with_script["versions"][0]["id"],
                "enabled": True,
                "script_requires_approval": True,
            },
        )
        harness.advance_clock(10)

        # 2) 公司级 ZIP 纯文档（无脚本，设为推荐）
        zip_doc_only = super_client.post(
            "/api/v1/admin/skills/import",
            headers={"Idempotency-Key": "dir-zip-doc"},
            files={
                "package": (
                    "doc.zip",
                    _build_zip_package(
                        slug="co-zip-doc",
                        name="公司纯文档 ZIP",
                        description="无脚本",
                        with_script=False,
                    ),
                    "application/zip",
                )
            },
        ).json()
        super_client.patch(
            f"/api/v1/admin/skills/{zip_doc_only['id']}",
            json={
                "expected_revision": 1,
                "current_version_id": zip_doc_only["versions"][0]["id"],
                "enabled": True,
                "script_requires_approval": False,
                "featured": True,
                "starter_prompt": "请告诉我你想查询的公司制度主题。",
            },
        )
        harness.advance_clock(10)

        # 3) 财务部在线 Markdown 技能（推荐）
        dept_md = fin_admin.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "dir-dept-md"},
            json={
                "name": "财务报销智能初审",
                "description": "财务部专属在线 Markdown 技能",
                "guide": "# 财务初审指南\n请逐项核对金额、税率与审批链。",
                "scope": "department",
                "owner_department_id": "dept_fin",
                "featured": True,
                "starter_prompt": "请上传报销单 PDF 或截图。",
            },
        ).json()
        harness.advance_clock(10)

        # 4) 财务员工个人 Markdown 技能（恒为 format='markdown', featured=False）
        personal_md = fin_member.post(
            "/api/v1/me/skills",
            headers={"Idempotency-Key": "dir-me-md"},
            json={
                "name": "个人周报模板",
                "description": "我自己的周报风格",
                "guide": "每周五总结重点成果。",
            },
        ).json()
        assert personal_md["format"] == "markdown"
        assert personal_md["has_scripts"] is False
        assert personal_md["featured"] is False
        assert personal_md["starter_prompt"] is None

        # 5) 核查 GET /api/v1/admin/skills 字段投影（超管可见全部 3 个非个人技能）
        admin_items = {
            item["id"]: item for item in super_client.get("/api/v1/admin/skills").json()["items"]
        }
        assert admin_items[zip_with_script["id"]]["format"] == "package"
        assert admin_items[zip_with_script["id"]]["has_scripts"] is True
        assert admin_items[zip_with_script["id"]]["featured"] is False

        assert admin_items[zip_doc_only["id"]]["format"] == "package"
        assert admin_items[zip_doc_only["id"]]["has_scripts"] is False
        assert admin_items[zip_doc_only["id"]]["featured"] is True
        assert admin_items[zip_doc_only["id"]]["starter_prompt"] == (
            "请告诉我你想查询的公司制度主题。"
        )

        assert admin_items[dept_md["id"]]["format"] == "markdown"
        assert admin_items[dept_md["id"]]["has_scripts"] is False
        assert admin_items[dept_md["id"]]["featured"] is True
        assert admin_items[dept_md["id"]]["starter_prompt"] == "请上传报销单 PDF 或截图。"

        # 6) 核查 GET /api/v1/skills 完整字段投影与稳定排序：
        #    featured DESC -> department > company > personal -> updated_at DESC
        user_items = fin_member.get("/api/v1/skills").json()["items"]
        assert [item["id"] for item in user_items] == [
            dept_md["id"],          # featured=True, department
            zip_doc_only["id"],     # featured=True, company
            zip_with_script["id"],  # featured=False, company
            personal_md["id"],      # featured=False, personal
        ]
        by_id = {item["id"]: item for item in user_items}
        assert by_id[dept_md["id"]]["format"] == "markdown"
        assert by_id[dept_md["id"]]["has_scripts"] is False
        assert by_id[dept_md["id"]]["featured"] is True
        assert by_id[dept_md["id"]]["starter_prompt"] == "请上传报销单 PDF 或截图。"
        assert by_id[zip_with_script["id"]]["format"] == "package"
        assert by_id[zip_with_script["id"]]["has_scripts"] is True
        assert by_id[zip_doc_only["id"]]["has_scripts"] is False

        # 7) 普通财务员工可直接将新创建的在线部门 Markdown 技能挂载进 Run 快照并解析指南；
        #    无权研发员工尝试选中该财务部门技能必须被权限快照拒绝。
        async def _verify_run_mounting() -> None:
            async with harness.session_factory() as session:
                snapshot = await build_run_skill_snapshot(
                    session,
                    selected_skill_ids=(dept_md["id"],),
                    owner_id="usr_fin_member",
                )
            assert snapshot.selected_skill_ids == (dept_md["id"],)
            prompt_ctx = await harness.skill_service.prompt_context(
                snapshot,
                active_skill_ids=(dept_md["id"],),
            )
            assert len(prompt_ctx.active) == 1
            assert prompt_ctx.active[0].skill_id == dept_md["id"]
            assert prompt_ctx.active[0].scope is SkillScope.DEPARTMENT
            assert "逐项核对金额、税率与审批链" in prompt_ctx.active[0].guide

            async with harness.session_factory() as session:
                with pytest.raises(SkillConflictError, match="skill_selection_unavailable"):
                    await build_run_skill_snapshot(
                        session,
                        selected_skill_ids=(dept_md["id"],),
                        owner_id="usr_rd_member",
                    )

            # 直接复核仓储级格式锁：即使绕过服务预检直接调用仓储，
            # 也绝不允许对 ZIP 包发布 Markdown 版本
            with pytest.raises(SkillConflictError, match="skill_markdown_edit_unsupported"):
                await harness.skill_repo.publish_admin_markdown_version(
                    zip_with_script["id"],
                    version_id="skillver_direct_blocked",
                    expected_revision=2,
                    label="v2",
                    activate=True,
                    manifest=await _load_manifest(
                        harness.skill_repo, dept_md["current_version_id"]
                    ),
                    object_key="skills/xx/blocked.zip",
                    package_sha256="a" * 64,
                    dependency_check=DependencyCheck(
                        status=DependencyCheckStatus.SATISFIED,
                        python_version="not-checked",
                        python_implementation="markdown-only",
                        items=(),
                        checked_at=NOW,
                    ),
                    actor=await _auth_context(
                        harness.session_factory,
                        harness.auth_repo,
                        "usr_super",
                    ),
                    idempotency_key="repo-direct-format-lock",
                    request_fingerprint="f" * 64,
                    created_at=NOW,
                )

        asyncio.run(_verify_run_mounting())
    finally:
        harness.close()


async def _load_manifest(repo: SqlAlchemySkillRepository, version_id: str) -> Any:
    version = await repo.get_version(version_id)
    assert version is not None
    return version.manifest


async def _auth_context(
    session_factory: async_sessionmaker[AsyncSession],
    auth_repo: SqlAlchemyAuthRepository,
    user_id: str,
) -> Any:
    from domain.auth import AuthContext

    user = await auth_repo.get_user_by_id(user_id)
    assert user is not None
    async with session_factory() as session:
        session_row = await session.scalar(
            select(AuthSessionModel)
            .where(
                AuthSessionModel.user_id == user_id,
                AuthSessionModel.revoked_time.is_(None),
            )
            .order_by(AuthSessionModel.created_at.desc())
        )
    assert session_row is not None
    return AuthContext(user=user, session_id=session_row.id)


def test_skill_format_enum_values() -> None:
    """确认 SkillFormat 枚举稳定值与数据库约束字面量完全一致。"""
    assert SkillFormat.MARKDOWN.value == "markdown"
    assert SkillFormat.PACKAGE.value == "package"


# ---------------------------------------------------------------------------
# 8. 反向格式锁：禁止向在线 Markdown 技能上传 ZIP 包 + ZIP add_version 幂等重放回归
# ---------------------------------------------------------------------------


def test_markdown_skill_rejects_zip_version_upload_and_zip_replay_returns_request_version(
    tmp_path: Path,
) -> None:
    """验证反向格式锁（服务预检与事务内双重拦截、零孤儿）及 ZIP add_version 幂等重放准确性。"""
    harness = _M33Harness(tmp_path)
    try:
        super_client = harness.login_client("super@acme.com")

        # 1) 创建在线 Markdown 技能，尝试通过 ZIP 接口 POST .../versions 上传新版本 -> 409 且零孤儿
        md_created = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "rev-lock-md-1"},
            json={
                "name": "纯文档合规手册",
                "description": "在线 Markdown 形态",
                "guide": "# 合规指南\n请严格遵守。",
                "slug": "md-compliance-guide",
            },
        )
        assert md_created.status_code == 201, md_created.text
        md_skill_id = md_created.json()["id"]
        md_v1_id = md_created.json()["current_version_id"]

        objs_before = len(harness.object_store.objects)
        deleted_before = len(harness.object_store.deleted_keys)
        zip_for_md = _build_zip_package(
            slug="md-compliance-guide",
            name="试图塞入脚本的 ZIP",
            description="应被反向格式锁拦截",
            with_script=True,
        )
        blocked_zip = super_client.post(
            f"/api/v1/admin/skills/{md_skill_id}/versions",
            headers={"Idempotency-Key": "illegal-zip-on-md"},
            files={"package": ("bad.zip", zip_for_md, "application/zip")},
        )
        assert blocked_zip.status_code == 409
        assert blocked_zip.json() == {"detail": "skill_package_upload_unsupported"}
        # 预检即拦截，对象存储既未新增也未产生孤儿清理
        assert len(harness.object_store.objects) == objs_before
        assert len(harness.object_store.deleted_keys) == deleted_before

        # 2) 直接绕过服务预检调用仓储 add_version，验证事务内 FOR UPDATE 二次防线同样拦截
        async def _verify_repo_reverse_lock() -> None:
            actor = await _auth_context(
                harness.session_factory,
                harness.auth_repo,
                "usr_super",
            )
            manifest = await _load_manifest(harness.skill_repo, md_v1_id)
            with pytest.raises(SkillConflictError, match="skill_package_upload_unsupported"):
                await harness.skill_repo.add_version(
                    md_skill_id,
                    version_id="skillver_repo_zip_blocked",
                    label="v2",
                    manifest=manifest,
                    object_key="skills/zz/blocked.zip",
                    package_sha256="b" * 64,
                    dependency_check=DependencyCheck(
                        status=DependencyCheckStatus.SATISFIED,
                        python_version="3.12.0",
                        python_implementation="CPython",
                        items=(),
                        checked_at=NOW,
                    ),
                    actor=actor,
                    idempotency_key="repo-reverse-lock-key",
                    request_fingerprint="c" * 64,
                    created_at=NOW,
                )

        asyncio.run(_verify_repo_reverse_lock())

        # 3) 回归校验：对已激活 v1 的 ZIP 技能包上传未激活 v2 并幂等重放，
        #    必须返回本次请求创建的 v2（request_version），而非当前激活的 v1。
        zip_v1_bytes = _build_zip_package(
            slug="zip-replay-check",
            name="ZIP 重放验证 v1",
            description="v1",
            with_script=True,
        )
        zip_v2_bytes = _build_zip_package(
            slug="zip-replay-check",
            name="ZIP 重放验证 v2",
            description="v2",
            with_script=True,
        )
        imp = super_client.post(
            "/api/v1/admin/skills/import",
            headers={"Idempotency-Key": "zip-replay-imp"},
            files={"package": ("v1.zip", zip_v1_bytes, "application/zip")},
        ).json()
        zip_skill_id = imp["id"]
        zip_v1_id = imp["versions"][0]["id"]
        super_client.patch(
            f"/api/v1/admin/skills/{zip_skill_id}",
            json={
                "expected_revision": 1,
                "current_version_id": zip_v1_id,
                "enabled": True,
                "script_requires_approval": True,
            },
        )
        added = super_client.post(
            f"/api/v1/admin/skills/{zip_skill_id}/versions",
            headers={"Idempotency-Key": "zip-replay-add-v2"},
            params={"label": "v2-pkg"},
            files={"package": ("v2.zip", zip_v2_bytes, "application/zip")},
        )
        assert added.status_code == 201, added.text
        v2_id = added.json()["id"]
        assert v2_id != zip_v1_id

        replayed_add = super_client.post(
            f"/api/v1/admin/skills/{zip_skill_id}/versions",
            headers={"Idempotency-Key": "zip-replay-add-v2"},
            params={"label": "v2-pkg"},
            files={"package": ("v2.zip", zip_v2_bytes, "application/zip")},
        )
        assert replayed_add.status_code == 201
        assert replayed_add.json()["id"] == v2_id
        assert replayed_add.json()["label"] == "v2-pkg"
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# 9. UploadAdmissionMiddleware 对 3 个管理端 Markdown 写端点的流式限流保护
# ---------------------------------------------------------------------------


def test_upload_admission_middleware_guards_admin_markdown_endpoints() -> None:
    """验证 3 个管理端 Markdown 端点正确接入流式上传限流策略并在超大请求体时熔断 413。"""
    parse_policy = match_upload_policy("POST", "/api/v1/admin/skills/parse-markdown")
    assert parse_policy is not None
    assert parse_policy.kind == "personal_markdown"
    assert parse_policy.overflow_code == "skill_markdown_too_large"
    assert parse_policy.maximum_bytes == 1024 * 1024 + 64 * 1024

    create_policy = match_upload_policy("POST", "/api/v1/admin/skills/markdown")
    assert create_policy is not None
    assert create_policy.kind == "personal_json"
    assert create_policy.overflow_code == "skill_personal_request_too_large"
    assert create_policy.maximum_bytes == 2 * 1024 * 1024

    version_policy = match_upload_policy("POST", "/api/v1/admin/skills/sk_abc/versions/markdown")
    assert version_policy is not None
    assert version_policy.kind == "personal_json"
    assert version_policy.overflow_code == "skill_personal_request_too_large"
    assert version_policy.maximum_bytes == 2 * 1024 * 1024

    # 原 ZIP 版本上传路由不受影响，仍命中 skill 策略
    zip_policy = match_upload_policy("POST", "/api/v1/admin/skills/sk_abc/versions")
    assert zip_policy is not None
    assert zip_policy.kind == "skill"

    async def _run_streaming_limit_checks() -> None:
        downstream_called = 0

        async def _downstream(
            scope: dict[str, object],
            receive: Any,
            send: Any,
        ) -> None:
            nonlocal downstream_called
            del scope
            downstream_called += 1
            while True:
                event_msg = await receive()
                if not event_msg.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = UploadAdmissionMiddleware(_downstream)

        for path, total_bytes, expected_code in (
            (
                "/api/v1/admin/skills/parse-markdown",
                1024 * 1024 + 64 * 1024 + 1,
                "skill_markdown_too_large",
            ),
            (
                "/api/v1/admin/skills/markdown",
                2 * 1024 * 1024 + 1,
                "skill_personal_request_too_large",
            ),
            (
                "/api/v1/admin/skills/sk_abc/versions/markdown",
                2 * 1024 * 1024 + 1,
                "skill_personal_request_too_large",
            ),
        ):
            # 1) 携带 Content-Length 快速拒绝：不调用下游
            before_calls = downstream_called
            sent_cl: list[dict[str, object]] = []

            async def _recv_unused() -> dict[str, object]:
                return {"type": "http.request", "body": b"", "more_body": False}

            async def _send_cl(
                msg: dict[str, object],
                out: list[dict[str, object]] = sent_cl,
            ) -> None:
                out.append(msg)

            await middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "headers": [(b"content-length", str(total_bytes).encode("ascii"))],
                },
                _recv_unused,
                _send_cl,
            )
            assert downstream_called == before_calls
            assert sent_cl[0]["status"] == 413
            assert sent_cl[1]["body"] == f'{{"detail":"{expected_code}"}}'.encode()

            # 2) 无 Content-Length 分块流式超限：中途熔断返回 413
            chunk_size = 512 * 1024
            chunks: list[bytes] = []
            remaining = total_bytes
            while remaining > 0:
                piece = min(chunk_size, remaining)
                chunks.append(b"a" * piece)
                remaining -= piece
            chunk_iter = iter(chunks)
            sent_stream: list[dict[str, object]] = []

            async def _recv_stream(it: Any = chunk_iter) -> dict[str, object]:
                try:
                    data = next(it)
                    return {"type": "http.request", "body": data, "more_body": True}
                except StopIteration:
                    return {"type": "http.request", "body": b"", "more_body": False}

            async def _send_stream(
                msg: dict[str, object], out: list[dict[str, object]] = sent_stream
            ) -> None:
                out.append(msg)

            await middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "headers": [],
                },
                _recv_stream,
                _send_stream,
            )
            assert sent_stream[0]["status"] == 413
            assert sent_stream[1]["body"] == f'{{"detail":"{expected_code}"}}'.encode()

    asyncio.run(_run_streaming_limit_checks())


def test_markdown_skills_work_without_minio_while_zip_and_log_return_503(
    tmp_path: Path,
) -> None:
    """无 MinIO（object_store=None）时纯 Markdown 全流程可用，ZIP 导入/资源读取均返回 503。"""
    harness = _M33Harness(tmp_path, with_object_store=False)
    try:
        super_client = harness.login_client("super@acme.com")
        fin_member = harness.login_client("fin_member@acme.com")

        # 1) 管理端纯 Markdown 创建 + 发布新版本 + 用户目录读取全部成功。
        created_admin = super_client.post(
            "/api/v1/admin/skills/markdown",
            headers={"Idempotency-Key": "no-minio-admin-1"},
            json={
                "name": "无 MinIO 公司指南",
                "description": "脱离对象存储独立可用",
                "guide": "# 指南 v1\n纯数据库持久化。",
                "scope": "company",
                "enabled": True,
            },
        )
        assert created_admin.status_code == 201, created_admin.text
        admin_skill_id = created_admin.json()["id"]

        published_admin = super_client.post(
            f"/api/v1/admin/skills/{admin_skill_id}/versions/markdown",
            headers={"Idempotency-Key": "no-minio-admin-v2"},
            json={
                "name": "无 MinIO 公司指南",
                "description": "脱离对象存储独立可用",
                "guide": "# 指南 v2\n编辑发版零对象存储。",
                "expected_revision": 1,
                "activate": True,
            },
        )
        assert published_admin.status_code == 201, published_admin.text

        # 2) 普通员工个人 Markdown 创建 + 编辑 + 目录读取全部成功。
        created_personal = fin_member.post(
            "/api/v1/me/skills",
            headers={"Idempotency-Key": "no-minio-personal-1"},
            json={
                "name": "无 MinIO 个人周报",
                "description": "个人纯 Markdown 技能",
                "guide": "# 个人周报\n只写事实。",
            },
        )
        assert created_personal.status_code == 201, created_personal.text
        personal_id = created_personal.json()["id"]

        directory = fin_member.get("/api/v1/skills")
        assert directory.status_code == 200
        dir_ids = {item["id"] for item in directory.json()["items"]}
        assert {admin_skill_id, personal_id} <= dir_ids

        # 3) 通过 Run 快照读取纯 Markdown 的 SKILL.md 零对象存储直出。
        async def _verify_read_without_minio() -> None:
            snapshot = await harness.skill_service.build_run_snapshot(
                "usr_fin_member",
                (admin_skill_id, personal_id),
            )
            sliced = await harness.skill_service.read_resource(
                snapshot,
                skill_id=admin_skill_id,
                path="SKILL.md",
                offset=0,
                limit_bytes=1024,
            )
            assert sliced.content == "# 指南 v2\n编辑发版零对象存储。"
            assert sliced.eof is True

        asyncio.run(_verify_read_without_minio())

        # 4) ZIP 包导入在无对象存储时返回 503 file_storage_unavailable。
        zip_bytes = _build_zip_package(
            slug="no-minio-zip-blocked",
            name="需要 MinIO 的脚本包",
            description="应返回 503",
            with_script=True,
        )
        zip_resp = super_client.post(
            "/api/v1/admin/skills/import",
            headers={"Idempotency-Key": "no-minio-zip-1"},
            files={"package": ("blocked.zip", zip_bytes, "application/zip")},
        )
        assert zip_resp.status_code == 503
        assert zip_resp.json() == {"detail": "file_storage_unavailable"}

        # 5) 存量 ZIP 技能追加版本、读取 ZIP 资源与下载脚本日志在无 MinIO 时均返回 503。
        async def _seed_zip_and_log_and_verify_503() -> tuple[str, str]:
            actor = await _auth_context(
                harness.session_factory,
                harness.auth_repo,
                "usr_super",
            )
            pkg_manifest = SkillPackageManifest(
                slug="legacy-zip-skill",
                name="存量脚本包",
                description="用于校验无 MinIO 时 ZIP 路径返回 503",
                guide="# 指南",
                files=(
                    PackageResource(
                        path="SKILL.md",
                        size_bytes=8,
                        sha256="a" * 64,
                        text=True,
                    ),
                ),
            )
            dep = DependencyCheck(
                status=DependencyCheckStatus.SATISFIED,
                python_version="3.12",
                python_implementation="cpython",
                items=(),
                checked_at=NOW,
            )
            zip_skill, zip_version = await harness.skill_repo.create_skill(
                skill_id="skill_legacy_zip",
                version_id="skillver_legacy_zip",
                label="v1",
                manifest=pkg_manifest,
                object_key="skills/aa/legacy.zip",
                package_sha256="b" * 64,
                dependency_check=dep,
                actor=actor,
                idempotency_key="seed-legacy-zip",
                request_fingerprint="f" * 64,
                created_at=NOW,
                format=SkillFormat.PACKAGE,
                enabled=True,
            )
            zip_snapshot = RunSkillSnapshot(
                skills=(
                    RunSkillEntry(
                        skill_id=zip_skill.id,
                        slug=zip_skill.slug,
                        name=zip_skill.name,
                        description=zip_skill.description,
                        version_id=zip_version.id,
                        version_label=zip_version.label,
                        requires_approval=False,
                    ),
                ),
                selected_skill_ids=(zip_skill.id,),
            )
            with pytest.raises(FileStorageError, match="file_storage_unavailable"):
                await harness.skill_service.read_resource(
                    zip_snapshot,
                    skill_id=zip_skill.id,
                    path="SKILL.md",
                    offset=0,
                    limit_bytes=64,
                )
            return zip_skill.id, zip_version.id

        legacy_zip_id, _ = asyncio.run(_seed_zip_and_log_and_verify_503())
        add_zip_resp = super_client.post(
            f"/api/v1/admin/skills/{legacy_zip_id}/versions",
            headers={"Idempotency-Key": "no-minio-add-ver-1"},
            files={
                "package": (
                    "v2.zip",
                    _build_zip_package(
                        slug="legacy-zip-skill",
                        name="存量脚本包 v2",
                        description="应返回 503",
                        with_script=False,
                    ),
                    "application/zip",
                )
            },
        )
        assert add_zip_resp.status_code == 503
        assert add_zip_resp.json() == {"detail": "file_storage_unavailable"}

        # 模拟存在脚本执行日志记录时下载日志也明确返回 503 file_storage_unavailable。
        async def _verify_admin_log_503() -> None:
            from domain.skill import SkillExecutionRecord, SkillExecutionStatus

            original_get_exec = harness.skill_repo.get_admin_execution

            async def fake_exec(call_id: str) -> SkillExecutionRecord | None:
                del call_id
                return SkillExecutionRecord(
                    id="skexec_1",
                    run_id="run_1",
                    session_id="ses_1",
                    tool_call_record_id="call_log_503",
                    skill_id=legacy_zip_id,
                    skill_version_id="skillver_legacy_zip",
                    script_path="scripts/run.py",
                    input_file_ids=(),
                    parameters_sha256="a" * 64,
                    python_version="3.12",
                    dependency_check=DependencyCheck(
                        status=DependencyCheckStatus.SATISFIED,
                        python_version="3.12",
                        python_implementation="cpython",
                        items=(),
                        checked_at=NOW,
                    ),
                    status=SkillExecutionStatus.SUCCEEDED,
                    log_object_key="skill-logs/aa/test.log",
                    log_truncated=False,
                    log_error_code=None,
                    error_code=None,
                    cleanup_error_code=None,
                    result_artifacts=(),
                    created_at=NOW,
                    finished_at=NOW,
                )

            harness.skill_repo.get_admin_execution = fake_exec  # type: ignore[method-assign]
            try:
                actor = await _auth_context(
                    harness.session_factory,
                    harness.auth_repo,
                    "usr_super",
                )
                with pytest.raises(FileStorageError, match="file_storage_unavailable"):
                    await harness.skill_service.admin_log(actor, "call_log_503")
            finally:
                harness.skill_repo.get_admin_execution = original_get_exec  # type: ignore[method-assign]

        asyncio.run(_verify_admin_log_503())
    finally:
        harness.close()
