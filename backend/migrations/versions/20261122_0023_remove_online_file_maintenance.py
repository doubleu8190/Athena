"""移除在线对象发布预留和持久回收游标。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261122_0023"
down_revision: str | None = "20261121_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """删除只服务在线回收的两张维护表。"""
    op.drop_table("file_object_reclamation_cursors")
    op.drop_table("file_object_publications")


def downgrade() -> None:
    """恢复停写维护窗口之前的两张表结构，不回填业务数据。"""
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
    cursor_table = op.create_table(
        "file_object_reclamation_cursors",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("after_key", sa.String(1024), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.bulk_insert(
        cursor_table,
        [{"id": "file-orphan-objects", "after_key": None, "generation": 0}],
    )
