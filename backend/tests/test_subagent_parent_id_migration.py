"""Subagent 父 Run 关联列宽度迁移测试。"""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _migration_config() -> Config:
    """构造无需连接数据库的 PostgreSQL 离线迁移配置。"""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    return config


def test_subagent_parent_id_migration_emits_safe_widening_sql() -> None:
    """离线升级应生成三个目标宽度，且不生成任何向窄宽度回缩的 SQL。"""
    output = StringIO()
    with redirect_stdout(output):
        command.upgrade(
            _migration_config(),
            "20261128_0029:20261129_0030",
            sql=True,
        )

    sql = output.getvalue()
    assert "ALTER TABLE runs ALTER COLUMN parent_run_id TYPE VARCHAR(128)" in sql
    assert "ALTER TABLE runs ALTER COLUMN root_run_id TYPE VARCHAR(128)" in sql
    assert "ALTER TABLE runs ALTER COLUMN parent_tool_call_id TYPE VARCHAR(256)" in sql
    assert "VARCHAR(64)" not in sql


def test_new_database_migration_chain_creates_target_widths() -> None:
    """新库完整升级应直接创建目标宽度，再由 0030 幂等校正旧布局。"""
    output = StringIO()
    with redirect_stdout(output):
        command.upgrade(
            _migration_config(),
            "20261126_0027:head",
            sql=True,
        )

    sql = output.getvalue()
    assert "ADD COLUMN parent_run_id VARCHAR(128)" in sql
    assert "ADD COLUMN root_run_id VARCHAR(128)" in sql
    assert "ADD COLUMN parent_tool_call_id VARCHAR(256)" in sql
    assert "ALTER TABLE runs ALTER COLUMN parent_run_id TYPE VARCHAR(128)" in sql
    assert "ALTER TABLE runs ALTER COLUMN root_run_id TYPE VARCHAR(128)" in sql
    assert "ALTER TABLE runs ALTER COLUMN parent_tool_call_id TYPE VARCHAR(256)" in sql


def test_subagent_parent_id_migration_downgrade_keeps_widened_columns() -> None:
    """降级 0030 不应回缩列宽，以便长 ID 在后续版本回退过程中保持可读。"""
    output = StringIO()
    with redirect_stdout(output):
        command.downgrade(
            _migration_config(),
            "20261129_0030:20261128_0029",
            sql=True,
        )

    sql = output.getvalue()
    assert "ALTER TABLE runs ALTER COLUMN parent_run_id" not in sql
    assert "ALTER TABLE runs ALTER COLUMN root_run_id" not in sql
    assert "ALTER TABLE runs ALTER COLUMN parent_tool_call_id" not in sql
