"""M1.6 管理后台核心运维与工具业务化审批元信息：模型乐观锁、Run 快照、MCP 启停与软删、审批映射。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261204_0037"
down_revision: str | None = "20261203_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    """在 PostgreSQL 使用 JSONB，在 SQLite 迁移测试中使用 JSON。"""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """扩展模型版本、Run、MCP Server 与 MCP 工具表，支撑可编辑、软删除与业务化审批投影。"""
    # 1. 扩展 llm_model_profile_versions：新增乐观锁 version 与 updated_at。
    op.add_column(
        "llm_model_profile_versions",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        "ck_llm_profile_version",
        "llm_model_profile_versions",
        "version >= 1",
    )
    op.add_column(
        "llm_model_profile_versions",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE llm_model_profile_versions SET updated_at = created_at WHERE updated_at IS NULL"
    )
    op.alter_column(
        "llm_model_profile_versions",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )

    # 2. 扩展 runs：固化创建 Run 时的非密钥模型元数据快照，解耦后续编辑/软删除对历史审计的影响。
    op.add_column(
        "runs",
        sa.Column("model_snapshot_json", _json_type(), nullable=True),
    )

    # 3. 扩展 mcp_servers：新增启停开关与软删标记，并将名称唯一约束改为部分唯一索引。
    op.add_column(
        "mcp_servers",
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "mcp_servers",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("uq_mcp_servers_name", "mcp_servers", type_="unique")
    op.create_index(
        "uq_mcp_servers_name_active",
        "mcp_servers",
        ["name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    # 4. 扩展 mcp_tools：新增面向业务用户的中文展示名、审批摘要模板及参数中文标签映射。
    op.add_column(
        "mcp_tools",
        sa.Column("display_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "mcp_tools",
        sa.Column("approval_summary_template", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "mcp_tools",
        sa.Column(
            "parameter_labels_json",
            _json_type(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    """按逆序回滚 mcp_tools、mcp_servers、runs 与 llm_model_profile_versions 表变更。"""
    # 1. 回滚 mcp_tools 新增列。
    op.drop_column("mcp_tools", "parameter_labels_json")
    op.drop_column("mcp_tools", "approval_summary_template")
    op.drop_column("mcp_tools", "display_name")

    # 2. 回滚 mcp_servers 部分唯一索引与新增列。
    op.drop_index("uq_mcp_servers_name_active", table_name="mcp_servers")
    op.create_unique_constraint("uq_mcp_servers_name", "mcp_servers", ["name"])
    op.drop_column("mcp_servers", "deleted_at")
    op.drop_column("mcp_servers", "enabled")

    # 3. 回滚 runs 模型快照列。
    op.drop_column("runs", "model_snapshot_json")

    # 4. 回滚 llm_model_profile_versions 更新时间与乐观锁版本列。
    op.drop_column("llm_model_profile_versions", "updated_at")
    op.drop_constraint(
        "ck_llm_profile_version",
        "llm_model_profile_versions",
        type_="check",
    )
    op.drop_column("llm_model_profile_versions", "version")
