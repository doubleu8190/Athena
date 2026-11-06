"""add user role"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261106_0006"
down_revision: str | None = "20261105_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为既有用户补入普通用户角色并约束允许值。"""
    op.add_column("users", sa.Column("role", sa.String(length=16), nullable=True))
    op.execute("UPDATE users SET role = 'user'")
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=16),
        nullable=False,
        server_default="user",
    )
    op.create_check_constraint("ck_users_role", "users", "role IN ('admin', 'user')")


def downgrade() -> None:
    """移除用户角色字段及其约束。"""
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.drop_column("users", "role")
