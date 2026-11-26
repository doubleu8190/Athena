"""高频热路径索引迁移（20261207_0040）与 ORM 元数据一致性测试。"""

from __future__ import annotations

import asyncio
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
from sqlalchemy.ext.asyncio import create_async_engine

from adapter.models import Base

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20261207_0040_add_hotpath_indexes_and_drop_redundant.py"
)


def _pg_config() -> Config:
    """构造无需连接数据库的 PostgreSQL 离线迁移配置。"""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    return config


def _load_migration_module() -> Any:
    """动态加载 20261207_0040 迁移模块以支持 SQLite 在线升/降级验证。"""
    spec = importlib.util.spec_from_file_location("migration_20261207_0040", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_orm_metadata_matches_hotpath_indexes_and_drops_redundant_indexes() -> None:
    """ORM 元数据应包含全部 9 个高频索引、移除 4 个冗余索引，并兼容 SQLite 建表。"""
    expected_present = {
        "run_input_requests": {"ix_run_input_requests_run_created"},
        "runs": {"ix_runs_session_created", "ix_runs_session_status"},
        "conversation_messages": {"ix_conversation_messages_session_run_seq"},
        "knowledge_documents": {"ix_knowledge_documents_kb_active_updated"},
        "knowledge_document_versions": {
            "ix_knowledge_versions_doc_created",
            "ix_knowledge_versions_source_id",
        },
        "knowledge_sources": {"ix_knowledge_sources_owner_id"},
        "departments": {"ix_departments_path_pattern"},
    }
    expected_absent = {
        "resource_visibility_grants": "ix_resource_visibility_grants_resource",
        "knowledge_chunks": "ix_knowledge_chunks_version",
        "run_tool_calls": "ix_run_tool_calls_run_ordinal",
        "skill_versions": "ix_skill_versions_skill_ordinal",
    }

    for table_name, index_names in expected_present.items():
        declared = {idx.name for idx in Base.metadata.tables[table_name].indexes}
        assert index_names.issubset(declared), (table_name, index_names - declared)

    for table_name, redundant_name in expected_absent.items():
        declared = {idx.name for idx in Base.metadata.tables[table_name].indexes}
        assert redundant_name not in declared

    dept_indexes = {idx.name: idx for idx in Base.metadata.tables["departments"].indexes}
    pattern_idx = next(
        idx for name, idx in dept_indexes.items() if name == "ix_departments_path_pattern"
    )
    assert pattern_idx.dialect_options["postgresql"]["ops"] == {"path": "varchar_pattern_ops"}

    async def create_sqlite_schema() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(create_sqlite_schema())


def test_hotpath_index_migration_emits_expected_postgresql_upgrade_and_downgrade_sql() -> None:
    """PostgreSQL 离线升级与降级 SQL 须对称创建/删除全部 9 个新索引与 4 个冗余索引。"""
    pg_cfg = _pg_config()
    script = ScriptDirectory.from_config(pg_cfg)
    # 校验主干始终保持单一 head，且 20261207_0040 位于迁移链中（后续由 20261208_0041 承接）。
    assert len(script.get_heads()) == 1
    rev = script.get_revision("20261207_0040")
    assert rev is not None
    assert rev.down_revision == "20261206_0039"

    upgrade_out = StringIO()
    with redirect_stdout(upgrade_out):
        command.upgrade(pg_cfg, "20261206_0039:20261207_0040", sql=True)
    upgrade_sql = upgrade_out.getvalue()

    for snippet in (
        "CREATE INDEX ix_run_input_requests_run_created "
        "ON run_input_requests (run_id, created_at, id)",
        "CREATE INDEX ix_runs_session_created ON runs (session_id, created_at DESC, id DESC)",
        "CREATE INDEX ix_runs_session_status ON runs (session_id, status)",
        "CREATE INDEX ix_conversation_messages_session_run_seq "
        "ON conversation_messages (session_id, run_id, sequence)",
        "CREATE INDEX ix_knowledge_documents_kb_active_updated "
        "ON knowledge_documents (knowledge_base_id, updated_at DESC, id DESC) "
        "WHERE deleted_time IS NULL",
        "CREATE INDEX ix_knowledge_versions_doc_created "
        "ON knowledge_document_versions (document_id, created_at DESC, id DESC)",
        "CREATE INDEX ix_knowledge_versions_source_id "
        "ON knowledge_document_versions (source_id) WHERE source_id IS NOT NULL",
        "CREATE INDEX ix_knowledge_sources_owner_id ON knowledge_sources (owner_id)",
        "CREATE INDEX ix_departments_path_pattern ON departments (path varchar_pattern_ops)",
        "DROP INDEX ix_resource_visibility_grants_resource",
        "DROP INDEX ix_knowledge_chunks_version",
        "DROP INDEX ix_run_tool_calls_run_ordinal",
        "DROP INDEX ix_skill_versions_skill_ordinal",
    ):
        assert snippet in upgrade_sql, f"missing upgrade SQL: {snippet}\n{upgrade_sql}"

    downgrade_out = StringIO()
    with redirect_stdout(downgrade_out):
        command.downgrade(_pg_config(), "20261207_0040:20261206_0039", sql=True)
    downgrade_sql = downgrade_out.getvalue()

    for snippet in (
        "CREATE INDEX ix_skill_versions_skill_ordinal ON skill_versions (skill_id, ordinal)",
        "CREATE INDEX ix_run_tool_calls_run_ordinal ON run_tool_calls (run_id, ordinal)",
        "CREATE INDEX ix_knowledge_chunks_version ON knowledge_chunks (version_id)",
        "CREATE INDEX ix_resource_visibility_grants_resource "
        "ON resource_visibility_grants (resource_type, resource_id)",
        "DROP INDEX ix_departments_path_pattern",
        "DROP INDEX ix_knowledge_sources_owner_id",
        "DROP INDEX ix_knowledge_versions_source_id",
        "DROP INDEX ix_knowledge_versions_doc_created",
        "DROP INDEX ix_knowledge_documents_kb_active_updated",
        "DROP INDEX ix_conversation_messages_session_run_seq",
        "DROP INDEX ix_runs_session_status",
        "DROP INDEX ix_runs_session_created",
        "DROP INDEX ix_run_input_requests_run_created",
    ):
        assert snippet in downgrade_sql, f"missing downgrade SQL: {snippet}\n{downgrade_sql}"


def test_hotpath_index_migration_upgrades_and_downgrades_cleanly_on_sqlite() -> None:
    """SQLite 在线执行 upgrade/downgrade 应跳过 PostgreSQL 专用 pattern 索引且其余索引对称。"""
    migration = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as conn:
            # 构造升级前（0039）涉及的最小表结构及待清理的 4 个历史冗余索引。
            conn.exec_driver_sql(
                "CREATE TABLE run_input_requests (id TEXT, run_id TEXT, created_at TEXT)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE runs (id TEXT, session_id TEXT, status TEXT, created_at TEXT)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE conversation_messages "
                "(id TEXT, session_id TEXT, run_id TEXT, sequence INTEGER)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE knowledge_documents "
                "(id TEXT, knowledge_base_id TEXT, updated_at TEXT, deleted_time TEXT)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE knowledge_document_versions "
                "(id TEXT, document_id TEXT, source_id TEXT, created_at TEXT)"
            )
            conn.exec_driver_sql("CREATE TABLE knowledge_sources (id TEXT, owner_id TEXT)")
            conn.exec_driver_sql("CREATE TABLE departments (id TEXT, path TEXT)")
            conn.exec_driver_sql(
                "CREATE TABLE resource_visibility_grants (resource_type TEXT, resource_id TEXT)"
            )
            conn.exec_driver_sql("CREATE TABLE knowledge_chunks (version_id TEXT)")
            conn.exec_driver_sql("CREATE TABLE run_tool_calls (run_id TEXT, ordinal INTEGER)")
            conn.exec_driver_sql("CREATE TABLE skill_versions (skill_id TEXT, ordinal INTEGER)")
            conn.exec_driver_sql(
                "CREATE INDEX ix_resource_visibility_grants_resource "
                "ON resource_visibility_grants (resource_type, resource_id)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX ix_knowledge_chunks_version ON knowledge_chunks (version_id)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX ix_run_tool_calls_run_ordinal ON run_tool_calls (run_id, ordinal)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX ix_skill_versions_skill_ordinal ON skill_versions (skill_id, ordinal)"
            )

            def all_index_names() -> set[str]:
                rows = conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
                return {row[0] for row in rows}

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

            after_upgrade = all_index_names()
            assert {
                "ix_run_input_requests_run_created",
                "ix_runs_session_created",
                "ix_runs_session_status",
                "ix_conversation_messages_session_run_seq",
                "ix_knowledge_documents_kb_active_updated",
                "ix_knowledge_versions_doc_created",
                "ix_knowledge_versions_source_id",
                "ix_knowledge_sources_owner_id",
            }.issubset(after_upgrade)
            # SQLite 必须跳过 PostgreSQL 专用 varchar_pattern_ops 索引。
            assert "ix_departments_path_pattern" not in after_upgrade
            assert {
                "ix_resource_visibility_grants_resource",
                "ix_knowledge_chunks_version",
                "ix_run_tool_calls_run_ordinal",
                "ix_skill_versions_skill_ordinal",
            }.isdisjoint(after_upgrade)

            with Operations.context(ctx):
                migration.downgrade()

            after_downgrade = all_index_names()
            assert {
                "ix_resource_visibility_grants_resource",
                "ix_knowledge_chunks_version",
                "ix_run_tool_calls_run_ordinal",
                "ix_skill_versions_skill_ordinal",
            }.issubset(after_downgrade)
            assert {
                "ix_run_input_requests_run_created",
                "ix_runs_session_created",
                "ix_runs_session_status",
                "ix_conversation_messages_session_run_seq",
                "ix_knowledge_documents_kb_active_updated",
                "ix_knowledge_versions_doc_created",
                "ix_knowledge_versions_source_id",
                "ix_knowledge_sources_owner_id",
            }.isdisjoint(after_downgrade)
    finally:
        engine.dispose()
