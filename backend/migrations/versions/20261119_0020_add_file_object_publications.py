"""为跨阶段文件对象发布增加持久化预留。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261119_0020"
down_revision: str | None = "20261118_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增对象键唯一预留表；已有引用不由迁移补写保护行。"""
    op.create_table(
        "file_object_publications",
        sa.Column("object_key", sa.String(1024), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_host_id", sa.String(256)),
        sa.Column("owner_boot_id", sa.String(256)),
        sa.Column("owner_process_id", sa.Integer()),
        sa.Column("owner_process_group_id", sa.Integer()),
        sa.Column("owner_process_start_time", sa.String(64)),
    )


def downgrade() -> None:
    """只移除预留结构；对象正文仍交由部署回收流程处理。"""
    op.drop_table("file_object_publications")
