"""下线重型自动记忆表与 Run 记忆列，新建个人工作偏好与显式记忆表。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20261130_0033"
down_revision: str | None = "20261123_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[object]:
    """在 PostgreSQL 使用 JSONB，兼容跨方言元数据解析。"""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """移除重型自动记忆子系统数据结构，创建轻量个人工作偏好与显式记忆表。"""
    # 1. 按外键依赖顺序删除旧重型记忆五表（memory_sources 引用 memories.id）。
    op.drop_table("memory_sources")
    op.drop_table("memory_jobs")
    op.drop_table("memory_scope_state")
    op.drop_table("memory_settings")
    op.drop_table("memories")

    # 2. 移除 runs 表上的旧记忆索引、状态检查约束及全部记忆生命周期列。
    op.drop_index("ix_runs_memory_owner_pending", table_name="runs")
    op.drop_index("ix_runs_memory_pending", table_name="runs")
    op.drop_constraint("ck_runs_memory_extraction_status", "runs", type_="check")
    op.drop_column("runs", "memory_extraction_safe_error_code")
    op.drop_column("runs", "memory_extraction_batch_id")
    op.drop_column("runs", "memory_extracted_at")
    op.drop_column("runs", "memory_extraction_status")
    op.drop_column("runs", "memory_context_state")
    op.drop_column("runs", "memory_input_message_ids")
    op.drop_column("runs", "memory_project_id")

    # 3. 新建用户个人工作偏好表（user_work_profiles）。
    op.create_table(
        "user_work_profiles",
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "custom_instructions",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "memory_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 4. 新建用户显式备忘记忆表（user_memories）及按更新时间倒序查询索引。
    op.create_table(
        "user_memories",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.String(500), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('manual', 'chat_explicit')",
            name="ck_user_memories_source",
        ),
    )
    op.create_index(
        "ix_user_memories_user_updated",
        "user_memories",
        ["user_id", sa.text("updated_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    """回滚轻量偏好与显式记忆表，并恢复 20261123_0032 版本的旧记忆表结构。"""
    # 1. 删除新建的轻量偏好与显式记忆表结构。
    op.drop_index("ix_user_memories_user_updated", table_name="user_memories")
    op.drop_table("user_memories")
    op.drop_table("user_work_profiles")

    # 2. 恢复 runs 表上的旧记忆列、约束与索引。
    op.add_column("runs", sa.Column("memory_project_id", sa.String(64), nullable=True))
    op.add_column(
        "runs",
        sa.Column(
            "memory_input_message_ids",
            _json_type(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "memory_context_state",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column("runs", sa.Column("memory_extraction_status", sa.String(16), nullable=True))
    op.add_column("runs", sa.Column("memory_extracted_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("memory_extraction_batch_id", sa.String(128), nullable=True))
    op.add_column(
        "runs",
        sa.Column("memory_extraction_safe_error_code", sa.String(128), nullable=True),
    )
    op.create_check_constraint(
        "ck_runs_memory_extraction_status",
        "runs",
        "memory_extraction_status IS NULL OR "
        "memory_extraction_status IN ('pending','completed','skipped','failed')",
    )
    op.create_index(
        "ix_runs_memory_pending",
        "runs",
        ["memory_extraction_status", "finished_at", "id"],
    )
    op.create_index(
        "ix_runs_memory_owner_pending",
        "runs",
        ["owner_id", "memory_extraction_status", "finished_at", "id"],
    )

    # 3. 恢复 20261123_0032 版本的旧记忆五表空结构，保持 Alembic 降级链合法完整。
    op.create_table(
        "memories",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("namespace_id", sa.String(128), nullable=False),
        sa.Column("scope_key", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_document", postgresql.TSVECTOR(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("embedding_profile_id", sa.String(128), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=True),
        sa.Column("kind", sa.String(32), nullable=True),
        sa.Column("load_policy", sa.String(16), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("explicit_long_term", sa.Boolean(), nullable=True),
        sa.Column("last_evidenced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "superseded_by_id",
            sa.String(64),
            sa.ForeignKey("memories.id", name="fk_memories_superseded_by", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','active','archived','superseded')",
            name="ck_memories_status",
        ),
        sa.CheckConstraint(
            "origin IS NULL OR origin IN ('manual','explicit','extracted','')",
            name="ck_memories_origin",
        ),
        sa.CheckConstraint(
            "kind IS NULL OR kind IN ('constraint','decision','preference','fact')",
            name="ck_memories_kind",
        ),
        sa.CheckConstraint(
            "load_policy IS NULL OR load_policy IN ('always','on_demand')",
            name="ck_memories_load_policy",
        ),
        sa.CheckConstraint("version >= 1", name="ck_memories_version"),
        sa.UniqueConstraint("namespace_id", "request_id", name="uq_memories_request"),
    )
    op.create_index(
        "ix_memories_scope_active",
        "memories",
        ["namespace_id", "scope_key", "updated_at"],
    )
    op.create_index(
        "ix_memories_content_fts",
        "memories",
        ["search_document"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_memories_context",
        "memories",
        ["namespace_id", "scope_key", "status", "load_policy", "expires_at"],
    )
    op.create_index(
        "ix_memories_content_hash",
        "memories",
        ["namespace_id", "scope_key", "content_hash"],
    )
    op.create_index(
        "ix_memories_embedding_hnsw",
        "memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL AND deleted_time IS NULL"),
    )

    op.create_table(
        "memory_settings",
        sa.Column("namespace_id", sa.String(128), primary_key=True),
        sa.Column("read_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_extract_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("generation >= 1", name="ck_memory_settings_generation"),
    )
    op.create_table(
        "memory_scope_state",
        sa.Column("namespace_id", sa.String(128), primary_key=True),
        sa.Column("scope_key", sa.String(128), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_maintained_day", sa.String(10)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "memory_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dedupe_key", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", _json_type(), nullable=False),
        sa.Column("result", _json_type(), nullable=True),
        sa.Column("namespace_id", sa.String(128), nullable=False),
        sa.Column("scope_key", sa.String(128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("schema_retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('extract_pending','maintain_scope','cleanup_scope','control','scan_runs')",
            name="ck_memory_jobs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_memory_jobs_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_memory_jobs_attempt_count"),
        sa.CheckConstraint("schema_retry_count >= 0", name="ck_memory_jobs_schema_retry"),
        sa.UniqueConstraint("dedupe_key", name="uq_memory_jobs_dedupe"),
    )
    op.create_index(
        "ix_memory_jobs_claim",
        "memory_jobs",
        ["kind", "status", "available_at", "id"],
    )
    op.create_index(
        "ix_memory_jobs_scope_kind",
        "memory_jobs",
        ["namespace_id", "scope_key", "kind"],
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
        sa.Column("source_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("memory_id", "source_kind", "source_ref", name="uq_memory_source_ref"),
    )
    op.create_index(
        "ix_memory_sources_source_at",
        "memory_sources",
        ["memory_id", "source_at"],
    )
