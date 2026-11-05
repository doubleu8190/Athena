"""create sessions and message streams"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261105_0002"
down_revision: str | None = "20261105_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column(
            "last_message_sequence",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_time", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sessions_owner_deleted_updated",
        "sessions",
        ["owner_id", "deleted_time", sa.text("updated_at DESC")],
        unique=False,
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "parts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("reply_to_message_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=128), nullable=True),
        sa.Column("client_message_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'failed', 'cancelled')",
            name="ck_conversation_messages_status",
        ),
        sa.ForeignKeyConstraint(
            ["reply_to_message_id"],
            ["conversation_messages.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "client_message_id",
            name="uq_conversation_messages_client",
        ),
        sa.UniqueConstraint(
            "session_id",
            "run_id",
            "role",
            name="uq_conversation_messages_run_role",
        ),
        sa.UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_conversation_messages_session_sequence",
        ),
    )
    op.create_index(
        "ix_conversation_messages_session_created",
        "conversation_messages",
        ["session_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_messages_session_role_sequence",
        "conversation_messages",
        ["session_id", "role", "sequence"],
        unique=False,
    )

    op.create_table(
        "execution_messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "parts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("tool_call_id", sa.String(length=128), nullable=True),
        sa.Column("source_message_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_execution_messages_role",
        ),
        sa.CheckConstraint(
            "(role = 'tool' AND tool_call_id IS NOT NULL) "
            "OR (role <> 'tool' AND tool_call_id IS NULL)",
            name="ck_execution_messages_tool_call",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["conversation_messages.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_execution_messages_session_sequence",
        ),
        sa.UniqueConstraint(
            "session_id",
            "run_id",
            "thread_id",
            "tool_call_id",
            name="uq_execution_messages_scope_tool",
        ),
    )
    op.create_index(
        "ix_execution_messages_run_sequence",
        "execution_messages",
        ["run_id", "sequence"],
        unique=False,
    )
    op.create_index(
        "ix_execution_messages_thread_sequence",
        "execution_messages",
        ["thread_id", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_messages_thread_sequence", table_name="execution_messages")
    op.drop_index("ix_execution_messages_run_sequence", table_name="execution_messages")
    op.drop_table("execution_messages")
    op.drop_index(
        "ix_conversation_messages_session_role_sequence",
        table_name="conversation_messages",
    )
    op.drop_index(
        "ix_conversation_messages_session_created",
        table_name="conversation_messages",
    )
    op.drop_table("conversation_messages")
    op.drop_index("ix_sessions_owner_deleted_updated", table_name="sessions")
    op.drop_table("sessions")
