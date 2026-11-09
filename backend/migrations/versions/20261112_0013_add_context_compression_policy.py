"""Add administrator managed context compression policies."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "20261112_0013"
down_revision: str | None = "20261111_0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("context_turn_id", sa.String(64), nullable=True),
    )
    op.create_table(
        "llm_compression_policy_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("compression_model_profile_id", sa.String(64), nullable=False),
        sa.Column("trigger_ratio", sa.Float(), nullable=False),
        sa.Column("recent_ratio", sa.Float(), nullable=False),
        sa.Column("summary_ratio", sa.Float(), nullable=False),
        sa.Column("prompt_template_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("trigger_ratio > 0 AND trigger_ratio < 1", name="ck_compression_trigger"),
        sa.CheckConstraint("recent_ratio > 0 AND recent_ratio < 1", name="ck_compression_recent"),
        sa.CheckConstraint("summary_ratio > 0 AND summary_ratio < 1", name="ck_compression_summary"),
        sa.ForeignKeyConstraint(
            ["compression_model_profile_id"],
            ["llm_model_profile_versions.id"],
            name="fk_compression_policy_model",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "llm_current_compression_policy",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column("policy_version_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["llm_compression_policy_versions.id"],
            name="fk_current_compression_policy_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("singleton_id = 1", name="ck_current_compression_policy_singleton"),
    )
    op.add_column("runs", sa.Column("compression_policy_version_id", sa.String(64)))
    op.create_foreign_key(
        "fk_runs_compression_policy",
        "runs",
        "llm_compression_policy_versions",
        ["compression_policy_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "session_context_summaries",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("covered_through_sequence", sa.BigInteger(), nullable=False),
        sa.Column("parent_summary_id", sa.String(128)),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("execution_epoch", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("policy_version_id", sa.String(64), nullable=False),
        sa.Column("compression_model_version_id", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("covered_through_sequence > 0", name="ck_context_summary_coverage"),
        sa.CheckConstraint("execution_epoch > 0", name="ck_context_summary_epoch"),
        sa.UniqueConstraint("id", "session_id", name="uq_context_summaries_id_session"),
        sa.UniqueConstraint("run_id", "operation_id", name="uq_context_summary_run_operation"),
        sa.ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_context_summaries_session_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_summary_id", "session_id"],
            ["session_context_summaries.id", "session_context_summaries.session_id"],
            name="fk_context_summaries_parent_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_context_summaries_run_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["llm_compression_policy_versions.id"],
            name="fk_context_summaries_policy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["compression_model_version_id"],
            ["llm_model_profile_versions.id"],
            name="fk_context_summaries_model",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_context_summaries_session_coverage",
        "session_context_summaries",
        ["session_id", "owner_id", "covered_through_sequence", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_context_summaries_session_coverage", table_name="session_context_summaries")
    op.drop_table("session_context_summaries")
    op.drop_constraint("fk_runs_compression_policy", "runs", type_="foreignkey")
    op.drop_column("runs", "compression_policy_version_id")
    op.drop_table("llm_current_compression_policy")
    op.drop_table("llm_compression_policy_versions")
    op.drop_column("conversation_messages", "context_turn_id")
