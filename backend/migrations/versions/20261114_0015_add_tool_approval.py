"""Add tool approvals, parallel calls, and approval policy persistence."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261114_0015"
down_revision: str | None = "20261113_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_ACTIVE_STATUSES = "'queued', 'running', 'waiting_for_input', 'cancelling'"
_ACTIVE_STATUSES = (
    "'queued', 'running', 'waiting_for_input', 'waiting_for_approval', 'cancelling'"
)


def upgrade() -> None:
    op.add_column(
        "llm_model_profile_versions",
        sa.Column(
            "supports_parallel_tool_calls",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        "ck_llm_profile_supports_parallel_tool_calls",
        "llm_model_profile_versions",
        "supports_parallel_tool_calls IN (true, false)",
    )

    op.add_column(
        "mcp_tools",
        sa.Column(
            "requires_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.drop_index("uq_runs_active_session", table_name="runs")
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('queued', 'running', 'waiting_for_input', "
        "'waiting_for_approval', 'cancelling', 'completed', 'failed', 'cancelled')",
    )
    op.create_index(
        "uq_runs_active_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_ACTIVE_STATUSES})"),
        sqlite_where=sa.text(f"status IN ({_ACTIVE_STATUSES})"),
    )

    op.drop_constraint("ck_run_requests_op", "run_requests", type_="check")
    op.create_check_constraint(
        "ck_run_requests_op",
        "run_requests",
        "operation IN ('submit', 'answer', 'retry', 'decide_tool_approval')",
    )

    op.drop_constraint(
        "uq_run_tool_calls_call_message",
        "run_tool_calls",
        type_="unique",
    )
    op.drop_constraint("ck_run_tool_calls_status", "run_tool_calls", type_="check")
    op.create_check_constraint(
        "ck_run_tool_calls_status",
        "run_tool_calls",
        "status IN ('pending_approval', 'approved', 'executing', "
        "'completed', 'failed', 'cancelled', 'unknown')",
    )
    op.alter_column(
        "run_tool_calls",
        "started_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.add_column(
        "run_tool_calls",
        sa.Column(
            "requires_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "run_tool_calls",
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "run_tool_calls",
        sa.Column("approval_decision", sa.String(8), nullable=True),
    )
    op.add_column(
        "run_tool_calls",
        sa.Column("approval_decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE run_tool_calls SET requested_at = started_at WHERE requested_at IS NULL"
    )
    op.create_check_constraint(
        "ck_run_tool_calls_approval_decision",
        "run_tool_calls",
        "approval_decision IS NULL OR approval_decision IN ('approve', 'reject')",
    )
    op.create_index(
        "ix_run_tool_calls_call_message",
        "run_tool_calls",
        ["call_message_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM run_tool_calls "
            "WHERE status NOT IN ('executing', 'completed', 'failed', 'cancelled', 'unknown') "
            "OR started_at IS NULL LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "Cannot downgrade while approval-era tool calls have no legacy status/start time."
        )
    if bind.execute(
        sa.text(
            "SELECT call_message_id FROM run_tool_calls "
            "GROUP BY call_message_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first():
        raise RuntimeError(
            "Cannot restore one-call-per-message uniqueness while batches exist."
        )
    if bind.execute(
        sa.text("SELECT 1 FROM run_requests WHERE operation = 'decide_tool_approval' LIMIT 1")
    ).first():
        raise RuntimeError("Cannot downgrade after tool approval decisions were recorded.")
    if bind.execute(
        sa.text("SELECT 1 FROM runs WHERE status = 'waiting_for_approval' LIMIT 1")
    ).first():
        raise RuntimeError("Cannot downgrade while a Run is waiting for approval.")

    op.drop_index("ix_run_tool_calls_call_message", table_name="run_tool_calls")
    op.drop_constraint(
        "ck_run_tool_calls_approval_decision",
        "run_tool_calls",
        type_="check",
    )
    op.drop_column("run_tool_calls", "approval_decided_at")
    op.drop_column("run_tool_calls", "approval_decision")
    op.drop_column("run_tool_calls", "requested_at")
    op.drop_column("run_tool_calls", "requires_approval")
    op.alter_column(
        "run_tool_calls",
        "started_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.drop_constraint("ck_run_tool_calls_status", "run_tool_calls", type_="check")
    op.create_check_constraint(
        "ck_run_tool_calls_status",
        "run_tool_calls",
        "status IN ('executing', 'completed', 'failed', 'cancelled', 'unknown')",
    )
    op.create_unique_constraint(
        "uq_run_tool_calls_call_message",
        "run_tool_calls",
        ["call_message_id"],
    )

    op.drop_constraint("ck_run_requests_op", "run_requests", type_="check")
    op.create_check_constraint(
        "ck_run_requests_op",
        "run_requests",
        "operation IN ('submit', 'answer', 'retry')",
    )

    op.drop_index("uq_runs_active_session", table_name="runs")
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('queued', 'running', 'waiting_for_input', 'cancelling', "
        "'completed', 'failed', 'cancelled')",
    )
    op.create_index(
        "uq_runs_active_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_OLD_ACTIVE_STATUSES})"),
        sqlite_where=sa.text(f"status IN ({_OLD_ACTIVE_STATUSES})"),
    )

    op.drop_column("mcp_tools", "requires_approval")
    op.drop_constraint(
        "ck_llm_profile_supports_parallel_tool_calls",
        "llm_model_profile_versions",
        type_="check",
    )
    op.drop_column("llm_model_profile_versions", "supports_parallel_tool_calls")
