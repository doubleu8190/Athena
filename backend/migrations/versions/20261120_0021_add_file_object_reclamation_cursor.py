"""持久化跨 Worker 的文件对象回收游标。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261120_0021"
down_revision: str | None = "20261119_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建单条全局游标行，后续升级不会重置现有扫描位置。"""
    cursor_table = op.create_table(
        "file_object_reclamation_cursors",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("after_key", sa.String(1024), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.bulk_insert(
        cursor_table,
        [{"id": "file-orphan-objects", "after_key": None}],
    )


def downgrade() -> None:
    """删除回收游标；对象和数据库引用不受影响。"""
    op.drop_table("file_object_reclamation_cursors")
