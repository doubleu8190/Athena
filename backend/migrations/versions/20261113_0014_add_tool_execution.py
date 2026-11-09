"""Add Run tool snapshots, LLM request counters, and tool-call records."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261113_0014"
down_revision: str | None = "20261112_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("runs", sa.Column("tool_snapshot", _json_type()))
    op.add_column("runs", sa.Column("auth_session_id", sa.String(128)))
    op.add_column(
        "runs",
        sa.Column(
            "llm_request_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_runs_llm_request_count",
        "runs",
        "llm_request_count >= 0 AND llm_request_count <= 40",
    )
    op.create_unique_constraint(
        "uq_execution_messages_id_session",
        "execution_messages",
        ["id", "session_id"],
    )
    op.create_table(
        "run_tool_calls",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tool_call_id", sa.String(256), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("tool_id", sa.String(64)),
        sa.Column("tool_kind", sa.String(16)),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("execution_epoch", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("call_message_id", sa.String(64), nullable=False),
        sa.Column("result_message_id", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("safe_error_code", sa.String(128)),
        sa.Column("outcome", _json_type()),
        sa.Column("round", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("ordinal > 0", name="ck_run_tool_calls_ordinal"),
        sa.CheckConstraint("execution_epoch > 0", name="ck_run_tool_calls_epoch"),
        sa.CheckConstraint(
            "status IN ('executing', 'completed', 'failed', 'cancelled', 'unknown')",
            name="ck_run_tool_calls_status",
        ),
        sa.CheckConstraint(
            "tool_kind IS NULL OR tool_kind IN ('builtin', 'mcp')",
            name="ck_run_tool_calls_kind",
        ),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_run_tool_calls_protocol_id"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_run_tool_calls_ordinal"),
        sa.UniqueConstraint("call_message_id", name="uq_run_tool_calls_call_message"),
        sa.UniqueConstraint("result_message_id", name="uq_run_tool_calls_result_message"),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_run_tool_calls_run_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["call_message_id", "session_id"],
            ["execution_messages.id", "execution_messages.session_id"],
            name="fk_run_tool_calls_call_message_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_message_id", "session_id"],
            ["execution_messages.id", "execution_messages.session_id"],
            name="fk_run_tool_calls_result_message_session",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_run_tool_calls_run_ordinal",
        "run_tool_calls",
        ["run_id", "ordinal"],
    )
    op.create_index(
        "ix_run_tool_calls_run_status",
        "run_tool_calls",
        ["run_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_tool_calls_run_status", table_name="run_tool_calls")
    op.drop_index("ix_run_tool_calls_run_ordinal", table_name="run_tool_calls")
    op.drop_table("run_tool_calls")
    op.drop_constraint(
        "uq_execution_messages_id_session",
        "execution_messages",
        type_="unique",
    )
    op.drop_constraint("ck_runs_llm_request_count", "runs", type_="check")
    op.drop_column("runs", "llm_request_count")
    op.drop_column("runs", "auth_session_id")
    op.drop_column("runs", "tool_snapshot")
