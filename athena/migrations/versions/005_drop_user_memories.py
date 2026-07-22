"""Drop user_memories table — memories now live in ChromaDB only.

Revision ID: 005
Revises: 004
Create Date: 2026-07-11
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("user_memories")


def downgrade() -> None:
    op.create_table(
        "user_memories",
        sa.Column("memory_id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False, index=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("vector_id", sa.String(), nullable=True, index=True),
        sa.Column("sync_status", sa.String(), default="pending", index=True),
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )