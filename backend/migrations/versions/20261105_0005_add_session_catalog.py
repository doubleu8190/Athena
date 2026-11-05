"""add session catalog fields and ownership constraints"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261105_0005"
down_revision: str | None = "20261105_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_sessions_owner_deleted_updated", table_name="sessions")
    op.add_column(
        "sessions",
        sa.Column("title", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("project_id", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_sessions_last_message_sequence",
        "sessions",
        "last_message_sequence >= 0",
    )
    op.create_check_constraint(
        "ck_sessions_title_length",
        "sessions",
        "title IS NULL OR length(title) <= 120",
    )
    op.create_unique_constraint(
        "uq_projects_id_owner",
        "projects",
        ["id", "owner_id"],
    )
    op.create_foreign_key(
        "fk_sessions_project_owner_projects",
        "sessions",
        "projects",
        ["project_id", "owner_id"],
        ["id", "owner_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_sessions_owner_active_updated",
        "sessions",
        ["owner_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        unique=False,
        postgresql_where=sa.text("deleted_time IS NULL"),
    )
    op.create_index(
        "ix_sessions_owner_project_active_updated",
        "sessions",
        ["owner_id", "project_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        unique=False,
        postgresql_where=sa.text("deleted_time IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sessions_owner_project_active_updated",
        table_name="sessions",
    )
    op.drop_index("ix_sessions_owner_active_updated", table_name="sessions")
    op.drop_constraint(
        "fk_sessions_project_owner_projects",
        "sessions",
        type_="foreignkey",
    )
    op.drop_constraint("uq_projects_id_owner", "projects", type_="unique")
    op.drop_constraint(
        "ck_sessions_title_length",
        "sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_sessions_last_message_sequence",
        "sessions",
        type_="check",
    )
    op.drop_column("sessions", "project_id")
    op.drop_column("sessions", "title")
    op.create_index(
        "ix_sessions_owner_deleted_updated",
        "sessions",
        ["owner_id", "deleted_time", sa.text("updated_at DESC")],
        unique=False,
    )
