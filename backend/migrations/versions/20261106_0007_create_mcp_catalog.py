"""create MCP Server and tool catalog"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261106_0007"
down_revision: str | None = "20261106_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """创建 Server 与工具目录及其唯一性、安全状态约束。"""
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column("config_json", _JSON_DOCUMENT, nullable=False),
        sa.Column("secrets_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("secrets_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("key_version", sa.Integer(), nullable=True),
        sa.Column("config_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tools_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "transport IN ('streamable_http', 'stdio')",
            name="ck_mcp_servers_transport",
        ),
        sa.CheckConstraint("config_version >= 1", name="ck_mcp_servers_config_version"),
        sa.CheckConstraint(
            "key_version IS NULL OR key_version >= 1",
            name="ck_mcp_servers_key_version",
        ),
        sa.CheckConstraint(
            "(secrets_ciphertext IS NULL AND secrets_nonce IS NULL AND key_version IS NULL) "
            "OR (secrets_ciphertext IS NOT NULL AND secrets_nonce IS NOT NULL "
            "AND key_version IS NOT NULL)",
            name="ck_mcp_servers_secret_bundle",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_mcp_servers_name"),
    )
    op.create_table(
        "mcp_tools",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("server_id", sa.String(length=64), nullable=False),
        sa.Column("remote_name", sa.String(length=256), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=8192), nullable=False),
        sa.Column("input_schema_json", _JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "remote_name", name="uq_mcp_tools_server_remote"),
        sa.UniqueConstraint("model_name", name="uq_mcp_tools_model_name"),
    )


def downgrade() -> None:
    """按外键依赖顺序移除工具和 Server 表。"""
    op.drop_table("mcp_tools")
    op.drop_table("mcp_servers")
