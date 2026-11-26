"""PostgreSQL 单元测试底座（pg_unit_harness）与根级 conftest 环境隔离回归测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import conftest as root_conftest
import pg_unit_harness
import pytest
from pg_unit_harness import (
    create_pg_unit_db,
    ensure_pg_unit_schema,
    pg_unit_db,
    require_pg_test_database_url,
    temp_pg_schema,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel

_TESTS_ROOT = Path(__file__).resolve().parent


def _fake_request(test_path: Path, *, markers: set[str] | None = None) -> pytest.FixtureRequest:
    """构造最小 pytest.FixtureRequest 替身，供离线校验 conftest 环境隔离规则。"""
    active_markers = markers or set()
    node = SimpleNamespace(
        get_closest_marker=lambda name: object() if name in active_markers else None
    )
    return cast(pytest.FixtureRequest, SimpleNamespace(path=test_path, node=node))


def test_require_pg_test_database_url_gives_actionable_error_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置 ATHENA_TEST_DATABASE_URL 时应给出可直接复制执行的启动与加载命令提示。"""
    monkeypatch.delenv("ATHENA_TEST_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        require_pg_test_database_url()
    message = str(exc_info.value)
    assert "ATHENA_TEST_DATABASE_URL" in message
    assert "compose.test.yml" in message
    assert "scripts/load-test-env.sh" in message


def test_conftest_runtime_env_isolation_strips_unit_env_and_exempts_special_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通单测隔离开发服 ATHENA_* 且保留 ATHENA_TEST_*，特殊标记与集成目录全部豁免。"""
    # 1. 普通单测：清理 ATHENA_DATABASE_URL / ATHENA_MCP_SECRET_KEY，保留 ATHENA_TEST_* 等前缀。
    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1/athena")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "dummy-secret-key")
    monkeypatch.setenv("ATHENA_AUTH_MOCK_ENABLED", "true")
    monkeypatch.setenv("ATHENA_TEST_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1/athena_test")
    monkeypatch.setenv("ATHENA_LLM_LIVE_TESTS", "1")
    monkeypatch.setenv("ATHENA_HARNESS_SCHEMA", "s1")

    isolated = root_conftest._apply_unit_test_runtime_env_isolation(
        _fake_request(_TESTS_ROOT / "test_sample_unit.py"),
        monkeypatch,
    )
    assert isolated is True
    assert os.getenv("ATHENA_DATABASE_URL") is None
    assert os.getenv("ATHENA_MCP_SECRET_KEY") is None
    assert os.getenv("ATHENA_AUTH_MOCK_ENABLED") is None
    assert os.getenv("ATHENA_TEST_DATABASE_URL") == "postgresql+asyncpg://u:p@127.0.0.1/athena_test"
    assert os.getenv("ATHENA_LLM_LIVE_TESTS") == "1"
    assert os.getenv("ATHENA_HARNESS_SCHEMA") == "s1"

    # 2. 携带 llm_live / integration / real_ocr 标记的根目录单测必须豁免，不得清空密钥与连接串。
    for marker_name in ("llm_live", "integration", "real_ocr"):
        monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1/athena")
        monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "live-secret-key")
        applied = root_conftest._apply_unit_test_runtime_env_isolation(
            _fake_request(_TESTS_ROOT / "test_llm_live.py", markers={marker_name}),
            monkeypatch,
        )
        assert applied is False
        assert os.getenv("ATHENA_DATABASE_URL") == "postgresql+asyncpg://u:p@127.0.0.1/athena"
        assert os.getenv("ATHENA_MCP_SECRET_KEY") == "live-secret-key"

    # 3. integration/ 与 browser/ 目录下的用例按路径自动豁免。
    for subdir in ("integration", "browser"):
        monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1/athena")
        monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "dir-secret-key")
        applied = root_conftest._apply_unit_test_runtime_env_isolation(
            _fake_request(_TESTS_ROOT / subdir / "test_case.py"),
            monkeypatch,
        )
        assert applied is False
        assert os.getenv("ATHENA_DATABASE_URL") == "postgresql+asyncpg://u:p@127.0.0.1/athena"
        assert os.getenv("ATHENA_MCP_SECRET_KEY") == "dir-secret-key"


def test_ordinary_unit_test_live_autouse_clears_runtime_env() -> None:
    """在真实 pytest 会话中，普通单测启动前即已自动清空宿主机注入的运行时环境变量。"""
    assert os.getenv("ATHENA_DATABASE_URL") is None
    assert os.getenv("ATHENA_MCP_SECRET_KEY") is None


def test_pg_unit_db_reuses_session_schema_and_truncates_between_runs() -> None:
    """同进程内连续两次进入 pg_unit_db 应复用同一隔离 Schema 且自动 TRUNCATE 清空前序数据。"""
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)

    async def write_user() -> str:
        async with pg_unit_db() as (_engine, factory):
            async with factory() as session, session.begin():
                schema = str(await session.scalar(text("SELECT current_schema()")))
                session.add(
                    UserModel(
                        id="usr_harness_1",
                        email="harness@example.test",
                        password_hash="h",
                        display_name=None,
                        status="active",
                        role="member",
                        created_at=now,
                        updated_at=now,
                    )
                )
            return schema

    async def verify_clean() -> tuple[str, object]:
        engine, factory = await create_pg_unit_db()
        try:
            async with factory() as session:
                schema = str(await session.scalar(text("SELECT current_schema()")))
                existing = await session.get(UserModel, "usr_harness_1")
            return schema, existing
        finally:
            await engine.dispose()

    first_schema = asyncio.run(write_user())
    second_schema, existing = asyncio.run(verify_clean())

    assert first_schema.startswith("unit_")
    assert second_schema == first_schema
    assert existing is None


def test_temp_pg_schema_drops_schema_immediately_on_exit() -> None:
    """temp_pg_schema 退出后应立即级联删除一次性 Schema，不在测试库遗留。"""

    async def scenario() -> None:
        database_url, _ = await ensure_pg_unit_schema()
        created_schema = ""
        async with temp_pg_schema(prefix="unit_ddl_check") as (engine, schema):
            created_schema = schema
            async with engine.begin() as conn:
                current = await conn.scalar(text("SELECT current_schema()"))
                assert current == schema

        # 退出上下文后查询 information_schema.schemata 确认已彻底删除。
        admin = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                exists = await conn.scalar(
                    text(
                        "SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema_name"
                    ),
                    {"schema_name": created_schema},
                )
            assert exists is None
        finally:
            await admin.dispose()

    asyncio.run(scenario())
    # 验证模块清理函数已注册且可被安全引用（实际清理由 conftest.pytest_sessionfinish 执行）。
    assert callable(pg_unit_harness.cleanup_pg_unit_schema)
