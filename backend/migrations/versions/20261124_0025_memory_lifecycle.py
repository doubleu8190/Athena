"""增加记忆生命周期状态、后台任务和 Run 提取进度。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261124_0025"
down_revision: str | None = "20261123_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[object]:
    """让迁移在 PostgreSQL 使用 JSONB。"""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """在保留旧正文和来源的前提下增加可回滚的五表记忆生命周期结构。"""
    op.drop_constraint("ck_memories_status", "memories", type_="check")
    op.drop_constraint("ck_memories_origin", "memories", type_="check")
    op.alter_column("memories", "embedding_profile_id", existing_type=sa.String(128), nullable=True)
    # 墓碑清洗会清空来源分类，因此必须先放宽旧非空约束再更新旧墓碑；
    # 否则升级包含历史删除记录时会因 origin NOT NULL 整笔失败。
    op.alter_column("memories", "origin", existing_type=sa.String(16), nullable=True)
    op.add_column("memories", sa.Column("kind", sa.String(32), nullable=True))
    op.add_column("memories", sa.Column("load_policy", sa.String(16), nullable=True))
    op.add_column("memories", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column(
        "memories",
        sa.Column("explicit_long_term", sa.Boolean(), nullable=True),
    )
    op.add_column("memories", sa.Column("last_evidenced_at", sa.DateTime(timezone=True)))
    op.add_column("memories", sa.Column("superseded_by_id", sa.String(64), nullable=True))
    op.add_column("memories", sa.Column("subject_key", sa.String(200), nullable=True))
    op.add_column("memories", sa.Column("fact_key", sa.String(128), nullable=True))
    op.add_column("memories", sa.Column("fact_value", sa.String(500), nullable=True))
    op.add_column("memories", sa.Column("cardinality", sa.String(16), nullable=True))
    op.add_column("memories", sa.Column("polarity", sa.String(16), nullable=True))
    op.add_column("memories", sa.Column("applicability", _json_type(), nullable=True))
    op.add_column("memories", sa.Column("applicability_key", sa.String(64), nullable=True))
    op.add_column("memories", sa.Column("normalization_version", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_memories_superseded_by",
        "memories",
        "memories",
        ["superseded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        "UPDATE memories SET kind = 'fact' "
        "WHERE kind IS NULL AND deleted_time IS NULL"
    )
    op.execute(
        "UPDATE memories SET load_policy = 'on_demand' "
        "WHERE load_policy IS NULL AND deleted_time IS NULL"
    )
    op.execute(
        "UPDATE memories SET last_evidenced_at = created_at "
        "WHERE last_evidenced_at IS NULL AND deleted_time IS NULL"
    )
    op.execute(
        "UPDATE memories SET explicit_long_term = true "
        "WHERE origin = 'explicit' AND deleted_time IS NULL"
    )
    # 旧版本墓碑仍可能残留正文、检索索引、向量、结构化事实和来源。
    # 升级时按墓碑白名单一次性清除全部内容载体，只保留幂等键、定位和审计字段。
    op.execute(
        "UPDATE memories SET "
        "content = '', "
        "search_document = ''::tsvector, "
        "embedding = NULL, "
        "embedding_profile_id = NULL, "
        "origin = NULL, "
        "kind = NULL, "
        "load_policy = NULL, "
        "content_hash = NULL, "
        "explicit_long_term = NULL, "
        "last_evidenced_at = NULL, "
        "expires_at = NULL, "
        "subject_key = NULL, "
        "fact_key = NULL, "
        "fact_value = NULL, "
        "cardinality = NULL, "
        "polarity = NULL, "
        "applicability = NULL, "
        "applicability_key = NULL, "
        "normalization_version = NULL, "
        "superseded_by_id = NULL "
        "WHERE deleted_time IS NOT NULL"
    )
    op.execute(
        "DELETE FROM memory_sources "
        "WHERE memory_id IN ("
        "SELECT id FROM memories WHERE deleted_time IS NOT NULL"
        ")"
    )
    op.create_check_constraint(
        "ck_memories_status",
        "memories",
        "status IN ('pending','active','archived','superseded')",
    )
    op.create_check_constraint(
        "ck_memories_origin",
        "memories",
        "origin IS NULL OR origin IN ('manual','explicit','extracted','')",
    )
    op.create_check_constraint(
        "ck_memories_kind",
        "memories",
        "kind IS NULL OR kind IN ('constraint','decision','preference','fact')",
    )
    op.create_check_constraint(
        "ck_memories_load_policy",
        "memories",
        "load_policy IS NULL OR load_policy IN ('always','on_demand')",
    )
    op.create_check_constraint(
        "ck_memories_cardinality",
        "memories",
        "cardinality IS NULL OR cardinality IN ('single','set','unknown')",
    )
    op.create_check_constraint(
        "ck_memories_polarity",
        "memories",
        "polarity IS NULL OR polarity IN ('affirmed','negated')",
    )
    op.create_index(
        "ix_memories_fact_group",
        "memories",
        ["namespace_id", "scope_key", "subject_key", "fact_key", "applicability_key"],
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

    op.add_column("memory_sources", sa.Column("source_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_memory_sources_source_at",
        "memory_sources",
        ["memory_id", "source_at"],
    )
    op.add_column(
        "memory_settings",
        sa.Column("auto_extract_boundary_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "memory_scope_state",
        sa.Column("namespace_id", sa.String(128), primary_key=True),
        sa.Column("scope_key", sa.String(128), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("mutation_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("extraction_not_before", sa.DateTime(timezone=True)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_maintained_day", sa.String(10)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_memory_scope_revision"),
        sa.CheckConstraint("mutation_epoch >= 1", name="ck_memory_scope_epoch"),
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
        sa.Column("worker_id", sa.String(128)),
        sa.Column("lease_token", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('extract_pending','maintain_scope','cleanup_scope','control','scan_runs')",
            name="ck_memory_jobs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued','leased','done','dead')",
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
        "ix_memory_jobs_lease",
        "memory_jobs",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_memory_jobs_scope_kind",
        "memory_jobs",
        ["namespace_id", "scope_key", "kind"],
    )
    # 旧版本已存在的 active scope 必须先获得状态行；否则生命周期读取会因
    # 缺少 enabled scope 而把历史记忆误判为不可见。
    op.execute(
        "INSERT INTO memory_scope_state "
        "(namespace_id, scope_key, revision, mutation_epoch, "
        "extraction_not_before, enabled, last_maintained_day, created_at, updated_at) "
        "SELECT namespace_id, scope_key, 1, 1, NULL, TRUE, NULL, "
        "MIN(created_at), MAX(updated_at) "
        "FROM memories "
        "GROUP BY namespace_id, scope_key "
        "ON CONFLICT (namespace_id, scope_key) DO NOTHING"
    )

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
    op.add_column("runs", sa.Column("memory_extraction_status", sa.String(16), nullable=True))
    op.add_column("runs", sa.Column("memory_extracted_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("memory_extraction_batch_id", sa.String(128)))
    op.add_column("runs", sa.Column("memory_extraction_safe_error_code", sa.String(128)))
    op.add_column(
        "runs",
        sa.Column(
            "memory_extraction_state",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
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


def downgrade() -> None:
    """删除新生命周期投影，保留旧三张记忆表原有正文和来源。"""
    op.drop_index("ix_runs_memory_owner_pending", table_name="runs")
    op.drop_index("ix_runs_memory_pending", table_name="runs")
    op.drop_constraint("ck_runs_memory_extraction_status", "runs", type_="check")
    op.drop_column("runs", "memory_extraction_state")
    op.drop_column("runs", "memory_extraction_safe_error_code")
    op.drop_column("runs", "memory_extraction_batch_id")
    op.drop_column("runs", "memory_extracted_at")
    op.drop_column("runs", "memory_extraction_status")
    op.drop_column("runs", "memory_input_message_ids")
    op.drop_column("runs", "memory_project_id")

    op.drop_index("ix_memory_jobs_scope_kind", table_name="memory_jobs")
    op.drop_index("ix_memory_jobs_lease", table_name="memory_jobs")
    op.drop_index("ix_memory_jobs_claim", table_name="memory_jobs")
    op.drop_table("memory_jobs")
    op.drop_table("memory_scope_state")
    op.drop_column("memory_settings", "auto_extract_boundary_at")
    op.drop_index("ix_memory_sources_source_at", table_name="memory_sources")
    op.drop_column("memory_sources", "source_at")

    op.drop_index("ix_memories_content_hash", table_name="memories")
    op.drop_index("ix_memories_context", table_name="memories")
    op.drop_index("ix_memories_fact_group", table_name="memories")
    op.drop_constraint("ck_memories_polarity", "memories", type_="check")
    op.drop_constraint("ck_memories_cardinality", "memories", type_="check")
    op.drop_constraint("ck_memories_load_policy", "memories", type_="check")
    op.drop_constraint("ck_memories_kind", "memories", type_="check")
    op.drop_constraint("ck_memories_origin", "memories", type_="check")
    op.drop_constraint("ck_memories_status", "memories", type_="check")
    op.drop_constraint("fk_memories_superseded_by", "memories", type_="foreignkey")
    for column_name in (
        "normalization_version",
        "applicability_key",
        "applicability",
        "polarity",
        "cardinality",
        "fact_value",
        "fact_key",
        "subject_key",
        "superseded_by_id",
        "last_evidenced_at",
        "explicit_long_term",
        "content_hash",
        "load_policy",
        "kind",
    ):
        op.drop_column("memories", column_name)
    # 旧 schema 要求这两个字段非空且只允许旧枚举。墓碑已清空正文和来源，
    # 降级时写入不含原内容的兼容哨兵；deleted_time 仍使墓碑不可读取。
    op.execute(
        "UPDATE memories SET embedding_profile_id = 'memory-tombstone-v1' "
        "WHERE deleted_time IS NOT NULL AND embedding_profile_id IS NULL"
    )
    op.execute(
        "UPDATE memories SET origin = 'manual' "
        "WHERE deleted_time IS NOT NULL AND origin IS NULL"
    )
    # 旧状态机无法表达 archived/superseded；降级保留正文时统一投影为 active。
    # 该兼容转换只服务旧版本读取，升级后不会自动恢复生命周期语义。
    op.execute(
        "UPDATE memories SET status = 'active' "
        "WHERE status IN ('archived', 'superseded')"
    )
    op.alter_column(
        "memories",
        "embedding_profile_id",
        existing_type=sa.String(128),
        nullable=False,
    )
    op.alter_column("memories", "origin", existing_type=sa.String(16), nullable=False)
    op.create_check_constraint(
        "ck_memories_status",
        "memories",
        "status IN ('pending','active')",
    )
    op.create_check_constraint(
        "ck_memories_origin",
        "memories",
        "origin IN ('manual','explicit','extracted')",
    )
