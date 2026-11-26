"""迁移 20261210_0043（删除知识库上传预留与共享槽位协调表）回归测试。"""

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

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
MIGRATION_0010 = VERSIONS_DIR / "20261109_0010_knowledge_upload_safety.py"
MIGRATION_0043 = VERSIONS_DIR / "20261210_0043_drop_knowledge_upload_coordination_tables.py"


def _pg_config() -> Config:
    """构造无需真实连接的 PostgreSQL 离线迁移配置。"""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    return config


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0043_single_head_and_offline_postgresql_sql() -> None:
    """核验单一 head 为 20261210_0043，且 PG 离线升降级 SQL 完整删除并还原两表与索引。"""
    script = ScriptDirectory.from_config(_pg_config())
    assert script.get_heads() == ["20261210_0043"]
    rev = script.get_revision("20261210_0043")
    assert rev is not None
    assert rev.down_revision == "20261209_0042"

    # 从空库到 head 的全量离线 SQL 可正常生成。
    full_out = StringIO()
    with redirect_stdout(full_out):
        command.upgrade(_pg_config(), "head", sql=True)
    assert "DROP TABLE knowledge_upload_slots" in full_out.getvalue()
    assert "DROP TABLE knowledge_upload_reservations" in full_out.getvalue()

    up_out = StringIO()
    with redirect_stdout(up_out):
        command.upgrade(_pg_config(), "20261209_0042:20261210_0043", sql=True)
    up_sql = up_out.getvalue()
    assert "DROP TABLE knowledge_upload_slots" in up_sql
    assert "DROP INDEX ix_knowledge_upload_reservations_expiry" in up_sql
    assert "DROP TABLE knowledge_upload_reservations" in up_sql

    down_out = StringIO()
    with redirect_stdout(down_out):
        command.downgrade(_pg_config(), "20261210_0043:20261209_0042", sql=True)
    down_sql = down_out.getvalue()
    assert "CREATE TABLE knowledge_upload_reservations" in down_sql
    assert "CREATE INDEX ix_knowledge_upload_reservations_expiry" in down_sql
    assert "CREATE TABLE knowledge_upload_slots" in down_sql


def test_0043_sqlite_upgrade_downgrade_roundtrip() -> None:
    """在 SQLite 内存库上验证 0010 建表后经 0043 升级、降级还原与再次升级闭环。"""
    migration_0010 = _load_module("migration_20261109_0010", MIGRATION_0010)
    migration_0043 = _load_module("migration_20261210_0043", MIGRATION_0043)

    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE users (id VARCHAR(128) PRIMARY KEY)"))
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration_0010.upgrade()
                assert {"knowledge_upload_reservations", "knowledge_upload_slots"} <= set(
                    sa.inspect(conn).get_table_names()
                )

                migration_0043.upgrade()
                tables_after_up = set(sa.inspect(conn).get_table_names())
                assert "knowledge_upload_reservations" not in tables_after_up
                assert "knowledge_upload_slots" not in tables_after_up

                migration_0043.downgrade()
                inspector = sa.inspect(conn)
                assert {"knowledge_upload_reservations", "knowledge_upload_slots"} <= set(
                    inspector.get_table_names()
                )
                index_names = [
                    index["name"]
                    for index in inspector.get_indexes("knowledge_upload_reservations")
                ]
                assert "ix_knowledge_upload_reservations_expiry" in index_names
                slot_ids: list[int] = list(
                    conn.execute(
                        sa.text("SELECT slot_id FROM knowledge_upload_slots ORDER BY slot_id")
                    )
                    .scalars()
                    .all()
                )
                assert slot_ids == [1, 2, 3, 4]

                migration_0043.upgrade()
                tables_final = set(sa.inspect(conn).get_table_names())
                assert "knowledge_upload_reservations" not in tables_final
                assert "knowledge_upload_slots" not in tables_final
    finally:
        engine.dispose()
