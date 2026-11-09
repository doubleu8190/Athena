"""Create Run persistence tables and replace legacy message run-role uniqueness."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261111_0012"
down_revision: str | None = "20261110_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_STATUSES = "'queued', 'running', 'waiting_for_input', 'cancelling'"


def _json_type() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_unique_constraint("uq_sessions_id_owner", "sessions", ["id", "owner_id"])
    op.create_unique_constraint(
        "uq_conversation_messages_id_session",
        "conversation_messages",
        ["id", "session_id"],
    )
    op.add_column("conversation_messages", sa.Column("managed_run_id", sa.String(128)))
    op.add_column("execution_messages", sa.Column("managed_run_id", sa.String(128)))

    op.create_table(
        "runs",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("trigger_message_id", sa.String(64), nullable=False),
        sa.Column("context_cutoff_sequence", sa.BigInteger(), nullable=False),
        sa.Column("retry_of_run_id", sa.String(128)),
        sa.Column("model_config_version_id", sa.String(64), nullable=False),
        sa.Column("prompt_template_version", sa.String(64), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("execution_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("execution_started_at", sa.DateTime(timezone=True)),
        sa.Column("execution_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("safe_error_code", sa.String(128)),
        sa.Column("usage", _json_type(), nullable=False, server_default=sa.text("'{}'")),
        sa.CheckConstraint(
            f"status IN ({ACTIVE_STATUSES}, 'completed', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint("context_cutoff_sequence >= 0", name="ck_runs_context_cutoff"),
        sa.CheckConstraint("event_sequence >= 0", name="ck_runs_event_sequence"),
        sa.CheckConstraint("execution_epoch >= 0", name="ck_runs_execution_epoch"),
        sa.ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_runs_session_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_runs_trigger_message_session",
            deferrable=True,
            initially="DEFERRED",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_runs_retry_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_config_version_id"],
            ["llm_model_profile_versions.id"],
            name="fk_runs_model_profile",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "session_id", name="uq_runs_id_session_pair"),
    )
    op.create_index(
        "uq_runs_active_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({ACTIVE_STATUSES})"),
        sqlite_where=sa.text(f"status IN ({ACTIVE_STATUSES})"),
    )
    op.create_index("ix_runs_claim", "runs", ["status", "queued_at", "id"])
    op.create_index("ix_runs_lease", "runs", ["status", "lease_expires_at"])
    op.create_foreign_key(
        "fk_conversation_messages_managed_run_session",
        "conversation_messages",
        "runs",
        ["managed_run_id", "session_id"],
        ["id", "session_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_execution_messages_managed_run_session",
        "execution_messages",
        "runs",
        ["managed_run_id", "session_id"],
        ["id", "session_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "run_input_requests",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("question_message_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("answer_message_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'answered', 'closed')", name="ck_run_input_requests_status"
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_run_input_requests_run_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_run_input_requests_question_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["answer_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_run_input_requests_answer_session",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_run_input_requests_pending",
        "run_input_requests",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", _json_type(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_run_events_run_session",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "run_requests",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("result", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("operation IN ('submit', 'answer', 'retry')", name="ck_run_requests_op"),
        sa.UniqueConstraint(
            "owner_id", "session_id", "operation", "request_id", name="uq_run_requests_scope_key"
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["sessions.id", "sessions.owner_id"],
            name="fk_run_requests_session_owner",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_run_requests_session_created", "run_requests", ["session_id", "created_at"])
    op.drop_constraint("uq_conversation_messages_run_role", "conversation_messages", type_="unique")


def downgrade() -> None:
    conflict = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT session_id, run_id, role FROM conversation_messages "
                "WHERE run_id IS NOT NULL GROUP BY session_id, run_id, role "
                "HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if conflict is not None:
        raise RuntimeError(
            "Cannot restore uq_conversation_messages_run_role while duplicate rows exist; "
            "restore a database backup before downgrading."
        )

    op.create_unique_constraint(
        "uq_conversation_messages_run_role",
        "conversation_messages",
        ["session_id", "run_id", "role"],
    )
    op.drop_index("ix_run_requests_session_created", table_name="run_requests")
    op.drop_table("run_requests")
    op.drop_table("run_events")
    op.drop_index("uq_run_input_requests_pending", table_name="run_input_requests")
    op.drop_table("run_input_requests")
    op.drop_constraint(
        "fk_conversation_messages_managed_run_session",
        "conversation_messages",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_execution_messages_managed_run_session",
        "execution_messages",
        type_="foreignkey",
    )
    op.drop_index("ix_runs_lease", table_name="runs")
    op.drop_index("ix_runs_claim", table_name="runs")
    op.drop_index("uq_runs_active_session", table_name="runs")
    op.drop_table("runs")
    op.drop_column("conversation_messages", "managed_run_id")
    op.drop_column("execution_messages", "managed_run_id")
    op.drop_constraint(
        "uq_conversation_messages_id_session", "conversation_messages", type_="unique"
    )
    op.drop_constraint("uq_sessions_id_owner", "sessions", type_="unique")
