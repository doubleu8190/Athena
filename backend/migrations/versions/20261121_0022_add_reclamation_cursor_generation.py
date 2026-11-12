"""为文件对象回收游标增加防 ABA 的单调代次。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261121_0022"
down_revision: str | None = "20261120_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """现有游标从代次零开始，后续每次推进或回卷均递增。"""
    op.add_column(
        "file_object_reclamation_cursors",
        sa.Column(
            "generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    """移除代次；对象引用和扫描位置本身保持不变。"""
    op.drop_column("file_object_reclamation_cursors", "generation")
