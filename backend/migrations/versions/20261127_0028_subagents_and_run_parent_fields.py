"""新增 subagents 配置表并在 runs 表预留 Subagent 父子关联字段。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261127_0028"
down_revision: str | None = "20261126_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[object]:
    """在 PostgreSQL 使用 JSONB，在其他方言退化为 JSON。"""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """创建 subagents 表（含部分唯一索引）并扩展 runs 表预留字段。"""
    op.create_table(
        "subagents",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model_profile_id", sa.String(length=64), nullable=True),
        sa.Column(
            "allowed_tool_ids",
            _json_type(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("max_llm_requests", sa.Integer(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_subagents_version"),
        sa.CheckConstraint(
            "max_llm_requests IS NULL OR (max_llm_requests >= 1 AND max_llm_requests <= 50)",
            name="ck_subagents_max_llm_requests",
        ),
        sa.CheckConstraint(
            "timeout_seconds IS NULL OR (timeout_seconds >= 10 AND timeout_seconds <= 1800)",
            name="ck_subagents_timeout_seconds",
        ),
    )
    op.create_index(
        "uq_subagents_slug_active",
        "subagents",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_subagents_enabled_created",
        "subagents",
        ["enabled", "created_at", "id"],
    )

    op.add_column("runs", sa.Column("parent_run_id", sa.String(length=128), nullable=True))
    op.add_column("runs", sa.Column("root_run_id", sa.String(length=128), nullable=True))
    op.add_column("runs", sa.Column("parent_tool_call_id", sa.String(length=256), nullable=True))
    op.add_column(
        "runs",
        sa.Column("depth", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("runs", sa.Column("subagent_profile_id", sa.String(length=64), nullable=True))
    op.add_column("runs", sa.Column("subagent_slug", sa.String(length=64), nullable=True))
    op.add_column("runs", sa.Column("subagent_name", sa.String(length=100), nullable=True))
    op.create_index("ix_runs_parent_run_id", "runs", ["parent_run_id"])
    op.create_index("ix_runs_root_run_id", "runs", ["root_run_id"])


def downgrade() -> None:
    """移除 runs 预留字段及 subagents 表。"""
    op.drop_index("ix_runs_root_run_id", table_name="runs")
    op.drop_index("ix_runs_parent_run_id", table_name="runs")
    op.drop_column("runs", "subagent_name")
    op.drop_column("runs", "subagent_slug")
    op.drop_column("runs", "subagent_profile_id")
    op.drop_column("runs", "depth")
    op.drop_column("runs", "parent_tool_call_id")
    op.drop_column("runs", "root_run_id")
    op.drop_column("runs", "parent_run_id")

    op.drop_index("ix_subagents_enabled_created", table_name="subagents")
    op.drop_index("uq_subagents_slug_active", table_name="subagents")
    op.drop_table("subagents")
