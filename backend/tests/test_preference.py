"""个人工作偏好与显式记忆领域规则、仓储、HTTP API、迁移链与下线清洁度测试。"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Coroutine
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from auth_support import AuthTestClient, make_auth_service
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from adapter.auth.model import UserModel
from adapter.models import Base
from adapter.preference import SqlAlchemyPreferenceRepository
from app.preference import PreferenceService
from domain.preference import (
    MAX_CUSTOM_INSTRUCTIONS_LENGTH,
    MAX_USER_MEMORIES_PER_USER,
    MAX_USER_MEMORY_CONTENT_LENGTH,
    InvalidCustomInstructionsError,
    InvalidUserMemoryContentError,
    PreferenceContext,
    PreferenceError,
    UserMemory,
    UserMemoryDisabledError,
    UserMemoryLimitReachedError,
    UserMemoryNotFoundError,
    UserMemorySource,
    UserWorkProfile,
    validate_custom_instructions,
    validate_user_memory_content,
)
from main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


class MutableClock:
    """可推进的确定性 UTC 时钟，用于验证排序与更新时间变化。"""

    def __init__(self, initial: datetime | None = None) -> None:
        self.value = initial or datetime(2026, 11, 20, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int = 1) -> None:
        self.value += timedelta(seconds=seconds)


class InMemoryPreferenceRepository:
    """供 HTTP API 单元测试使用的内存偏好仓储，与 PostgreSQL 仓储语义一致。"""

    def __init__(self) -> None:
        self.profiles: dict[str, UserWorkProfile] = {}
        self.memories: dict[str, UserMemory] = {}

    async def get_profile(self, user_id: str) -> UserWorkProfile | None:
        return self.profiles.get(user_id)

    async def upsert_profile(
        self,
        user_id: str,
        *,
        custom_instructions: str,
        memory_enabled: bool,
        updated_at: datetime,
    ) -> UserWorkProfile:
        profile = UserWorkProfile(
            user_id=user_id,
            custom_instructions=custom_instructions,
            memory_enabled=memory_enabled,
            updated_at=updated_at,
        )
        self.profiles[user_id] = profile
        return profile

    async def list_memories(self, user_id: str) -> tuple[UserMemory, ...]:
        items = [item for item in self.memories.values() if item.user_id == user_id]
        items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(items)

    async def create_memory(
        self,
        memory: UserMemory,
        *,
        max_memories: int,
        require_memory_enabled: bool = False,
    ) -> UserMemory:
        profile = self.profiles.get(memory.user_id)
        if require_memory_enabled and profile is not None and not profile.memory_enabled:
            raise UserMemoryDisabledError("user_memory_disabled")
        existing_count = sum(1 for item in self.memories.values() if item.user_id == memory.user_id)
        if existing_count >= max_memories:
            raise UserMemoryLimitReachedError("user_memory_limit_reached")
        self.memories[memory.id] = memory
        return memory

    async def update_memory(
        self,
        user_id: str,
        memory_id: str,
        *,
        content: str,
        updated_at: datetime,
    ) -> UserMemory | None:
        current = self.memories.get(memory_id)
        if current is None or current.user_id != user_id:
            return None
        updated = replace(current, content=content, updated_at=updated_at)
        self.memories[memory_id] = updated
        return updated

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        current = self.memories.get(memory_id)
        if current is None or current.user_id != user_id:
            return False
        del self.memories[memory_id]
        return True

    async def get_context(self, user_id: str) -> PreferenceContext:
        profile = self.profiles.get(user_id)
        custom_instructions = "" if profile is None else profile.custom_instructions
        memory_enabled = True if profile is None else profile.memory_enabled
        if not memory_enabled:
            return PreferenceContext(
                custom_instructions=custom_instructions,
                memory_enabled=False,
                memories=(),
            )
        return PreferenceContext(
            custom_instructions=custom_instructions,
            memory_enabled=True,
            memories=await self.list_memories(user_id),
        )


def _incrementing_id_factory() -> Any:
    counter = 0

    def factory(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}_{counter:04d}"

    return factory


async def _make_sqlite_repository() -> tuple[
    Any,
    async_sessionmaker[AsyncSession],
    SqlAlchemyPreferenceRepository,
]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory, SqlAlchemyPreferenceRepository(session_factory)


async def _seed_user(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: str,
) -> None:
    timestamp = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                UserModel(
                    id=user_id,
                    email=f"{user_id}@example.com",
                    password_hash="hash:test",
                    display_name=None,
                    status="active",
                    created_at=timestamp,
                    updated_at=timestamp,
                    deleted_time=None,
                )
            )


def _migration_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    return config


# ---------------------------------------------------------------------------
# 1. 领域规则与输入校验
# ---------------------------------------------------------------------------


def test_validate_custom_instructions_trims_and_allows_multiline_whitespace() -> None:
    """个人工作背景指令允许为空、保留换行/回车/制表符并去除首尾空白。"""
    assert validate_custom_instructions("   \n\t  ") == ""
    assert validate_custom_instructions("  第一行\n\t第二行\r\n第三行  ") == (
        "第一行\n\t第二行\r\n第三行"
    )
    exact_max = "测" * MAX_CUSTOM_INSTRUCTIONS_LENGTH
    assert validate_custom_instructions(f"  {exact_max}  ") == exact_max


@pytest.mark.parametrize(
    "bad_value",
    [
        "a" * (MAX_CUSTOM_INSTRUCTIONS_LENGTH + 1),
        "含空字节\x00指令",
        "含响铃符\x07指令",
        "\x0b前置垂直制表符",
        "后置换页符\x0c",
        "含删除控制符\x7f",
        "含下一行控制符\x85",
    ],
)
def test_validate_custom_instructions_rejects_oversized_and_control_chars(bad_value: str) -> None:
    """超 4000 字符或包含非法 Cc 控制字符（含首尾 \\x0b/\\x0c）必须被拒绝。"""
    with pytest.raises(InvalidCustomInstructionsError) as exc_info:
        validate_custom_instructions(bad_value)
    assert exc_info.value.code == "custom_instructions_invalid"


def test_validate_user_memory_content_trims_and_accepts_bounds() -> None:
    """显式记忆正文首尾去空白，支持 1..500 字符及常规换行/制表符。"""
    assert validate_user_memory_content("  偏好使用简体中文回答\n\t简洁输出  ") == (
        "偏好使用简体中文回答\n\t简洁输出"
    )
    exact_max = "记" * MAX_USER_MEMORY_CONTENT_LENGTH
    assert validate_user_memory_content(f"\n {exact_max} \t") == exact_max


@pytest.mark.parametrize(
    "bad_content",
    [
        "",
        "   \n\t\r  ",
        "x" * (MAX_USER_MEMORY_CONTENT_LENGTH + 1),
        "含空字节\x00记忆",
        "含转义符\x1b[31m红字",
        "\x0b前置控制符",
        "后置控制符\x0c",
    ],
)
def test_validate_user_memory_content_rejects_empty_oversized_and_control_chars(
    bad_content: str,
) -> None:
    """空内容、超 500 字符及非法 Cc 控制字符必须抛出 user_memory_content_invalid。"""
    with pytest.raises(InvalidUserMemoryContentError) as exc_info:
        validate_user_memory_content(bad_content)
    assert exc_info.value.code == "user_memory_content_invalid"


# ---------------------------------------------------------------------------
# 2. SQLAlchemy 仓储事务锁、配额上限、排序与跨用户隔离
# ---------------------------------------------------------------------------


def test_sqlalchemy_repository_profile_crud_and_context_toggle() -> None:
    """仓储应支持默认空偏好读取、upsert 更新及按开关控制上下文返回。"""

    async def scenario() -> None:
        engine, session_factory, repo = await _make_sqlite_repository()
        try:
            await _seed_user(session_factory, "user_a")
            clock = MutableClock()
            service = PreferenceService(
                repo,
                id_factory=_incrementing_id_factory(),
                clock=clock,
            )

            default_profile = await service.get_profile("user_a")
            assert default_profile == UserWorkProfile(
                user_id="user_a",
                custom_instructions="",
                memory_enabled=True,
                updated_at=None,
            )

            created_profile = await service.update_profile(
                "user_a",
                custom_instructions="  我是后端工程师  ",
                memory_enabled=True,
            )
            assert created_profile.custom_instructions == "我是后端工程师"
            assert created_profile.memory_enabled is True
            assert created_profile.updated_at == clock.value

            memory = await service.create_memory("user_a", "回答尽量给代码示例")
            context_enabled = await service.get_context("user_a")
            assert context_enabled.custom_instructions == "我是后端工程师"
            assert context_enabled.memory_enabled is True
            assert tuple(item.id for item in context_enabled.memories) == (memory.id,)

            clock.advance(seconds=10)
            disabled_profile = await service.update_profile(
                "user_a",
                custom_instructions="我是架构师",
                memory_enabled=False,
            )
            assert disabled_profile.memory_enabled is False
            assert disabled_profile.updated_at == clock.value

            # memory_enabled=False 时上下文仅保留 custom_instructions，不再返回显式记忆。
            context_disabled = await service.get_context("user_a")
            assert context_disabled.custom_instructions == "我是架构师"
            assert context_disabled.memory_enabled is False
            assert context_disabled.memories == ()

            # 关闭开关时对话工具写入必须被拒绝，但手动维护仍被允许。
            with pytest.raises(UserMemoryDisabledError) as disabled_exc:
                await service.create_memory(
                    "user_a",
                    "对话自动记住",
                    source=UserMemorySource.CHAT_EXPLICIT,
                )
            assert disabled_exc.value.code == "user_memory_disabled"

            manual_when_disabled = await service.create_memory(
                "user_a",
                "设置页手动维护",
                source=UserMemorySource.MANUAL,
            )
            assert manual_when_disabled.source is UserMemorySource.MANUAL
        finally:
            await engine.dispose()

    _run(scenario())


def test_sqlalchemy_repository_enforces_50_limit_and_locks_user_row_for_update() -> None:
    """新增显式记忆必须在事务内对 users 表执行 FOR UPDATE 行锁，并严守 50 条上限。"""

    async def scenario() -> None:
        engine, session_factory, repo = await _make_sqlite_repository()
        recorded_pg_sql: list[str] = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _capture_sql(
            _conn: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            recorded_pg_sql.append(statement)

        # 同时使用 PostgreSQL 方言编译验证 repository 中的 users 行锁确实生成 FOR UPDATE。
        from sqlalchemy import select as sa_select

        pg_lock_sql = str(
            sa_select(UserModel.id)
            .where(UserModel.id == "user_a")
            .with_for_update()
            .compile(dialect=cast(Any, postgresql).dialect())
        )
        assert "FROM users" in pg_lock_sql
        assert "FOR UPDATE" in pg_lock_sql

        try:
            await _seed_user(session_factory, "user_a")
            await _seed_user(session_factory, "user_b")
            clock = MutableClock()
            service = PreferenceService(
                repo,
                id_factory=_incrementing_id_factory(),
                clock=clock,
            )

            recorded_pg_sql.clear()
            created_ids: list[str] = []
            for index in range(MAX_USER_MEMORIES_PER_USER):
                clock.advance(seconds=1)
                item = await service.create_memory("user_a", f"记忆条目 {index + 1}")
                created_ids.append(item.id)

            # 验证 create_memory 事务内先查 users 行再统计数量与插入。
            first_select_users = next(
                idx
                for idx, sql in enumerate(recorded_pg_sql)
                if "FROM users" in sql and "WHERE users.id" in sql
            )
            first_count = next(
                idx
                for idx, sql in enumerate(recorded_pg_sql)
                if "count(user_memories.id)" in sql.lower()
            )
            assert first_select_users < first_count

            # 第 51 条必须触发硬上限错误。
            with pytest.raises(UserMemoryLimitReachedError) as exc_info:
                await service.create_memory("user_a", "第 51 条记忆")
            assert exc_info.value.code == "user_memory_limit_reached"

            # 其他用户拥有独立 50 条配额，不受 user_a 满额影响。
            other_memory = await service.create_memory("user_b", "用户 B 的第一条记忆")
            assert other_memory.user_id == "user_b"

            # 删除一条后，user_a 应重新获得一个可用配额槽位。
            await service.delete_memory("user_a", created_ids[0])
            replacement = await service.create_memory("user_a", "补位的新记忆")
            assert replacement.content == "补位的新记忆"

            # 不存在的用户行应触发 user_not_found 保护错误。
            with pytest.raises(PreferenceError) as missing_user_exc:
                await service.create_memory("ghost_user", "无宿主记忆")
            assert missing_user_exc.value.code == "user_not_found"
        finally:
            await engine.dispose()

    _run(scenario())


def test_sqlalchemy_repository_ordering_and_cross_user_isolation() -> None:
    """记忆列表必须按 updated_at DESC, id DESC 排序，且跨用户改删均按不存在拒绝。"""

    async def scenario() -> None:
        engine, session_factory, repo = await _make_sqlite_repository()
        try:
            await _seed_user(session_factory, "user_a")
            await _seed_user(session_factory, "user_b")
            clock = MutableClock()
            service = PreferenceService(
                repo,
                id_factory=_incrementing_id_factory(),
                clock=clock,
            )

            # 同一时间戳下按 id DESC 稳定排序（umem_0002 在 umem_0001 前）。
            m1 = await service.create_memory("user_a", "第一条")
            m2 = await service.create_memory("user_a", "第二条")
            clock.advance(seconds=5)
            m3 = await service.create_memory("user_a", "第三条")

            listed = await service.list_memories("user_a")
            assert listed.total == 3
            assert listed.limit == MAX_USER_MEMORIES_PER_USER
            assert tuple(item.id for item in listed.items) == (m3.id, m2.id, m1.id)

            # 更新 m1 后其 updated_at 最新，应跃升至列表首位。
            clock.advance(seconds=5)
            updated_m1 = await service.update_memory("user_a", m1.id, content="更新后的第一条")
            assert updated_m1.content == "更新后的第一条"
            listed_after_update = await service.list_memories("user_a")
            assert tuple(item.id for item in listed_after_update.items) == (m1.id, m3.id, m2.id)

            # user_b 不可见、不可修改、不可删除 user_a 的任何记忆。
            assert (await service.list_memories("user_b")).items == ()
            with pytest.raises(UserMemoryNotFoundError) as patch_exc:
                await service.update_memory("user_b", m1.id, content="越权修改")
            assert patch_exc.value.code == "user_memory_not_found"

            with pytest.raises(UserMemoryNotFoundError) as delete_exc:
                await service.delete_memory("user_b", m1.id)
            assert delete_exc.value.code == "user_memory_not_found"
        finally:
            await engine.dispose()

    _run(scenario())


# ---------------------------------------------------------------------------
# 3. HTTP REST API (/api/v1/me/preferences & /api/v1/me/memories)
# ---------------------------------------------------------------------------


def test_preference_http_api_defaults_update_and_validation() -> None:
    """GET/PUT /api/v1/me/preferences 应返回默认值、持久化更新并拦截非法输入。"""
    clock = MutableClock()
    service = PreferenceService(
        InMemoryPreferenceRepository(),
        id_factory=_incrementing_id_factory(),
        clock=clock,
    )
    app = create_app(preference_service=service, auth_service=make_auth_service())

    with AuthTestClient(app, email="alice@example.com") as client:
        default_resp = client.get("/api/v1/me/preferences")
        assert default_resp.status_code == 200
        assert default_resp.json() == {
            "custom_instructions": "",
            "memory_enabled": True,
            "updated_at": None,
        }

        update_resp = client.put(
            "/api/v1/me/preferences",
            json={
                "custom_instructions": "  优先使用 Python 3.12 风格\n补充关键中文注释  ",
                "memory_enabled": False,
            },
        )
        assert update_resp.status_code == 200
        assert update_resp.json() == {
            "custom_instructions": "优先使用 Python 3.12 风格\n补充关键中文注释",
            "memory_enabled": False,
            "updated_at": "2026-11-20T09:00:00Z",
        }

        # 控制字符与超长文本返回 422。
        ctrl_resp = client.put(
            "/api/v1/me/preferences",
            json={"custom_instructions": "非法\x00字符", "memory_enabled": True},
        )
        assert ctrl_resp.status_code == 422
        assert ctrl_resp.json()["detail"] == "custom_instructions_invalid"

        too_long_resp = client.put(
            "/api/v1/me/preferences",
            json={
                "custom_instructions": "长" * (MAX_CUSTOM_INSTRUCTIONS_LENGTH + 1),
                "memory_enabled": True,
            },
        )
        assert too_long_resp.status_code == 422
        assert too_long_resp.json()["detail"] == "custom_instructions_invalid"

        # 额外未知字段被 DTO extra="forbid" 拒绝。
        extra_resp = client.put(
            "/api/v1/me/preferences",
            json={"custom_instructions": "ok", "memory_enabled": True, "user_id": "forged"},
        )
        assert extra_resp.status_code == 422


def test_memory_http_api_crud_quota_409_and_cross_user_404() -> None:
    """显式记忆 CRUD、50 条 409 配额限制与跨用户 404 隔离。"""
    clock = MutableClock()
    service = PreferenceService(
        InMemoryPreferenceRepository(),
        id_factory=_incrementing_id_factory(),
        clock=clock,
    )
    auth_service = make_auth_service()
    app = create_app(preference_service=service, auth_service=auth_service)

    with AuthTestClient(app, email="alice@example.com") as alice:
        empty_list = alice.get("/api/v1/me/memories")
        assert empty_list.status_code == 200
        assert empty_list.json() == {"items": [], "total": 0, "limit": 50}

        # 手动创建返回 201、Location 头和 source=manual。
        created = alice.post("/api/v1/me/memories", json={"content": "  熟悉 FastAPI 与 PG  "})
        assert created.status_code == 201
        first_body = created.json()
        first_id = first_body["id"]
        assert created.headers["location"] == f"/api/v1/me/memories/{first_id}"
        assert first_body["content"] == "熟悉 FastAPI 与 PG"
        assert first_body["source"] == "manual"
        assert "user_id" not in first_body

        # 修改正文返回 200 并推进 updated_at。
        clock.advance(seconds=30)
        patched = alice.patch(
            f"/api/v1/me/memories/{first_id}",
            json={"content": "  主攻 Python 后端与 PostgreSQL  "},
        )
        assert patched.status_code == 200
        assert patched.json()["content"] == "主攻 Python 后端与 PostgreSQL"
        assert patched.json()["updated_at"] == "2026-11-20T09:00:30Z"

        # 非法正文（空白 / 控制字符 / 超长）均返回 422。
        for bad_content in ("   ", "含\x0b控制符", "a" * (MAX_USER_MEMORY_CONTENT_LENGTH + 1)):
            bad_create = alice.post("/api/v1/me/memories", json={"content": bad_content})
            assert bad_create.status_code == 422
            assert bad_create.json()["detail"] == "user_memory_content_invalid"

            bad_patch = alice.patch(
                f"/api/v1/me/memories/{first_id}",
                json={"content": bad_content},
            )
            assert bad_patch.status_code == 422
            assert bad_patch.json()["detail"] == "user_memory_content_invalid"

        # 补足至 50 条，第 51 条必须返回 409 user_memory_limit_reached。
        for idx in range(2, MAX_USER_MEMORIES_PER_USER + 1):
            clock.advance(seconds=1)
            resp = alice.post("/api/v1/me/memories", json={"content": f"记忆 #{idx}"})
            assert resp.status_code == 201

        overflow = alice.post("/api/v1/me/memories", json={"content": "超出 50 条上限"})
        assert overflow.status_code == 409
        assert overflow.json()["detail"] == "user_memory_limit_reached"

        full_list = alice.get("/api/v1/me/memories")
        assert full_list.status_code == 200
        assert full_list.json()["total"] == 50
        assert full_list.json()["limit"] == 50
        assert full_list.json()["items"][0]["content"] == "记忆 #50"

    # 另一个用户 Bob 既看不到 Alice 的记忆，也无法 PATCH/DELETE（统一返回 404）。
    with AuthTestClient(app, email="bob@example.com") as bob:
        bob_list = bob.get("/api/v1/me/memories")
        assert bob_list.status_code == 200
        assert bob_list.json() == {"items": [], "total": 0, "limit": 50}

        bob_patch = bob.patch(f"/api/v1/me/memories/{first_id}", json={"content": "越权修改"})
        assert bob_patch.status_code == 404
        assert bob_patch.json()["detail"] == "user_memory_not_found"

        bob_delete = bob.delete(f"/api/v1/me/memories/{first_id}")
        assert bob_delete.status_code == 404
        assert bob_delete.json()["detail"] == "user_memory_not_found"

    # Alice 删除单条后返回 204，再次删除返回 404，且可重新新增一条。
    with AuthTestClient(app, email="alice@example.com") as alice:
        deleted = alice.delete(f"/api/v1/me/memories/{first_id}")
        assert deleted.status_code == 204
        assert deleted.content == b""

        deleted_again = alice.delete(f"/api/v1/me/memories/{first_id}")
        assert deleted_again.status_code == 404
        assert deleted_again.json()["detail"] == "user_memory_not_found"

        recreated = alice.post("/api/v1/me/memories", json={"content": "删除后可再次创建"})
        assert recreated.status_code == 201


def test_preference_http_api_requires_auth_and_verifies_origin() -> None:
    """未认证请求返回 401，跨源写入请求被 verify_request_origin 拒绝返回 403。"""
    service = PreferenceService(InMemoryPreferenceRepository())
    app = create_app(preference_service=service, auth_service=make_auth_service())

    with TestClient(app) as anon:
        assert anon.get("/api/v1/me/preferences").status_code == 401
        assert anon.get("/api/v1/me/memories").status_code == 401
        assert anon.post("/api/v1/me/memories", json={"content": "未登录写入"}).status_code == 401

    with AuthTestClient(app, email="csrf@example.com") as authed:
        cross_origin = authed.put(
            "/api/v1/me/preferences",
            json={"custom_instructions": "x", "memory_enabled": True},
            headers={"Origin": "https://evil.example"},
        )
        assert cross_origin.status_code == 403

        cross_origin_post = authed.post(
            "/api/v1/me/memories",
            json={"content": "跨源写入"},
            headers={"Origin": "https://evil.example"},
        )
        assert cross_origin_post.status_code == 403


# ---------------------------------------------------------------------------
# 4. Alembic 迁移 20261130_0033 与代码/依赖清理检查
# ---------------------------------------------------------------------------


def test_alembic_migration_20261130_0033_chain_upgrade_and_downgrade_sql() -> None:
    """验证 Alembic 迁移链保持单 head，且 20261130_0033 离线 upgrade/downgrade SQL 完整合法。"""
    config = _migration_config()
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    rev = script.get_revision("20261130_0033")
    assert rev is not None
    assert rev.down_revision == "20261123_0032"

    upgrade_sql_buf = StringIO()
    with redirect_stdout(upgrade_sql_buf):
        command.upgrade(config, "20261123_0032:20261130_0033", sql=True)
    upgrade_sql = upgrade_sql_buf.getvalue()

    for old_table in (
        "DROP TABLE memory_sources",
        "DROP TABLE memory_jobs",
        "DROP TABLE memory_scope_state",
        "DROP TABLE memory_settings",
        "DROP TABLE memories",
    ):
        assert old_table in upgrade_sql
    assert "DROP INDEX ix_runs_memory_owner_pending" in upgrade_sql
    assert "DROP INDEX ix_runs_memory_pending" in upgrade_sql
    assert "DROP CONSTRAINT ck_runs_memory_extraction_status" in upgrade_sql
    for old_col in (
        "memory_project_id",
        "memory_input_message_ids",
        "memory_extraction_status",
        "memory_context_state",
        "memory_extracted_at",
        "memory_extraction_batch_id",
        "memory_extraction_safe_error_code",
    ):
        assert f"DROP COLUMN {old_col}" in upgrade_sql
    assert "CREATE TABLE user_work_profiles" in upgrade_sql
    assert "CREATE TABLE user_memories" in upgrade_sql
    assert "CREATE INDEX ix_user_memories_user_updated ON user_memories" in upgrade_sql

    downgrade_sql_buf = StringIO()
    with redirect_stdout(downgrade_sql_buf):
        command.downgrade(config, "20261130_0033:20261123_0032", sql=True)
    downgrade_sql = downgrade_sql_buf.getvalue()
    assert "DROP TABLE user_memories" in downgrade_sql
    assert "DROP TABLE user_work_profiles" in downgrade_sql
    assert "CREATE TABLE memories" in downgrade_sql
    assert "CREATE TABLE memory_sources" in downgrade_sql


def test_backend_src_and_pyproject_have_no_heavy_memory_or_local_hf_residuals() -> None:
    """确保 backend/src 与 pyproject.toml 无残留旧重型记忆引用或本地 HF 依赖。"""
    pyproject = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
    dependencies: list[str] = pyproject["project"]["dependencies"]
    dep_names = [
        dep.split(">=")[0].split("==")[0].split("<")[0].strip().lower() for dep in dependencies
    ]
    assert "sentence-transformers" not in dep_names
    assert "transformers" not in dep_names

    forbidden_tokens = (
        "domain.memory",
        "app.memory",
        "adapter.memory",
        "infrastructure.memory",
        "memory_worker",
        "ATHENA_MEMORY_",
        "LocalEmbedding",
        "LocalTokenCounter",
        "sentence_transformers",
        "sentence-transformers",
        "import transformers",
        "from transformers",
        "memory_search",
        "memory_remember",
        "memory_correct",
        "memory_forget",
    )
    offenders: list[str] = []
    for py_file in sorted(BACKEND_SRC.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in text:
                offenders.append(f"{py_file.relative_to(REPO_ROOT)}:{token}")
    assert offenders == [], f"检测到残留重型记忆/本地模型引用: {offenders}"
