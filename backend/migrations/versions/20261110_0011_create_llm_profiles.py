"""Create immutable LLM profile versions and default selection."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261110_0011"
down_revision: str | None = "20261109_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_model_profile_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("credential_env", sa.String(128), nullable=False),
        sa.Column("context_window_tokens", sa.Integer(), nullable=False),
        sa.Column("tokenizer_encoding", sa.String(32), nullable=False),
        sa.Column("supports_stream", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("supports_tools", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("request_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("execution_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("temperature", sa.Float()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("protocol = 'chat_completions'", name="ck_llm_profile_protocol"),
        sa.CheckConstraint("context_window_tokens > 0", name="ck_llm_profile_context_window"),
        sa.CheckConstraint(
            "tokenizer_encoding IN ('cl100k_base', 'o200k_base')",
            name="ck_llm_profile_tokenizer_encoding",
        ),
        sa.CheckConstraint(
            "supports_stream IN (true, false)", name="ck_llm_profile_supports_stream"
        ),
        sa.CheckConstraint("supports_tools IN (true, false)", name="ck_llm_profile_supports_tools"),
        sa.CheckConstraint("max_output_tokens > 0", name="ck_llm_profile_output_tokens"),
        sa.CheckConstraint("request_timeout_seconds > 0", name="ck_llm_profile_request_timeout"),
        sa.CheckConstraint(
            "execution_timeout_seconds > 0", name="ck_llm_profile_execution_timeout"
        ),
        sa.CheckConstraint("max_concurrency > 0", name="ck_llm_profile_concurrency"),
    )
    op.create_table(
        "llm_default_model_profile",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column(
            "profile_id",
            sa.String(64),
            sa.ForeignKey("llm_model_profile_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint("singleton_id = 1", name="ck_llm_default_singleton"),
    )


def downgrade() -> None:
    op.drop_table("llm_default_model_profile")
    op.drop_table("llm_model_profile_versions")
