"""M1.5 对话工作台底座升级：项目指令与默认技能、Run 显式知识库范围、场景化模型目录与软删除铺垫。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261203_0036"
down_revision: str | None = "20261202_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    """在 PostgreSQL 使用 JSONB，在 SQLite 迁移测试中使用 JSON。"""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """扩展 projects、runs 与 llm_model_profile_versions 表以支持 M1.5 对话能力升级。"""
    # 1. 扩展 projects 表：新增项目简介、背景指令与默认技能集合。
    op.add_column(
        "projects",
        sa.Column("description", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("instructions", sa.Text(), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "default_skill_ids",
            _json_type(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )

    # 2. 扩展 runs 表：新增可空 knowledge_base_ids
    # （NULL=企业默认范围，[]=显式关闭，非空=显式限定）。
    op.add_column(
        "runs",
        sa.Column("knowledge_base_ids", _json_type(), nullable=True),
    )

    # 3. 扩展 llm_model_profile_versions 表：新增用户可见性、场景化标签/说明、视觉能力及软删除列。
    op.add_column(
        "llm_model_profile_versions",
        sa.Column(
            "visible_to_users",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "llm_model_profile_versions",
        sa.Column("scenario_label", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "llm_model_profile_versions",
        sa.Column("scenario_description", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "llm_model_profile_versions",
        sa.Column(
            "supports_vision",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "llm_model_profile_versions",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """按逆序回滚 llm_model_profile_versions、runs 与 projects 表的新增列。"""
    # 1. 回滚 llm_model_profile_versions 新增列。
    op.drop_column("llm_model_profile_versions", "deleted_at")
    op.drop_column("llm_model_profile_versions", "supports_vision")
    op.drop_column("llm_model_profile_versions", "scenario_description")
    op.drop_column("llm_model_profile_versions", "scenario_label")
    op.drop_column("llm_model_profile_versions", "visible_to_users")

    # 2. 回滚 runs 新增列。
    op.drop_column("runs", "knowledge_base_ids")

    # 3. 回滚 projects 新增列。
    op.drop_column("projects", "default_skill_ids")
    op.drop_column("projects", "instructions")
    op.drop_column("projects", "description")
