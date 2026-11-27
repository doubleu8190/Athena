"""为 mcp_tools 增加 result_labels_json 白名单结果标签映射列。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_add_mcp_tool_result_labels"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 mcp_tools 新增结果字段中文白名单映射列，默认空对象确保向后兼容。"""
    op.add_column(
        "mcp_tools",
        sa.Column(
            "result_labels_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    """回滚 mcp_tools.result_labels_json 列。"""
    op.drop_column("mcp_tools", "result_labels_json")
