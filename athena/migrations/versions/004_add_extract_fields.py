"""Add last_extracted_message_count to sessions table.

Revision ID: 004
Revises: 003
Create Date: 2026-07-09

Tracks how many messages existed at the time of the last conversation
extraction, so the extraction task can skip runs with too few new messages.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_sessions_columns(conn) -> set[str]:
    """Return the set of column names in the sessions table."""
    result = conn.execute(sa.text("PRAGMA table_info('sessions')"))
    return {row[1] for row in result.fetchall()}


def upgrade() -> None:
    conn = op.get_bind()
    columns = _get_sessions_columns(conn)

    if "last_extracted_message_count" not in columns:
        op.add_column(
            "sessions",
            sa.Column(
                "last_extracted_message_count",
                sa.Integer(),
                server_default="0",
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    columns = _get_sessions_columns(conn)

    if "last_extracted_message_count" in columns:
        op.drop_column("sessions", "last_extracted_message_count")
