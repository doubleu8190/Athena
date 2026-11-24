"""M3.3 技能在线 Markdown 双形态与首页快捷应用元数据（format / featured / starter_prompt）。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261206_0039"
down_revision: str | None = "20261205_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERSONAL_FORMAT_AND_FEATURED_SQL = (
    "(scope != 'personal') OR (format = 'markdown' AND NOT featured)"
)


def upgrade() -> None:
    """为 skills 表新增 format、featured、starter_prompt 列，回填个人技能格式并施加约束。"""
    # 第一阶段：先使用 batch_alter_table 新增 3 列，确保 PostgreSQL（在线/离线 SQL）
    # 与 SQLite 在线迁移均能安全添加默认值列而不立即触发个人技能组合约束。
    with op.batch_alter_table("skills") as batch_op:
        batch_op.add_column(
            sa.Column(
                "format",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'package'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "featured",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "starter_prompt",
                sa.String(length=500),
                nullable=True,
            )
        )

    # 第二阶段：在施加组合 CheckConstraint 前，先将既有 personal 技能回填为 markdown，
    # 避免存量个人技能行因默认 format='package' 违反个人技能格式不变式。
    op.execute(sa.text("UPDATE skills SET format = 'markdown' WHERE scope = 'personal'"))

    # 第三阶段：施加格式白名单约束与个人技能格式/首页推荐互斥约束
    # （NOT featured 表达同时兼容 PostgreSQL 与 SQLite）。
    with op.batch_alter_table("skills") as batch_op:
        batch_op.create_check_constraint(
            "ck_skills_format",
            "format IN ('markdown', 'package')",
        )
        batch_op.create_check_constraint(
            "ck_skills_personal_format_and_featured",
            _PERSONAL_FORMAT_AND_FEATURED_SQL,
        )


def downgrade() -> None:
    """按逆序移除 skills 的双形态与推荐约束及 3 个新增列。"""
    with op.batch_alter_table("skills") as batch_op:
        batch_op.drop_constraint(
            "ck_skills_personal_format_and_featured",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_skills_format",
            type_="check",
        )
        batch_op.drop_column("starter_prompt")
        batch_op.drop_column("featured")
        batch_op.drop_column("format")
