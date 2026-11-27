"""基线迁移 0001_initial_schema 往返升级与 ORM 元数据一致性 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import io
import os
import re
from dataclasses import dataclass

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from integration.postgres_harness import (
    ALEMBIC_INI,
    BACKEND_DIR,
    isolated_postgres_schema,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.models import Base
from domain.system_prompt import (
    INITIAL_COMPRESSION_SYSTEM_PROMPT,
    INITIAL_MAIN_SYSTEM_PROMPT,
    INITIAL_SYSTEM_PROMPT_IDS,
)

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="ATHENA_TEST_DATABASE_URL is not configured"),
]

_BASELINE_REVISION = "0001_initial_schema"


@dataclass(frozen=True, slots=True)
class _SchemaSnapshot:
    """从 PostgreSQL 系统目录抽取的完整 Schema 结构快照。"""

    tables: frozenset[str]
    columns: dict[str, list[tuple[object, ...]]]
    constraints: dict[str, set[tuple[str, str, bool, bool, str]]]
    indexes: dict[str, set[tuple[str, bool, bool, str]]]


def _strip_schema_prefix(definition: str, schema: str) -> str:
    """移除 pg_get_*def 输出中的隔离 Schema 前缀，以便跨 Schema 逐项比对。"""
    normalized = definition.replace(f'"{schema}".', "").replace(f"{schema}.", "")
    # 兼容 PostgreSQL 可能省略或保留 public. 前缀的扩展类型引用（如 public.vector）。
    return re.sub(r"\bpublic\.", "", normalized)


async def _capture_schema_snapshot(engine: AsyncEngine, schema: str) -> _SchemaSnapshot:
    """通过 information_schema 与 pg_catalog 采集目标 Schema 的表、列、约束与索引定义。"""
    async with engine.connect() as conn:
        table_result = await conn.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = :schema
                  AND table_type = 'BASE TABLE'
                  AND table_name <> 'alembic_version'
                ORDER BY table_name
                """
            ),
            {"schema": schema},
        )
        tables = frozenset(str(row[0]) for row in table_result.all())

        column_rows = (
            await conn.execute(
                text(
                    """
                    SELECT
                        table_name,
                        column_name,
                        data_type,
                        udt_name,
                        character_maximum_length,
                        numeric_precision,
                        numeric_scale,
                        is_nullable,
                        column_default
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                      AND table_name <> 'alembic_version'
                    ORDER BY table_name, ordinal_position
                    """
                ),
                {"schema": schema},
            )
        ).all()
        columns: dict[str, list[tuple[object, ...]]] = {name: [] for name in tables}
        for row in column_rows:
            table_name = str(row[0])
            default_expr = _strip_schema_prefix(str(row[8]), schema) if row[8] is not None else None
            columns[table_name].append(
                (
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    row[4],
                    row[5],
                    row[6],
                    str(row[7]),
                    default_expr,
                )
            )

        constraint_rows = (
            await conn.execute(
                text(
                    """
                    SELECT
                        cls.relname AS table_name,
                        con.conname AS constraint_name,
                        con.contype::text AS constraint_type,
                        con.condeferrable,
                        con.condeferred,
                        pg_get_constraintdef(con.oid) AS definition
                    FROM pg_constraint con
                    JOIN pg_class cls ON cls.oid = con.conrelid
                    JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                    WHERE ns.nspname = :schema
                      AND cls.relname <> 'alembic_version'
                    ORDER BY cls.relname, con.conname
                    """
                ),
                {"schema": schema},
            )
        ).all()
        constraints: dict[str, set[tuple[str, str, bool, bool, str]]] = {
            name: set() for name in tables
        }
        for row in constraint_rows:
            constraints[str(row[0])].add(
                (
                    str(row[1]),
                    str(row[2]),
                    bool(row[3]),
                    bool(row[4]),
                    _strip_schema_prefix(str(row[5]), schema),
                )
            )

        index_rows = (
            await conn.execute(
                text(
                    """
                    SELECT
                        tbl.relname AS table_name,
                        idx_cls.relname AS index_name,
                        idx.indisunique,
                        idx.indisprimary,
                        pg_get_indexdef(idx.indexrelid) AS definition
                    FROM pg_index idx
                    JOIN pg_class tbl ON tbl.oid = idx.indrelid
                    JOIN pg_class idx_cls ON idx_cls.oid = idx.indexrelid
                    JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
                    WHERE ns.nspname = :schema
                      AND tbl.relname <> 'alembic_version'
                    ORDER BY tbl.relname, idx_cls.relname
                    """
                ),
                {"schema": schema},
            )
        ).all()
        indexes: dict[str, set[tuple[str, bool, bool, str]]] = {name: set() for name in tables}
        for row in index_rows:
            indexes[str(row[0])].add(
                (
                    str(row[1]),
                    bool(row[2]),
                    bool(row[3]),
                    _strip_schema_prefix(str(row[4]), schema),
                )
            )

    return _SchemaSnapshot(
        tables=tables,
        columns=columns,
        constraints=constraints,
        indexes=indexes,
    )


def test_alembic_single_baseline_head_and_offline_sql() -> None:
    """验证 Alembic 仅存线性迁移链与唯一 head，且离线 upgrade/downgrade SQL 均可无错渲染。"""
    versions_dir = BACKEND_DIR / "migrations" / "versions"
    migration_files = sorted(path.name for path in versions_dir.glob("*.py"))
    assert migration_files == [
        "0001_initial_schema.py",
        "0002_add_mcp_tool_result_labels.py",
    ]

    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    assert script.get_heads() == ["0002_add_mcp_tool_result_labels"]
    baseline_rev = script.get_revision(_BASELINE_REVISION)
    assert baseline_rev is not None
    assert baseline_rev.down_revision is None
    head_rev = script.get_revision("0002_add_mcp_tool_result_labels")
    assert head_rev is not None
    assert head_rev.down_revision == _BASELINE_REVISION

    # 离线模式验证 upgrade head --sql 与 downgrade base --sql，防止仅在线才能跑通的隐蔽依赖。
    upgrade_buffer = io.StringIO()
    upgrade_cfg = Config(str(ALEMBIC_INI), output_buffer=upgrade_buffer)
    upgrade_cfg.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://offline:offline@localhost/athena",
    )
    command.upgrade(upgrade_cfg, "head", sql=True)
    upgrade_sql = upgrade_buffer.getvalue()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in upgrade_sql
    assert "fk_conversation_messages_run_session" in upgrade_sql
    assert "fk_execution_messages_run_session" in upgrade_sql
    assert "DEFERRABLE INITIALLY DEFERRED" in upgrade_sql
    assert "INSERT INTO system_prompt_versions" in upgrade_sql
    assert "INSERT INTO system_prompt_settings" in upgrade_sql
    assert INITIAL_SYSTEM_PROMPT_IDS["main"] in upgrade_sql
    assert INITIAL_SYSTEM_PROMPT_IDS["compression"] in upgrade_sql

    downgrade_buffer = io.StringIO()
    downgrade_cfg = Config(str(ALEMBIC_INI), output_buffer=downgrade_buffer)
    downgrade_cfg.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://offline:offline@localhost/athena",
    )
    command.downgrade(downgrade_cfg, "0002_add_mcp_tool_result_labels:base", sql=True)
    downgrade_sql = downgrade_buffer.getvalue()
    assert "DROP TABLE runs" in downgrade_sql
    assert "DROP TABLE users" in downgrade_sql


def test_initial_schema_migration_roundtrip_and_seeds() -> None:
    """真实 PostgreSQL 验证 upgrade head → downgrade base → upgrade head 往返与初始种子。"""
    database_url = DATABASE_URL
    assert database_url is not None

    async def scenario() -> None:
        async with isolated_postgres_schema(
            database_url,
            prefix="athena_baseline_rt",
            revision="head",
        ) as db:
            expected_tables = frozenset(Base.metadata.tables.keys())

            async def assert_upgraded_state() -> None:
                async with db.engine.connect() as conn:
                    version_rows = (
                        await conn.execute(text("SELECT version_num FROM alembic_version"))
                    ).all()
                    assert [str(row[0]) for row in version_rows] == [
                        "0002_add_mcp_tool_result_labels"
                    ]

                    table_rows = (
                        await conn.execute(
                            text(
                                """
                                SELECT table_name
                                FROM information_schema.tables
                                WHERE table_schema = :schema
                                  AND table_type = 'BASE TABLE'
                                  AND table_name <> 'alembic_version'
                                """
                            ),
                            {"schema": db.schema},
                        )
                    ).all()
                    table_names = frozenset(str(row[0]) for row in table_rows)
                    assert table_names == expected_tables

                    settings: dict[str, str] = {
                        str(row[0]): str(row[1])
                        for row in (
                            await conn.execute(
                                text("SELECT kind, current_version_id FROM system_prompt_settings")
                            )
                        ).all()
                    }
                    assert settings == INITIAL_SYSTEM_PROMPT_IDS

                    prompt_contents: dict[str, str] = {
                        str(row[0]): str(row[1])
                        for row in (
                            await conn.execute(
                                text(
                                    "SELECT kind, content FROM system_prompt_versions "
                                    "WHERE id IN (:main_id, :compression_id)"
                                ),
                                {
                                    "main_id": INITIAL_SYSTEM_PROMPT_IDS["main"],
                                    "compression_id": INITIAL_SYSTEM_PROMPT_IDS["compression"],
                                },
                            )
                        ).all()
                    }
                    assert prompt_contents == {
                        "main": INITIAL_MAIN_SYSTEM_PROMPT,
                        "compression": INITIAL_COMPRESSION_SYSTEM_PROMPT,
                    }

            await assert_upgraded_state()

            # 完整降级回 base，所有业务表必须全部清理干净。
            await db.downgrade_to("base")
            async with db.engine.connect() as conn:
                remaining_tables = (
                    await conn.execute(
                        text(
                            """
                            SELECT table_name
                            FROM information_schema.tables
                            WHERE table_schema = :schema
                              AND table_type = 'BASE TABLE'
                              AND table_name <> 'alembic_version'
                            """
                        ),
                        {"schema": db.schema},
                    )
                ).all()
                assert remaining_tables == []
                remaining_versions = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).all()
                assert remaining_versions == []

            # 重新升级回 head，表结构与初始系统提示词种子必须完全恢复。
            await db.upgrade_to("head")
            await assert_upgraded_state()

    asyncio.run(scenario())


def test_initial_schema_matches_orm_metadata_exactly() -> None:
    """逐表比对 Alembic 0001_initial_schema 与 Base.metadata.create_all 的真实 PostgreSQL 结构。"""
    database_url = DATABASE_URL
    assert database_url is not None

    async def scenario() -> None:
        async with (
            isolated_postgres_schema(
                database_url,
                prefix="athena_parity_mig",
                revision="head",
            ) as migrated,
            isolated_postgres_schema(
                database_url,
                prefix="athena_parity_orm",
                revision=None,
            ) as orm_db,
        ):
            async with orm_db.engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )

            migrated_snap = await _capture_schema_snapshot(migrated.engine, migrated.schema)
            orm_snap = await _capture_schema_snapshot(orm_db.engine, orm_db.schema)

            assert migrated_snap.tables == orm_snap.tables
            assert migrated_snap.tables == frozenset(Base.metadata.tables.keys())

            for table_name in sorted(migrated_snap.tables):
                assert migrated_snap.columns[table_name] == orm_snap.columns[table_name], (
                    f"column mismatch on table {table_name}"
                )
                assert migrated_snap.constraints[table_name] == orm_snap.constraints[table_name], (
                    f"constraint mismatch on table {table_name}"
                )
                assert migrated_snap.indexes[table_name] == orm_snap.indexes[table_name], (
                    f"index mismatch on table {table_name}"
                )

            # 显式核实三组延迟校验复合外键均以 DEFERRABLE INITIALLY DEFERRED 落库。
            deferred_checks = (
                ("conversation_messages", "fk_conversation_messages_run_session"),
                ("execution_messages", "fk_execution_messages_run_session"),
                ("runs", "fk_runs_trigger_message_session"),
            )
            for table_name, fk_name in deferred_checks:
                matching = [
                    item
                    for item in migrated_snap.constraints[table_name]
                    if item[0] == fk_name and item[1] == "f"
                ]
                assert len(matching) == 1, f"missing deferred fk {fk_name} on {table_name}"
                _, _, is_deferrable, is_deferred, _ = matching[0]
                assert is_deferrable is True
                assert is_deferred is True

            # 核实 run_requests.ck_run_requests_op 存在、knowledge_requests 无重复唯一约束，
            # 且 mcp_tools/runs/run_tool_calls 的 5 个非空列均带有数据库层 server_default。
            run_request_checks = {
                item[0] for item in migrated_snap.constraints["run_requests"] if item[1] == "c"
            }
            assert "ck_run_requests_op" in run_request_checks
            knowledge_request_constraints = {
                item[0] for item in migrated_snap.constraints["knowledge_requests"]
            }
            assert "uq_knowledge_request_key" not in knowledge_request_constraints

            expected_defaults: tuple[tuple[str, str], ...] = (
                ("mcp_tools", "requires_approval"),
                ("runs", "event_sequence"),
                ("runs", "execution_epoch"),
                ("runs", "usage"),
                ("run_tool_calls", "round"),
            )
            for table_name, column_name in expected_defaults:
                col_entry = next(
                    col for col in migrated_snap.columns[table_name] if col[0] == column_name
                )
                assert col_entry[7] is not None, (
                    f"missing server_default on {table_name}.{column_name}"
                )

    asyncio.run(scenario())
