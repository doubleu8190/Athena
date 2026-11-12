"""Persist the immutable user-safe Skill approval projection."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261118_0019"
down_revision: str | None = "20261117_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[Any]:
    """PostgreSQL 使用 JSONB，SQLite 迁移测试使用 JSON。"""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """保存已冻结审批摘要，后续查询不会回读最新 Skill 配置。"""
    op.add_column(
        "run_tool_calls",
        sa.Column("safe_skill_projection", _json_type(), nullable=True),
    )


def downgrade() -> None:
    """仅移除审批投影字段，不改变既有调用状态或文件。"""
    op.drop_column("run_tool_calls", "safe_skill_projection")
