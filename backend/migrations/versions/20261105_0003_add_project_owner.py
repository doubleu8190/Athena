"""add project owner"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261105_0003"
down_revision: str | None = "20261105_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS owner_id VARCHAR(128)"))
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM projects WHERE owner_id IS NULL) THEN
                    RAISE EXCEPTION
                        'projects.owner_id must be backfilled before upgrade';
                END IF;
            END
            $$;
            """
        )
    )
    op.execute(sa.text("ALTER TABLE projects ALTER COLUMN owner_id SET NOT NULL"))
    op.drop_index("ix_projects_active_updated", table_name="projects")
    op.create_index(
        "ix_projects_owner_active_updated",
        "projects",
        ["owner_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        unique=False,
        postgresql_where=sa.text("deleted_time IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_projects_owner_active_updated", table_name="projects")
    op.create_index(
        "ix_projects_active_updated",
        "projects",
        [sa.text("updated_at DESC"), sa.text("id DESC")],
        unique=False,
        postgresql_where=sa.text("deleted_time IS NULL"),
    )
    op.drop_column("projects", "owner_id")
