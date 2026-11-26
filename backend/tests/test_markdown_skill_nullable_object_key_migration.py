"""迁移 20261209_0042（skill_versions.object_key 可空）回归测试。"""

from __future__ import annotations

import importlib.util
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20261209_0042_markdown_skill_nullable_object_key.py"
)


def _pg_config() -> Config:
    """构造无需真实连接的 PostgreSQL 离线迁移配置。"""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    return config


def _load_0042_module() -> Any:
    """动态加载 20261209_0042 迁移模块，便于在 SQLite 内存库上直接验证升降级。"""
    spec = importlib.util.spec_from_file_location("migration_20261209_0042", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0042_single_head_and_offline_postgresql_sql() -> None:
    """核验单一 head 线性链路，且 20261209_0042 的 PG 离线升降级 SQL 精准修改 object_key 可空性。"""
    pg_cfg = _pg_config()
    script = ScriptDirectory.from_config(pg_cfg)
    assert len(script.get_heads()) == 1
    rev = script.get_revision("20261209_0042")
    assert rev is not None
    assert rev.down_revision == "20261208_0041"

    # 验证从空库升级到 head 的完整离线 SQL 可无错生成。
    full_out = StringIO()
    with redirect_stdout(full_out):
        command.upgrade(_pg_config(), "head", sql=True)
    assert "ALTER TABLE skill_versions ALTER COLUMN object_key DROP NOT NULL" in full_out.getvalue()

    up_out = StringIO()
    with redirect_stdout(up_out):
        command.upgrade(_pg_config(), "20261208_0041:20261209_0042", sql=True)
    assert "ALTER TABLE skill_versions ALTER COLUMN object_key DROP NOT NULL" in up_out.getvalue()

    down_out = StringIO()
    with redirect_stdout(down_out):
        command.downgrade(_pg_config(), "20261209_0042:20261208_0041", sql=True)
    assert "ALTER TABLE skill_versions ALTER COLUMN object_key SET NOT NULL" in down_out.getvalue()


def test_0042_sqlite_upgrade_allows_multiple_nulls_and_downgrade_guards_null_data() -> None:
    """SQLite 升级后 UNIQUE(object_key) 允许多行 NULL 且仍拒绝重复非空键；降级前拦截 NULL。"""
    migration = _load_0042_module()
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    CREATE TABLE skill_versions (
                        id VARCHAR(128) PRIMARY KEY,
                        object_key VARCHAR(1024) NOT NULL,
                        CONSTRAINT uq_skill_versions_object_key UNIQUE (object_key)
                    )
                    """
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO skill_versions (id, object_key) "
                    "VALUES ('v_zip', 'skills/aa/pkg.zip')"
                )
            )
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

            # 升级后允许多条纯 Markdown 版本写入 object_key = NULL。
            conn.execute(
                sa.text("INSERT INTO skill_versions (id, object_key) VALUES ('v_md_1', NULL)")
            )
            conn.execute(
                sa.text("INSERT INTO skill_versions (id, object_key) VALUES ('v_md_2', NULL)")
            )

            # 已有非空 object_key 的唯一约束仍然有效。
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text(
                        "INSERT INTO skill_versions (id, object_key) "
                        "VALUES ('v_zip_dup', 'skills/aa/pkg.zip')"
                    )
                )

        # 存在 object_key IS NULL 时降级必须显式失败且不静默删数据。
        with pytest.raises(
            RuntimeError,
            match="cannot_downgrade_skill_versions_with_null_object_key",
        ):
            with engine.begin() as conn:
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    migration.downgrade()

        with engine.begin() as conn:
            null_count: int = int(
                conn.execute(
                    sa.text("SELECT COUNT(*) FROM skill_versions WHERE object_key IS NULL")
                ).scalar_one()
            )
            assert null_count == 2

            # 清理 NULL 记录后再降级，应成功恢复 NOT NULL 约束。
            conn.execute(sa.text("DELETE FROM skill_versions WHERE object_key IS NULL"))
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.downgrade()

            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text("INSERT INTO skill_versions (id, object_key) VALUES ('v_md_3', NULL)")
                )
    finally:
        engine.dispose()
