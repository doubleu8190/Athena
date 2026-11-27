"""非生产本地 Mock 模式、超管组织与用户分权管理 API 及 Alembic 基线迁移测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from auth_support import FakePasswordHasher
from fastapi.testclient import TestClient
from pg_unit_harness import create_pg_unit_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.model import AuthSessionModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.org import DepartmentModel, SqlAlchemyOrgRepository
from app.auth import AuthService
from app.org import (
    MOCK_DEPT_FIN_ID,
    MOCK_DEPT_HR_ID,
    MOCK_DEPT_PLATFORM_ID,
    MOCK_DEPT_RD_ID,
    MOCK_DEPT_ROOT_ID,
    FeishuOrgSyncService,
    OrgService,
    seed_mock_organization,
)
from bootstrap.api import build_api_dependencies
from domain.org import DepartmentStatus, RemoteDepartment, RemoteEmployee
from main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


async def _make_pg_db() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    return await create_pg_unit_db()


def _migration_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    return config


# ---------------------------------------------------------------------------
# 1. 本地 Mock 模式：生产强安全护栏、幂等种子、多角色快捷登录
# ---------------------------------------------------------------------------


def test_production_forbids_auth_mock_mode_before_allocating_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ATHENA_ENV=production 且 ATHENA_AUTH_MOCK_ENABLED=true 必须立即拒绝启动。"""
    monkeypatch.setenv("ATHENA_ENV", "production")
    monkeypatch.setenv("ATHENA_AUTH_MOCK_ENABLED", "true")
    monkeypatch.setenv("ATHENA_REDIS_URL", "redis://127.0.0.1:6379/0")

    with pytest.raises(RuntimeError, match="auth_mock_forbidden_in_production"):
        build_api_dependencies()

    with pytest.raises(RuntimeError, match="auth_mock_forbidden_in_production"):
        create_app()


def test_mock_mode_seeds_idempotently_and_issues_real_cookie_sessions() -> None:
    """非生产 Mock 种子幂等预置 5 个部门与 6 个典型角色账号，并签发真实 HttpOnly Cookie。"""
    now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    engine, session_factory = _run(_make_pg_db())
    try:
        auth_repo = SqlAlchemyAuthRepository(session_factory)
        org_repo = SqlAlchemyOrgRepository(session_factory)
        # 连续两次预置验证幂等性。
        _run(seed_mock_organization(org_repo, clock=lambda: now))
        _run(seed_mock_organization(org_repo, clock=lambda: now))

        auth_service = AuthService(
            auth_repo,
            FakePasswordHasher(),
            org_reader=org_repo,
            mock_enabled=True,
            clock=lambda: now,
        )
        org_service = OrgService(org_repo, auth_repo, clock=lambda: now)
        app = create_app(auth_service=auth_service, org_service=org_service)

        with TestClient(app) as client:
            providers = client.get("/api/v1/auth/providers")
            assert providers.status_code == 200
            assert providers.json() == {
                "feishu_enabled": False,
                "local_login_enabled": True,
                "mock_enabled": True,
            }

            users_resp = client.get("/api/v1/auth/mock/users")
            assert users_resp.status_code == 200
            items = users_resp.json()["items"]
            assert len(items) == 6
            by_id = {item["id"]: item for item in items}
            assert set(by_id.keys()) == {
                "mock_super_admin",
                "mock_fin_admin",
                "mock_rd_admin",
                "mock_fin_member",
                "mock_rd_member",
                "mock_hr_member",
            }
            assert by_id["mock_rd_admin"]["role"] == "dept_admin"
            assert by_id["mock_rd_admin"]["managed_department_ids"] == [MOCK_DEPT_RD_ID]
            assert by_id["mock_rd_member"]["departments"][0]["id"] == MOCK_DEPT_PLATFORM_ID
            assert by_id["mock_rd_member"]["departments"][0]["path"] == (
                f"/{MOCK_DEPT_ROOT_ID}/{MOCK_DEPT_RD_ID}/{MOCK_DEPT_PLATFORM_ID}/"
            )

            # 跨站 Origin 触发 403。
            csrf_denied = client.post(
                "/api/v1/auth/mock/login",
                headers={"Origin": "https://evil.example.com"},
                json={"user_id": "mock_rd_admin"},
            )
            assert csrf_denied.status_code == 403

            # 不存在的 Mock 账号返回 404。
            missing = client.post(
                "/api/v1/auth/mock/login",
                json={"user_id": "nonexistent_mock"},
            )
            assert missing.status_code == 404
            assert missing.json() == {"detail": "mock_user_not_found"}

            # 快捷登录 mock_rd_admin，获得真实 HttpOnly 会话 Cookie，并通过 /auth/me 验证。
            login_resp = client.post(
                "/api/v1/auth/mock/login",
                json={"user_id": "mock_rd_admin"},
            )
            assert login_resp.status_code == 200
            assert "httponly" in login_resp.headers["set-cookie"].lower()
            me = client.get("/api/v1/auth/me")
            assert me.status_code == 200
            assert me.json()["id"] == "mock_rd_admin"
            assert me.json()["role"] == "dept_admin"
            assert me.json()["auth_provider"] == "mock"
            assert me.json()["managed_department_ids"] == [MOCK_DEPT_RD_ID]

            # Mock 账号不支持修改密码。
            pwd_resp = client.post(
                "/api/v1/auth/password",
                json={
                    "current_password": "any-password-here",
                    "new_password": "new-password-here",
                },
            )
            assert pwd_resp.status_code == 400
            assert pwd_resp.json() == {"detail": "password_change_not_supported"}

            # dept_admin 访问超管组织管理接口必须返回 403 admin_required。
            denied_org = client.get("/api/v1/admin/org/departments")
            assert denied_org.status_code == 403
            assert denied_org.json() == {"detail": "admin_required"}
    finally:
        _run(engine.dispose())


# ---------------------------------------------------------------------------
# 2. 超管组织与用户分权管理 API (/api/v1/admin/org)
# ---------------------------------------------------------------------------


def test_admin_org_api_departments_users_role_status_and_last_super_admin_protection() -> None:
    """覆盖部门查询、子部门前缀过滤、任命/撤销 dept_admin、停用吊销会话与最后超管保护。"""
    now = datetime(2026, 11, 23, 11, 0, tzinfo=UTC)
    engine, session_factory = _run(_make_pg_db())
    try:
        auth_repo = SqlAlchemyAuthRepository(session_factory)
        org_repo = SqlAlchemyOrgRepository(session_factory)
        _run(seed_mock_organization(org_repo, clock=lambda: now))

        auth_service = AuthService(
            auth_repo,
            FakePasswordHasher(),
            org_reader=org_repo,
            mock_enabled=True,
            clock=lambda: now,
        )
        org_service = OrgService(org_repo, auth_repo, clock=lambda: now)
        app = create_app(auth_service=auth_service, org_service=org_service)

        with TestClient(app) as admin_client, TestClient(app) as member_client:
            # 匿名请求返回 401。
            assert admin_client.get("/api/v1/admin/org/departments").status_code == 401

            # 分别登录超管与普通员工。
            assert (
                admin_client.post(
                    "/api/v1/auth/mock/login", json={"user_id": "mock_super_admin"}
                ).status_code
                == 200
            )
            assert (
                member_client.post(
                    "/api/v1/auth/mock/login", json={"user_id": "mock_hr_member"}
                ).status_code
                == 200
            )
            assert member_client.get("/api/v1/admin/org/users").status_code == 403

            # 1. 部门列表按 path 排序返回全部 5 个活跃部门。
            depts_resp = admin_client.get("/api/v1/admin/org/departments")
            assert depts_resp.status_code == 200
            dept_items = depts_resp.json()["items"]
            assert len(dept_items) == 5
            assert [d["id"] for d in dept_items] == sorted(
                [d["id"] for d in dept_items],
                key=lambda did: next(item["path"] for item in dept_items if item["id"] == did),
            )

            # 2. 按部门子树筛选：研发部含子部门 -> 研发主管 + 平台架构组研发员工（2 人）；
            #    不含子部门 -> 仅研发主管（1 人）。
            rd_tree = admin_client.get(
                "/api/v1/admin/org/users",
                params={"department_id": MOCK_DEPT_RD_ID, "include_subdepartments": "true"},
            )
            assert rd_tree.status_code == 200
            assert rd_tree.json()["total"] == 2
            assert {u["id"] for u in rd_tree.json()["items"]} == {
                "mock_rd_admin",
                "mock_rd_member",
            }

            rd_direct = admin_client.get(
                "/api/v1/admin/org/users",
                params={"department_id": MOCK_DEPT_RD_ID, "include_subdepartments": "false"},
            )
            assert rd_direct.status_code == 200
            assert rd_direct.json()["total"] == 1
            assert rd_direct.json()["items"][0]["id"] == "mock_rd_admin"

            # 3. 角色、状态与关键词（工号/邮箱/姓名）筛选及分页。
            by_q = admin_client.get(
                "/api/v1/admin/org/users",
                params={"role": "member", "status": "active", "q": "MOCK006"},
            )
            assert by_q.status_code == 200
            assert by_q.json()["total"] == 1
            assert by_q.json()["items"][0]["id"] == "mock_hr_member"

            # 4. 任命 dept_admin 校验：空部门列表 422、不存在部门 422、归档部门 422、重复部门 422。
            empty_scope = admin_client.put(
                "/api/v1/admin/org/users/mock_hr_member/role",
                json={"role": "dept_admin", "managed_departments": []},
            )
            assert empty_scope.status_code == 422
            assert empty_scope.json() == {"detail": "dept_admin_departments_required"}

            missing_dept = admin_client.put(
                "/api/v1/admin/org/users/mock_hr_member/role",
                json={
                    "role": "dept_admin",
                    "managed_departments": [{"department_id": "dept_missing"}],
                },
            )
            assert missing_dept.status_code == 422
            assert missing_dept.json() == {"detail": "department_not_found"}

            dup_dept = admin_client.put(
                "/api/v1/admin/org/users/mock_hr_member/role",
                json={
                    "role": "dept_admin",
                    "managed_departments": [
                        {"department_id": MOCK_DEPT_HR_ID, "include_subdepartments": True},
                        {"department_id": MOCK_DEPT_HR_ID, "include_subdepartments": False},
                    ],
                },
            )
            assert dup_dept.status_code == 422
            assert dup_dept.json() == {"detail": "duplicate_department_scope"}

            # 归档一个临时部门后不得作为 dept_admin 管辖范围。
            async def _add_archived_dept() -> None:
                async with session_factory() as s, s.begin():
                    s.add(
                        DepartmentModel(
                            id="dept_archived",
                            external_id="ext_archived",
                            parent_id=None,
                            name="已撤销部门",
                            path="/dept_archived/",
                            order_index=99,
                            status=DepartmentStatus.ARCHIVED.value,
                            synced_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )

            _run(_add_archived_dept())
            archived_scope = admin_client.put(
                "/api/v1/admin/org/users/mock_hr_member/role",
                json={
                    "role": "dept_admin",
                    "managed_departments": [{"department_id": "dept_archived"}],
                },
            )
            assert archived_scope.status_code == 422
            assert archived_scope.json() == {"detail": "department_not_found"}

            # 5. 合法任命 mock_hr_member 为 HR+财务部门管理员，随后降回 member 自动清空 scopes。
            promoted_dept_admin = admin_client.put(
                "/api/v1/admin/org/users/mock_hr_member/role",
                json={
                    "role": "dept_admin",
                    "managed_departments": [
                        {"department_id": MOCK_DEPT_HR_ID, "include_subdepartments": True},
                        {"department_id": MOCK_DEPT_FIN_ID, "include_subdepartments": False},
                    ],
                },
            )
            assert promoted_dept_admin.status_code == 200
            assert promoted_dept_admin.json()["role"] == "dept_admin"
            assert set(promoted_dept_admin.json()["managed_department_ids"]) == {
                MOCK_DEPT_HR_ID,
                MOCK_DEPT_FIN_ID,
            }
            # 被任命者 /auth/me 立即看到最新 role 与 managed_department_ids。
            hr_me = member_client.get("/api/v1/auth/me")
            assert hr_me.status_code == 200
            assert hr_me.json()["role"] == "dept_admin"
            assert set(hr_me.json()["managed_department_ids"]) == {
                MOCK_DEPT_HR_ID,
                MOCK_DEPT_FIN_ID,
            }

            demoted_member = admin_client.put(
                "/api/v1/admin/org/users/mock_hr_member/role",
                json={"role": "member", "managed_departments": []},
            )
            assert demoted_member.status_code == 200
            assert demoted_member.json()["role"] == "member"
            assert demoted_member.json()["managed_department_ids"] == []
            assert demoted_member.json()["managed_departments"] == []

            # 6. 保护系统最后一个活跃 super_admin：既禁止降权也禁止停用（409）。
            demote_last = admin_client.put(
                "/api/v1/admin/org/users/mock_super_admin/role",
                json={"role": "member"},
            )
            assert demote_last.status_code == 409
            assert demote_last.json() == {"detail": "last_super_admin_protected"}

            disable_last = admin_client.put(
                "/api/v1/admin/org/users/mock_super_admin/status",
                json={"status": "disabled"},
            )
            assert disable_last.status_code == 409
            assert disable_last.json() == {"detail": "last_super_admin_protected"}

            # 7. 停用普通用户同事务吊销其全部活跃会话，原 Cookie 立刻 401。
            disabled_hr = admin_client.put(
                "/api/v1/admin/org/users/mock_hr_member/status",
                json={"status": "disabled"},
            )
            assert disabled_hr.status_code == 200
            assert disabled_hr.json()["status"] == "disabled"
            assert member_client.get("/api/v1/auth/me").status_code == 401

            async def _hr_unrevoked_sessions() -> int:
                async with session_factory() as s:
                    rows = (
                        await s.scalars(
                            select(AuthSessionModel).where(
                                AuthSessionModel.user_id == "mock_hr_member",
                                AuthSessionModel.revoked_time.is_(None),
                            )
                        )
                    ).all()
                    return len(rows)

            assert _run(_hr_unrevoked_sessions()) == 0

            # 8. 新增第二位超管（自动清空原 dept_admin scopes）后，允许降权第一位超管。
            second_super = admin_client.put(
                "/api/v1/admin/org/users/mock_rd_admin/role",
                json={"role": "super_admin"},
            )
            assert second_super.status_code == 200
            assert second_super.json()["role"] == "super_admin"
            assert second_super.json()["managed_department_ids"] == []

            demote_first = admin_client.put(
                "/api/v1/admin/org/users/mock_super_admin/role",
                json={"role": "member"},
            )
            assert demote_first.status_code == 200
            assert demote_first.json()["role"] == "member"
            # 降权后原超管会话再调管理接口立刻返回 403。
            assert admin_client.get("/api/v1/admin/org/departments").status_code == 403

            # 9. 未启用飞书时手动触发同步返回 409 feishu_not_enabled。
            with TestClient(app) as second_admin_client:
                assert (
                    second_admin_client.post(
                        "/api/v1/auth/mock/login", json={"user_id": "mock_rd_admin"}
                    ).status_code
                    == 200
                )
                sync_disabled = second_admin_client.post("/api/v1/admin/org/sync")
                assert sync_disabled.status_code == 409
                assert sync_disabled.json() == {"detail": "feishu_not_enabled"}
    finally:
        _run(engine.dispose())


def test_admin_org_sync_endpoint_runs_feishu_sync_and_reports_cycle_error() -> None:
    """验证 POST /api/v1/admin/org/sync 在开启飞书时正常同步，并在发现环路时返回 422。"""
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

    class _DirectoryStub:
        def __init__(self) -> None:
            self.cycle = False

        async def list_departments(self) -> list[RemoteDepartment]:
            if self.cycle:
                return [
                    RemoteDepartment("od_a", "od_b", "环A"),
                    RemoteDepartment("od_b", "od_a", "环B"),
                ]
            return [RemoteDepartment("od_hq", None, "飞书总部", order_index=1)]

        async def list_department_users(self, department_external_id: str) -> list[RemoteEmployee]:
            if department_external_id == "od_hq":
                return [
                    RemoteEmployee(
                        union_id="on_1",
                        open_id="ou_1",
                        name="飞书员工1",
                        email="emp1@corp.com",
                        department_external_ids=("od_hq",),
                        is_active=True,
                    )
                ]
            return []

    engine, session_factory = _run(_make_pg_db())
    try:
        auth_repo = SqlAlchemyAuthRepository(session_factory)
        org_repo = SqlAlchemyOrgRepository(session_factory)
        auth_service = AuthService(
            auth_repo,
            FakePasswordHasher(),
            org_reader=org_repo,
            clock=lambda: now,
        )
        _run(
            auth_service.bootstrap_super_admin(
                "boss@corp.com",
                "super-admin-password-1",
            )
        )
        stub = _DirectoryStub()
        sync_service = FeishuOrgSyncService(stub, org_repo, clock=lambda: now)
        org_service = OrgService(
            org_repo,
            auth_repo,
            feishu_sync_service=sync_service,
            clock=lambda: now,
        )
        app = create_app(auth_service=auth_service, org_service=org_service)

        with TestClient(app) as client:
            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"email": "boss@corp.com", "password": "super-admin-password-1"},
                ).status_code
                == 200
            )
            ok = client.post("/api/v1/admin/org/sync")
            assert ok.status_code == 200
            assert ok.json()["departments_upserted"] == 1
            assert ok.json()["users_upserted"] == 1

            stub.cycle = True
            cycle_resp = client.post("/api/v1/admin/org/sync")
            assert cycle_resp.status_code == 422
            assert cycle_resp.json() == {"detail": "department_cycle_detected"}
    finally:
        _run(engine.dispose())


# ---------------------------------------------------------------------------
# 3. Alembic 基线单 head 与组织/身份离线 upgrade/downgrade SQL 验证
# ---------------------------------------------------------------------------


def test_alembic_initial_schema_single_head_and_org_offline_sql() -> None:
    """验证 Alembic 单 head 及离线 SQL 包含完整组织与身份表结构。"""
    config = _migration_config()
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["0002_add_mcp_tool_result_labels"]
    rev = script.get_revision("0002_add_mcp_tool_result_labels")
    assert rev is not None
    assert rev.down_revision == "0001_initial_schema"

    upgrade_buf = StringIO()
    with redirect_stdout(upgrade_buf):
        command.upgrade(config, "head", sql=True)
    upgrade_sql = upgrade_buf.getvalue()

    for expected in (
        "CREATE UNIQUE INDEX uq_users_email_active ON users (email) "
        "WHERE email IS NOT NULL AND deleted_time IS NULL",
        "role IN ('super_admin', 'dept_admin', 'member')",
        "auth_provider VARCHAR(16) DEFAULT 'local' NOT NULL",
        "external_union_id VARCHAR(128)",
        "external_open_id VARCHAR(128)",
        "employee_no VARCHAR(64)",
        "avatar_url VARCHAR(1024)",
        "job_title VARCHAR(120)",
        "last_synced_at TIMESTAMP WITH TIME ZONE",
        "auth_provider IN ('local', 'feishu', 'mock')",
        "(auth_provider != 'local') OR (password_hash IS NOT NULL)",
        "CREATE UNIQUE INDEX uq_users_external_union_id ON users (external_union_id) "
        "WHERE external_union_id IS NOT NULL AND deleted_time IS NULL",
        "CREATE UNIQUE INDEX uq_users_external_open_id ON users (external_open_id) "
        "WHERE external_open_id IS NOT NULL AND deleted_time IS NULL",
        "CREATE TABLE departments",
        "CREATE INDEX ix_departments_path ON departments (path)",
        "CREATE TABLE user_departments",
        "CREATE INDEX ix_user_departments_department ON user_departments (department_id, user_id)",
        "CREATE TABLE dept_admin_scopes",
        "CREATE INDEX ix_dept_admin_scopes_dept ON dept_admin_scopes (department_id)",
    ):
        assert expected in upgrade_sql, f"missing upgrade SQL fragment: {expected}"

    downgrade_buf = StringIO()
    with redirect_stdout(downgrade_buf):
        command.downgrade(config, "0002_add_mcp_tool_result_labels:base", sql=True)
    downgrade_sql = downgrade_buf.getvalue()
    for expected in (
        "DROP TABLE dept_admin_scopes",
        "DROP TABLE user_departments",
        "DROP TABLE departments",
        "DROP TABLE users",
    ):
        assert expected in downgrade_sql, f"missing downgrade SQL fragment: {expected}"
