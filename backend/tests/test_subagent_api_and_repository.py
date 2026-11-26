"""Subagent HTTP API 与 SQLAlchemy 仓储集成测试（基于 PostgreSQL 单元测试底座）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pg_unit_harness import create_pg_unit_db
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from adapter.access.repository import SqlAlchemyAccessControlRepository
from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.llm.model import ModelProfileVersionModel
from adapter.models import Base
from adapter.subagent import SqlAlchemySubagentModelChecker, SqlAlchemySubagentRepository
from app.access import AccessPolicyService
from app.auth.service import AuthService
from app.subagent import DefaultSubagentToolCatalog, SubagentService
from domain.auth import AuthContext, UserRole, UserStatus
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID
from infrastructure.security.password import Argon2PasswordHasher
from main import create_app

# 使用相对当前 UTC 时间构造测试认证会话有效期，避免日期推移后会话过期导致回归老化。
NOW = datetime.now(UTC)


async def _build_subagent_app() -> tuple[
    AsyncEngine, FastAPI, async_sessionmaker[AsyncSession], str, str
]:
    """初始化隔离 PostgreSQL 数据库、管理员与普通用户会话及 FastAPI 应用。"""
    engine, session_factory = await create_pg_unit_db()

    async with session_factory() as session:
        async with session.begin():
            session.add_all(
                [
                    UserModel(
                        id="usr_admin",
                        email="admin@example.com",
                        password_hash="unused",
                        display_name="Admin",
                        role=UserRole.SUPER_ADMIN.value,
                        status=UserStatus.ACTIVE.value,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    UserModel(
                        id="usr_member",
                        email="member@example.com",
                        password_hash="unused",
                        display_name="Member",
                        role=UserRole.MEMBER.value,
                        status=UserStatus.ACTIVE.value,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    ModelProfileVersionModel(
                        id="mp_active",
                        name="GPT-4o",
                        protocol="chat_completions",
                        base_url="https://api.example.com/v1",
                        model="gpt-4o",
                        api_key_ciphertext=b"ciphertext",
                        api_key_nonce=b"0123456789ab",
                        api_key_key_version=1,
                        context_window_tokens=128000,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        supports_parallel_tool_calls=True,
                        max_output_tokens=4096,
                        request_timeout_seconds=30,
                        execution_timeout_seconds=120,
                        max_concurrency=4,
                        enabled=True,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    ModelProfileVersionModel(
                        id="mp_disabled",
                        name="GPT-4o Mini",
                        protocol="chat_completions",
                        base_url="https://api.example.com/v1",
                        model="gpt-4o-mini",
                        api_key_ciphertext=b"ciphertext",
                        api_key_nonce=b"0123456789ab",
                        api_key_key_version=1,
                        context_window_tokens=128000,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        supports_parallel_tool_calls=True,
                        max_output_tokens=4096,
                        request_timeout_seconds=30,
                        execution_timeout_seconds=120,
                        max_concurrency=4,
                        enabled=False,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    AuthSessionModel(
                        id="sess_admin",
                        user_id="usr_admin",
                        token_hash="hash_admin",
                        expires_at=NOW + timedelta(days=7),
                        created_at=NOW,
                    ),
                    AuthSessionModel(
                        id="sess_member",
                        user_id="usr_member",
                        token_hash="hash_member",
                        expires_at=NOW + timedelta(days=7),
                        created_at=NOW,
                    ),
                ]
            )

    auth_repo = SqlAlchemyAuthRepository(session_factory)
    auth_service = AuthService(auth_repo, Argon2PasswordHasher())
    subagent_service = SubagentService(
        SqlAlchemySubagentRepository(session_factory),
        auth_repo,
        model_checker=SqlAlchemySubagentModelChecker(session_factory),
        tool_catalog=DefaultSubagentToolCatalog(),
        access_policy=AccessPolicyService(SqlAlchemyAccessControlRepository(session_factory)),
    )
    app = create_app(
        auth_service=auth_service,
        subagent_service=subagent_service,
        public_origin="http://testserver",
    )
    return engine, app, session_factory, "tok_admin", "tok_member"


def _admin_headers(origin: str = "http://testserver") -> dict[str, str]:
    return {"Origin": origin}


def test_subagent_metadata_and_run_reserved_columns() -> None:
    """验证 subagents 表及 runs 表预留字段已注册在 ORM metadata 中。"""
    assert "subagents" in Base.metadata.tables
    runs_table = Base.metadata.tables["runs"]
    runs_columns = set(runs_table.columns.keys())
    assert {
        "parent_run_id",
        "root_run_id",
        "parent_tool_call_id",
        "depth",
        "subagent_profile_id",
        "subagent_slug",
        "subagent_name",
    }.issubset(runs_columns)
    parent_run_type = runs_table.c.parent_run_id.type
    root_run_type = runs_table.c.root_run_id.type
    parent_tool_call_type = runs_table.c.parent_tool_call_id.type
    assert isinstance(parent_run_type, String)
    assert isinstance(root_run_type, String)
    assert isinstance(parent_tool_call_type, String)
    assert parent_run_type.length == 128
    assert root_run_type.length == 128
    assert parent_tool_call_type.length == 256


def test_subagent_admin_crud_lifecycle_and_errors() -> None:
    async def scenario() -> None:
        engine, app, session_factory, admin_token, member_token = await _build_subagent_app()

        async def fake_auth_token(token: str) -> AuthContext | None:
            auth_repo = SqlAlchemyAuthRepository(session_factory)
            if token == admin_token:
                user = await auth_repo.get_current_session_user("usr_admin", "sess_admin", NOW)
                assert user is not None
                return AuthContext(user=user, session_id="sess_admin")
            if token == member_token:
                user = await auth_repo.get_current_session_user("usr_member", "sess_member", NOW)
                assert user is not None
                return AuthContext(user=user, session_id="sess_member")
            return None

        try:
            with (
                patch.object(app.state.authenticator, "authenticate", side_effect=fake_auth_token),
                patch("adapter.auth.repository.transaction_guard.now_utc", return_value=NOW),
                patch("app.subagent.service.now_utc", return_value=NOW),
            ):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                    # 1. 未登录返回 401
                    unauth = await client.get("/api/v1/admin/subagents")
                    assert unauth.status_code == 401

                    # 2. 非管理员返回 403 admin_required
                    client.cookies.set("athena_session", member_token)
                    forbidden = await client.get("/api/v1/admin/subagents")
                    assert forbidden.status_code == 403
                    assert forbidden.json()["detail"] == "admin_required"

                    # 切换为管理员
                    client.cookies.set("athena_session", admin_token)

                    # 3. 获取可选工具目录（排除 dispatch_subagents）
                    tools_resp = await client.get("/api/v1/admin/subagents/available-tools")
                    assert tools_resp.status_code == 200
                    groups = tools_resp.json()["groups"]
                    tool_ids = {item["tool_id"] for g in groups for item in g["items"]}
                    assert DISPATCH_SUBAGENTS_TOOL_ID not in tool_ids
                    assert "builtin_knowledge_search" in tool_ids

                    # 4. 保留字 slug 返回 422 subagent_slug_reserved
                    reserved_resp = await client.post(
                        "/api/v1/admin/subagents",
                        headers=_admin_headers(),
                        json={
                            "slug": "main",
                            "name": "主助手",
                            "description": "保留字测试",
                            "system_prompt": "提示词",
                        },
                    )
                    assert reserved_resp.status_code == 422
                    assert reserved_resp.json()["detail"] == "subagent_slug_reserved"

                    # 5. 禁止工具 dispatch_subagents 返回 422 subagent_tool_forbidden
                    forbidden_tool_resp = await client.post(
                        "/api/v1/admin/subagents",
                        headers=_admin_headers(),
                        json={
                            "slug": "nested-agent",
                            "name": "嵌套助手",
                            "description": "禁止工具测试",
                            "system_prompt": "提示词",
                            "allowed_tool_ids": [
                                "builtin_knowledge_search",
                                DISPATCH_SUBAGENTS_TOOL_ID,
                            ],
                        },
                    )
                    assert forbidden_tool_resp.status_code == 422
                    assert forbidden_tool_resp.json()["detail"] == "subagent_tool_forbidden"

                    # 6. 不可用模型返回 422 subagent_model_unavailable
                    bad_model_resp = await client.post(
                        "/api/v1/admin/subagents",
                        headers=_admin_headers(),
                        json={
                            "slug": "bad-model-agent",
                            "name": "不可用模型",
                            "description": "模型已禁用",
                            "system_prompt": "提示词",
                            "model_profile_id": "mp_disabled",
                        },
                    )
                    assert bad_model_resp.status_code == 422
                    assert bad_model_resp.json()["detail"] == "subagent_model_unavailable"

                    # 7. 正常创建 Subagent
                    create_resp = await client.post(
                        "/api/v1/admin/subagents",
                        headers=_admin_headers(),
                        json={
                            "slug": "researcher",
                            "name": "研究员",
                            "description": "负责检索资料",
                            "system_prompt": "你是专业研究员",
                            "model_profile_id": "mp_active",
                            "allowed_tool_ids": [
                                "builtin_knowledge_search",
                                "builtin_knowledge_search",
                            ],
                            "max_llm_requests": 15,
                            "timeout_seconds": 300,
                        },
                    )
                    assert create_resp.status_code == 201
                    created = create_resp.json()
                    subagent_id = created["id"]
                    assert subagent_id.startswith("subagent_")
                    assert created["allowed_tool_ids"] == ["builtin_knowledge_search"]
                    assert created["version"] == 1
                    assert created["enabled"] is True

                    # 8. slug 冲突返回 409 subagent_slug_conflict
                    conflict_resp = await client.post(
                        "/api/v1/admin/subagents",
                        headers=_admin_headers(),
                        json={
                            "slug": "researcher",
                            "name": "重复研究员",
                            "description": "冲突测试",
                            "system_prompt": "提示词",
                        },
                    )
                    assert conflict_resp.status_code == 409
                    assert conflict_resp.json()["detail"] == "subagent_slug_conflict"

                    # 9. 详情与列表过滤
                    get_resp = await client.get(f"/api/v1/admin/subagents/{subagent_id}")
                    assert get_resp.status_code == 200
                    assert get_resp.json()["slug"] == "researcher"

                    list_enabled = await client.get("/api/v1/admin/subagents?enabled=true")
                    assert list_enabled.status_code == 200
                    assert list_enabled.json()["total"] == 1

                    # 10. 乐观锁更新成功 & 版本冲突 409
                    patch_resp = await client.patch(
                        f"/api/v1/admin/subagents/{subagent_id}",
                        headers=_admin_headers(),
                        json={
                            "version": 1,
                            "name": "高级研究员",
                            "model_profile_id": None,
                        },
                    )
                    assert patch_resp.status_code == 200
                    patched = patch_resp.json()
                    assert patched["version"] == 2
                    assert patched["name"] == "高级研究员"
                    assert patched["model_profile_id"] is None

                    stale_patch_resp = await client.patch(
                        f"/api/v1/admin/subagents/{subagent_id}",
                        headers=_admin_headers(),
                        json={"version": 1, "name": "过期更新"},
                    )
                    assert stale_patch_resp.status_code == 409
                    assert stale_patch_resp.json()["detail"] == "subagent_version_conflict"

                    # 11. 启停切换
                    disable_resp = await client.put(
                        f"/api/v1/admin/subagents/{subagent_id}/enabled",
                        headers=_admin_headers(),
                        json={"enabled": False},
                    )
                    assert disable_resp.status_code == 200
                    assert disable_resp.json()["enabled"] is False
                    assert disable_resp.json()["version"] == 3

                    list_disabled = await client.get("/api/v1/admin/subagents?enabled=false")
                    assert list_disabled.status_code == 200
                    assert list_disabled.json()["total"] == 1

                    # 12. 幂等软删除 & 删除后同 slug 可重建
                    del1 = await client.delete(
                        f"/api/v1/admin/subagents/{subagent_id}",
                        headers=_admin_headers(),
                    )
                    assert del1.status_code == 204

                    del2 = await client.delete(
                        f"/api/v1/admin/subagents/{subagent_id}",
                        headers=_admin_headers(),
                    )
                    assert del2.status_code == 204

                    get_deleted = await client.get(f"/api/v1/admin/subagents/{subagent_id}")
                    assert get_deleted.status_code == 404
                    assert get_deleted.json()["detail"] == "subagent_not_found"

                    recreate_resp = await client.post(
                        "/api/v1/admin/subagents",
                        headers=_admin_headers(),
                        json={
                            "slug": "researcher",
                            "name": "重建研究员",
                            "description": "软删除后同 slug 应可重建",
                            "system_prompt": "新提示词",
                        },
                    )
                    assert recreate_resp.status_code == 201
                    assert recreate_resp.json()["id"] != subagent_id

                    # 13. 事务内管理员权限被降级时，写操作返回 403
                    async with session_factory() as session:
                        async with session.begin():
                            user_row = await session.scalar(
                                select(UserModel).where(UserModel.id == "usr_admin")
                            )
                            assert user_row is not None
                            user_row.role = UserRole.MEMBER.value

                    demoted_resp = await client.post(
                        "/api/v1/admin/subagents",
                        headers=_admin_headers(),
                        json={
                            "slug": "after-demote",
                            "name": "降权测试",
                            "description": "应被拒绝",
                            "system_prompt": "提示词",
                        },
                    )
                    assert demoted_resp.status_code == 403
                    assert demoted_resp.json()["detail"] == "admin_required"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
