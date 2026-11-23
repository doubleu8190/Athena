"""create memory tables"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20261107_0008"
down_revision: str | None = "20261106_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS zhparser")
    if context.is_offline_mode():
        # 离线 SQL 生成无法查询实时 schema 与 zhparser token 列表，使用标准 DO 块生成等价静态 DDL。
        op.execute("CREATE TEXT SEARCH CONFIGURATION athena_memory_chinese (PARSER=zhparser)")
        op.execute(
            "DO $$ DECLARE aliases text; BEGIN "
            "SELECT string_agg(quote_ident(alias), ', ' ORDER BY tokid) INTO aliases "
            "FROM ts_token_type('zhparser'); "
            "IF aliases IS NULL THEN "
            "RAISE EXCEPTION 'zhparser returned no token types'; "
            "END IF; "
            "EXECUTE 'ALTER TEXT SEARCH CONFIGURATION athena_memory_chinese "
            "ADD MAPPING FOR ' || aliases || ' WITH simple'; "
            "END $$"
        )
    else:
        bind = op.get_bind()
        schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
        exists = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_ts_config c "
                "JOIN pg_namespace n ON n.oid=c.cfgnamespace "
                "WHERE n.nspname=:schema AND c.cfgname=:config)"
            ),
            {"schema": schema, "config": "athena_memory_chinese"},
        ).scalar_one()
        if exists:
            raise RuntimeError("athena_memory_chinese already exists in migration schema")
        quote = bind.dialect.identifier_preparer.quote
        config = f"{quote(schema)}.{quote('athena_memory_chinese')}"
        op.execute(f"CREATE TEXT SEARCH CONFIGURATION {config} (PARSER=zhparser)")
        aliases = (
            op.get_bind()
            .execute(sa.text("SELECT alias FROM ts_token_type('zhparser') ORDER BY tokid"))
            .scalars()
            .all()
        )
        if not aliases:
            raise RuntimeError("zhparser returned no token types")
        op.execute(
            f"ALTER TEXT SEARCH CONFIGURATION {config} ADD MAPPING FOR "
            + ", ".join(quote(alias) for alias in aliases)
            + " WITH simple"
        )
    op.create_table(
        "memories",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("namespace_id", sa.String(128), nullable=False),
        sa.Column("scope_key", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_document", postgresql.TSVECTOR(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("embedding_profile_id", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending','active')", name="ck_memories_status"),
        sa.CheckConstraint(
            "origin IN ('manual','explicit','extracted')", name="ck_memories_origin"
        ),
        sa.CheckConstraint("version >= 1", name="ck_memories_version"),
        sa.UniqueConstraint("namespace_id", "request_id", name="uq_memories_request"),
    )
    op.create_index(
        "ix_memories_scope_active", "memories", ["namespace_id", "scope_key", "updated_at"]
    )
    op.create_index(
        "ix_memories_content_fts", "memories", ["search_document"], postgresql_using="gin"
    )
    op.create_table(
        "memory_sources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "memory_id",
            sa.String(64),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(256), nullable=False),
        sa.UniqueConstraint("memory_id", "source_kind", "source_ref", name="uq_memory_source_ref"),
    )
    op.create_table(
        "memory_settings",
        sa.Column("namespace_id", sa.String(128), primary_key=True),
        sa.Column("read_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_extract_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_table("memory_settings")
    op.drop_table("memory_sources")
    op.drop_index("ix_memories_content_fts", table_name="memories")
    op.drop_index("ix_memories_scope_active", table_name="memories")
    op.drop_table("memories")
    if context.is_offline_mode():
        op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS athena_memory_chinese")
    else:
        bind = op.get_bind()
        schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
        quote = bind.dialect.identifier_preparer.quote
        config_name = f"{quote(schema)}.{quote('athena_memory_chinese')}"
        op.execute(f"DROP TEXT SEARCH CONFIGURATION IF EXISTS {config_name}")
