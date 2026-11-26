"""摘除 zhparser 扩展依赖（0008/0027 补丁与 20261208_0041 清理迁移）回归测试。"""

from __future__ import annotations

import importlib.util
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.dialects.postgresql.base import PGDialect

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20261208_0041_drop_zhparser_artifacts.py"
)


def _pg_config() -> Config:
    """构造无需真实连接的 PostgreSQL 离线迁移配置。"""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    return config


def _load_0041_module() -> Any:
    """动态加载 20261208_0041 迁移模块，便于覆盖 PostgreSQL/SQLite 在线执行分支。"""
    spec = importlib.util.spec_from_file_location("migration_20261208_0041", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_full_offline_upgrade_sql_has_single_head_0041_and_no_zhparser_creation() -> None:
    """保持单一 head（含 20261208_0041），从空库升至 head 的离线 SQL 不含 zhparser 创建。"""
    pg_cfg = _pg_config()
    script = ScriptDirectory.from_config(pg_cfg)
    assert len(script.get_heads()) == 1
    rev = script.get_revision("20261208_0041")
    assert rev is not None
    assert rev.down_revision == "20261207_0040"

    full_out = StringIO()
    with redirect_stdout(full_out):
        command.upgrade(pg_cfg, "head", sql=True)
    full_sql = full_out.getvalue()

    # 确认全量建库路径保留 pgvector，但彻底不包含 zhparser 创建或历史分词配置定义。
    assert "CREATE EXTENSION IF NOT EXISTS vector" in full_sql
    assert "CREATE EXTENSION IF NOT EXISTS zhparser" not in full_sql
    assert "CREATE TEXT SEARCH CONFIGURATION" not in full_sql
    assert "ts_token_type('zhparser')" not in full_sql
    assert "to_tsvector('simple', preserved.preserved_content)" in full_sql
    assert "to_tsvector('athena_memory_chinese'" not in full_sql

    # 确认 0041 离线升级段输出受权限与依赖异常保护的幂等清理 DDL，且严禁使用 CASCADE。
    step_out = StringIO()
    with redirect_stdout(step_out):
        command.upgrade(_pg_config(), "20261207_0040:20261208_0041", sql=True)
    step_sql = step_out.getvalue()
    assert "DROP TEXT SEARCH CONFIGURATION IF EXISTS athena_memory_chinese" in step_sql
    assert "DROP EXTENSION IF EXISTS zhparser" in step_sql
    assert "insufficient_privilege" in step_sql
    assert "dependent_objects_still_exist" in step_sql
    assert "CASCADE" not in step_sql.upper()

    # 确认 0041 离线降级为 no-op，不重新创建已废弃的 zhparser 对象。
    down_out = StringIO()
    with redirect_stdout(down_out):
        command.downgrade(_pg_config(), "20261208_0041:20261207_0040", sql=True)
    down_sql = down_out.getvalue()
    assert "zhparser" not in down_sql
    assert "athena_memory_chinese" not in down_sql


class _ScalarResult:
    """模拟 SQLAlchemy scalar_one 返回值。"""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _FakePgBind:
    """模拟 PostgreSQL 在线连接，按场景返回 schema 与 pg_extension 探测结果并记录执行的 DDL。"""

    def __init__(self, *, schema: str | None, has_zhparser: bool) -> None:
        self.dialect = PGDialect()  # type: ignore[no-untyped-call]
        self._schema = schema
        self._has_zhparser = has_zhparser
        self.executed: list[str] = []

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _ScalarResult:
        sql = str(statement)
        self.executed.append(sql)
        if "SELECT current_schema()" in sql:
            return _ScalarResult(self._schema)
        if "FROM pg_extension WHERE extname = 'zhparser'" in sql:
            return _ScalarResult(self._has_zhparser)
        return _ScalarResult(None)


def test_0041_online_postgresql_drops_config_and_guards_extension_drop() -> None:
    """存量库存在 zhparser 时先删当前 schema 配置再用双异常保护 DO 块卸载；全新库跳过扩展卸载。"""
    migration = _load_0041_module()

    # 场景 1：存量库安装了 zhparser（非 public schema 亦需正确引用标识符）。
    legacy_bind = _FakePgBind(schema="tenant_schema", has_zhparser=True)
    legacy_ctx = MigrationContext.configure(
        connection=legacy_bind,  # type: ignore[arg-type]
        opts={"dialect_name": "postgresql"},
    )
    with Operations.context(legacy_ctx):
        migration.upgrade()
        migration.downgrade()

    assert any(
        "DROP TEXT SEARCH CONFIGURATION IF EXISTS tenant_schema.athena_memory_chinese" in sql
        for sql in legacy_bind.executed
    )
    assert any(
        "DROP EXTENSION IF EXISTS zhparser" in sql
        and "insufficient_privilege" in sql
        and "dependent_objects_still_exist" in sql
        and "CASCADE" not in sql.upper()
        for sql in legacy_bind.executed
    )

    # 场景 2：全新无 zhparser 数据库，不尝试执行 DROP EXTENSION，避免非超管或环境差异告警。
    clean_bind = _FakePgBind(schema="public", has_zhparser=False)
    clean_ctx = MigrationContext.configure(
        connection=clean_bind,  # type: ignore[arg-type]
        opts={"dialect_name": "postgresql"},
    )
    with Operations.context(clean_ctx):
        migration.upgrade()

    assert any(
        "DROP TEXT SEARCH CONFIGURATION IF EXISTS public.athena_memory_chinese" in sql
        for sql in clean_bind.executed
    )
    assert not any("DROP EXTENSION IF EXISTS zhparser" in sql for sql in clean_bind.executed)


def test_0041_online_postgresql_quotes_special_schema_and_handles_missing_current_schema() -> None:
    """含特殊字符 schema 必须正确引号转义；current_schema() 为 None 时跳过限定删除避免拼出 None。"""
    migration = _load_0041_module()

    # 场景 1：含大写字母、连字符与双引号的 schema 须经 PostgreSQL 标识符转义器安全引用。
    special_bind = _FakePgBind(schema='My-"Custom"-Schema', has_zhparser=True)
    special_ctx = MigrationContext.configure(
        connection=special_bind,  # type: ignore[arg-type]
        opts={"dialect_name": "postgresql"},
    )
    with Operations.context(special_ctx):
        migration.upgrade()

    assert any(
        'DROP TEXT SEARCH CONFIGURATION IF EXISTS "My-""Custom""-Schema".athena_memory_chinese'
        in sql
        for sql in special_bind.executed
    )
    assert any(
        "DROP EXTENSION IF EXISTS zhparser" in sql
        and "insufficient_privilege" in sql
        and "dependent_objects_still_exist" in sql
        for sql in special_bind.executed
    )

    # 场景 2：异常 search_path 下 current_schema() 返回 None 时，不得拼出 "None" 分词配置名。
    null_schema_bind = _FakePgBind(schema=None, has_zhparser=True)
    null_schema_ctx = MigrationContext.configure(
        connection=null_schema_bind,  # type: ignore[arg-type]
        opts={"dialect_name": "postgresql"},
    )
    with Operations.context(null_schema_ctx):
        migration.upgrade()

    assert not any("DROP TEXT SEARCH CONFIGURATION" in sql for sql in null_schema_bind.executed)
    assert not any("None" in sql for sql in null_schema_bind.executed)
    assert any(
        "DROP EXTENSION IF EXISTS zhparser" in sql
        and "insufficient_privilege" in sql
        and "dependent_objects_still_exist" in sql
        for sql in null_schema_bind.executed
    )


def test_0041_online_sqlite_is_safe_noop() -> None:
    """SQLite 内存测试环境下 0041 升级与降级均为安全空操作。"""
    migration = _load_0041_module()
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()
                migration.downgrade()
    finally:
        engine.dispose()
