"""放宽 skill_versions.object_key 为可空，支持纯 Markdown 技能脱离 MinIO 直接存库。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261209_0042"
down_revision: str | None = "20261208_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """放宽 skill_versions.object_key 为可空（PG 与 SQLite 的 UNIQUE 均允许多行 NULL）。"""
    with op.batch_alter_table("skill_versions") as batch_op:
        batch_op.alter_column(
            "object_key",
            existing_type=sa.String(length=1024),
            nullable=True,
        )


def downgrade() -> None:
    """降级恢复 NOT NULL 前先核验无 NULL 记录，避免静默删数据或触发不透明约束报错。"""
    if not bool(op.get_context().as_sql):
        bind = op.get_bind()
        has_null_object_key = bool(
            bind.execute(
                sa.text("SELECT EXISTS (SELECT 1 FROM skill_versions WHERE object_key IS NULL)")
            ).scalar_one()
        )
        if has_null_object_key:
            raise RuntimeError("cannot_downgrade_skill_versions_with_null_object_key")

    with op.batch_alter_table("skill_versions") as batch_op:
        batch_op.alter_column(
            "object_key",
            existing_type=sa.String(length=1024),
            nullable=False,
        )
