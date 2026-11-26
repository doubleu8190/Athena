"""PostgreSQL 集成测试隔离 Schema 夹具的单元测试（不依赖外部数据库）。"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from integration import postgres_harness
from integration.postgres_harness import (
    ALEMBIC_INI,
    build_alembic_config,
    isolated_postgres_schema,
    sync_isolated_postgres_schema,
)


class _FakeConnection:
    """记录执行过的 DDL 语句，可按需在 CREATE SCHEMA 时注入失败。"""

    def __init__(self, log: list[str], *, fail_on_create: bool = False) -> None:
        self._log = log
        self._fail_on_create = fail_on_create

    async def execute(self, statement: object) -> None:
        sql = str(statement)
        self._log.append(sql)
        if self._fail_on_create and "CREATE SCHEMA" in sql:
            raise ConnectionRefusedError("postgres_unavailable")


class _FakeEngine:
    """模拟 SQLAlchemy AsyncEngine，记录 begin/execute 与 dispose 顺序。"""

    def __init__(
        self,
        label: str,
        log: list[str],
        *,
        fail_on_create: bool = False,
        fail_on_dispose: bool = False,
    ) -> None:
        self.label = label
        self._log = log
        self._fail_on_create = fail_on_create
        self._fail_on_dispose = fail_on_dispose

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_FakeConnection]:
        yield _FakeConnection(self._log, fail_on_create=self._fail_on_create)

    async def dispose(self) -> None:
        self._log.append(f"dispose:{self.label}")
        if self._fail_on_dispose:
            raise RuntimeError(f"dispose_failed:{self.label}")


def test_build_alembic_config_escapes_percent_and_isolates_search_path() -> None:
    """URL 中的 % 必须被转义为 %%，且 search_path 与 version_table_schema 指向目标 schema。"""
    url = "postgresql+asyncpg://user:p%40ss%25word@127.0.0.1:5432/athena"
    config = build_alembic_config(
        url,
        "athena_test_schema_01",
        connect_args={"timeout": 8, "command_timeout": 12},
        server_settings={"statement_timeout": "15000"},
    )
    assert config.config_file_name == str(ALEMBIC_INI)
    assert config.get_main_option("sqlalchemy.url") == url
    assert config.attributes["version_table_schema"] == "athena_test_schema_01"
    assert config.attributes["connect_args"] == {
        "timeout": 8,
        "command_timeout": 12,
        "server_settings": {
            "search_path": "athena_test_schema_01,public",
            "statement_timeout": "15000",
        },
    }


@pytest.mark.parametrize("bad_schema", ["", "123_starts_digit", 'bad"quote', "a" * 64])
def test_build_alembic_config_rejects_invalid_schema_names(bad_schema: str) -> None:
    """非法字符或超出 PostgreSQL 63 字节限制的 schema 名称必须被拒绝。"""
    with pytest.raises(ValueError, match="invalid_postgres_schema_name"):
        build_alembic_config("postgresql+asyncpg://u:p@localhost/db", bad_schema)


def test_isolated_postgres_schema_lifecycle_and_exception_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常与异常退出均按 dispose(engine) -> DROP SCHEMA -> dispose(admin) 顺序彻底清理。"""
    events: list[str] = []
    engine_kwargs_seen: list[dict[str, object]] = []

    def _fake_create_engine(url: str, **kwargs: object) -> _FakeEngine:
        del url
        engine_kwargs_seen.append(dict(kwargs))
        label = "admin" if len(engine_kwargs_seen) % 2 == 1 else "test"
        return _FakeEngine(label, events)

    def _fake_alembic_sync(
        action: str,
        config: object,
        target_revision: str,
    ) -> None:
        del config
        events.append(f"alembic:{action}:{target_revision}")

    monkeypatch.setattr(postgres_harness, "create_async_engine", _fake_create_engine)
    monkeypatch.setattr(postgres_harness, "_execute_alembic_sync", _fake_alembic_sync)
    monkeypatch.setattr(postgres_harness, "async_sessionmaker", lambda engine, **kw: (engine, kw))

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            async with isolated_postgres_schema(
                "postgresql+asyncpg://u:p@localhost/db",
                prefix="athena_unit",
                revision="0001_initial_schema",
                connect_args={"timeout": 8},
                server_settings={"lock_timeout": "5000"},
                engine_server_settings={"application_name": "race-app"},
                pool_size=1,
            ) as db:
                assert db.schema.startswith("athena_unit_")
                assert db.head_revision() == "0001_initial_schema"
                await db.upgrade_to("head")
                await db.downgrade_to("base")
                raise RuntimeError("boom")

    asyncio.run(scenario())

    # 校验连接池与 server_settings 精确注入，且测试引擎隔离了 application_name。
    assert engine_kwargs_seen[0]["connect_args"] == {"timeout": 8}
    assert engine_kwargs_seen[1]["pool_size"] == 1
    assert engine_kwargs_seen[1]["max_overflow"] == 0
    assert engine_kwargs_seen[1]["pool_timeout"] == 0.5
    test_connect_args = cast(dict[str, Any], engine_kwargs_seen[1]["connect_args"])
    assert test_connect_args["timeout"] == 8
    assert test_connect_args["server_settings"]["lock_timeout"] == "5000"
    assert test_connect_args["server_settings"]["application_name"] == "race-app"
    assert test_connect_args["server_settings"]["search_path"].startswith("athena_unit_")

    assert events[0].startswith('CREATE SCHEMA "athena_unit_')
    assert events[1:4] == [
        "alembic:upgrade:0001_initial_schema",
        "alembic:upgrade:head",
        "alembic:downgrade:base",
    ]
    assert events[4] == "dispose:test"
    assert events[5].startswith('DROP SCHEMA IF EXISTS "athena_unit_')
    assert events[6] == "dispose:admin"


def test_isolated_postgres_schema_skips_drop_when_create_schema_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CREATE SCHEMA 阶段即连接失败时不应尝试 DROP SCHEMA，且两个引擎仍须释放。"""
    events: list[str] = []
    call_count = 0

    def _fake_create_engine(url: str, **kwargs: object) -> _FakeEngine:
        del url, kwargs
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _FakeEngine("admin", events, fail_on_create=True)
        return _FakeEngine("test", events)

    monkeypatch.setattr(postgres_harness, "create_async_engine", _fake_create_engine)
    monkeypatch.setattr(postgres_harness, "async_sessionmaker", lambda engine, **kw: (engine, kw))

    async def scenario() -> None:
        with pytest.raises(ConnectionRefusedError, match="postgres_unavailable"):
            async with isolated_postgres_schema(
                "postgresql+asyncpg://u:p@localhost/db",
                prefix="athena_fail",
                revision=None,
            ):
                raise AssertionError("unreachable")

    asyncio.run(scenario())

    assert len(events) == 3
    assert events[0].startswith('CREATE SCHEMA "athena_fail_')
    assert events[1:] == ["dispose:test", "dispose:admin"]


def test_sync_isolated_postgres_schema_and_subprocess_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步上下文与子进程迁移模式均应正确传递参数并在退出时回收 schema。"""
    events: list[str] = []
    captured_env: dict[str, str] = {}

    def _fake_create_engine(url: str, **kwargs: object) -> _FakeEngine:
        del url, kwargs
        return _FakeEngine("admin", events)

    def _fake_subprocess_run(
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, capture_output, text, timeout, check
        captured_env.update(env)
        events.append(f"subprocess:{env['ATHENA_HARNESS_ALEMBIC_ACTION']}")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(postgres_harness, "create_async_engine", _fake_create_engine)
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)

    with sync_isolated_postgres_schema(
        "postgresql+asyncpg://u:p@localhost/db",
        prefix="athena_sync",
        connect_args={"timeout": 8},
        server_settings={"statement_timeout": "15000"},
        use_subprocess=True,
    ) as schema:
        assert schema.startswith("athena_sync_")
        events.append("body")

    assert captured_env["ATHENA_HARNESS_SCHEMA"] == schema
    assert captured_env["ATHENA_HARNESS_REVISION"] == "head"
    assert json.loads(captured_env["ATHENA_HARNESS_CONNECT_ARGS"]) == {"timeout": 8}
    assert json.loads(captured_env["ATHENA_HARNESS_SERVER_SETTINGS"]) == {
        "statement_timeout": "15000"
    }
    assert events == [
        f'CREATE SCHEMA "{schema}"',
        "subprocess:upgrade",
        "dispose:admin",
        "body",
        f'DROP SCHEMA IF EXISTS "{schema}" CASCADE',
        "dispose:admin",
    ]


def test_isolated_postgres_schema_drops_schema_and_disposes_admin_when_engine_dispose_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使 engine.dispose() 抛异常，仍必须级联删除隔离 schema 并关闭 admin_engine。"""
    events: list[str] = []
    call_count = 0

    def _fake_create_engine(url: str, **kwargs: object) -> _FakeEngine:
        del url, kwargs
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _FakeEngine("admin", events)
        return _FakeEngine("test", events, fail_on_dispose=True)

    monkeypatch.setattr(postgres_harness, "create_async_engine", _fake_create_engine)
    monkeypatch.setattr(postgres_harness, "async_sessionmaker", lambda engine, **kw: (engine, kw))

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="dispose_failed:test"):
            async with isolated_postgres_schema(
                "postgresql+asyncpg://u:p@localhost/db",
                prefix="athena_dispose_err",
                revision=None,
            ) as db:
                events.append(f"body:{db.schema}")

    asyncio.run(scenario())

    assert len(events) == 5
    assert events[0].startswith('CREATE SCHEMA "athena_dispose_err_')
    assert events[1].startswith("body:athena_dispose_err_")
    assert events[2] == "dispose:test"
    assert events[3].startswith('DROP SCHEMA IF EXISTS "athena_dispose_err_')
    assert events[4] == "dispose:admin"


def test_search_path_override_is_rejected_for_alembic_and_engine() -> None:
    """server_settings 与 engine_server_settings 不得覆盖隔离 schema 的 search_path。"""
    with pytest.raises(ValueError, match="cannot_override_isolated_search_path"):
        build_alembic_config(
            "postgresql+asyncpg://u:p@localhost/db",
            "athena_isolated_01",
            server_settings={"search_path": "public"},
        )

    async def _try_override_engine() -> None:
        async with isolated_postgres_schema(
            "postgresql+asyncpg://u:p@localhost/db",
            prefix="athena_isolated",
            revision=None,
            engine_server_settings={"search_path": "public"},
        ):
            raise AssertionError("unreachable")

    with pytest.raises(ValueError, match="cannot_override_isolated_search_path"):
        asyncio.run(_try_override_engine())
