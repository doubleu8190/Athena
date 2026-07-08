"""Add summary and summary_offset to sessions table.

Revision ID: 003
Revises: 002
Create Date: 2026-07-06

These fields support context-aware summarization:
- summary: cumulative text summary of older messages
- summary_offset: number of messages already included in the summary
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_sessions_columns(conn) -> set[str]:
    """Return the set of column names in the sessions table."""
    result = conn.execute(sa.text("PRAGMA table_info('sessions')"))
    return {row[1] for row in result.fetchall()}


def upgrade() -> None:
    conn = op.get_bind()
    columns = _get_sessions_columns(conn)

    if "summary" not in columns:
        op.add_column("sessions", sa.Column("summary", sa.Text(), nullable=True))

    if "summary_offset" not in columns:
        op.add_column("sessions", sa.Column("summary_offset", sa.Integer(), server_default="0"))


def downgrade() -> None:
    conn = op.get_bind()
    columns = _get_sessions_columns(conn)

    if "summary_offset" in columns:
        op.drop_column("sessions", "summary_offset")

    if "summary" in columns:
        op.drop_column("sessions", "summary")
