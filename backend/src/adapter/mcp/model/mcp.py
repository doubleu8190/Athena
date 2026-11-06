"""MCP Server 与工具目录 PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from adapter.base import Base

_JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class McpServerModel(Base):
    """MCP Server 连接配置与加密凭据。"""

    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint(
            "transport IN ('streamable_http', 'stdio')",
            name="ck_mcp_servers_transport",
        ),
        CheckConstraint("config_version >= 1", name="ck_mcp_servers_config_version"),
        CheckConstraint(
            "key_version IS NULL OR key_version >= 1", name="ck_mcp_servers_key_version"
        ),
        CheckConstraint(
            "(secrets_ciphertext IS NULL AND secrets_nonce IS NULL AND key_version IS NULL) "
            "OR (secrets_ciphertext IS NOT NULL AND secrets_nonce IS NOT NULL "
            "AND key_version IS NOT NULL)",
            name="ck_mcp_servers_secret_bundle",
        ),
        UniqueConstraint("name", name="uq_mcp_servers_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    secrets_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secrets_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    key_version: Mapped[int | None] = mapped_column(nullable=True)
    config_version: Mapped[int] = mapped_column(nullable=False, server_default="1")
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tools_discovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class McpToolModel(Base):
    """一个 Server 最近一次提交的发现工具快照。"""

    __tablename__ = "mcp_tools"
    __table_args__ = (
        UniqueConstraint("server_id", "remote_name", name="uq_mcp_tools_server_remote"),
        UniqueConstraint("model_name", name="uq_mcp_tools_model_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    server_id: Mapped[str] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
    )
    remote_name: Mapped[str] = mapped_column(String(256), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(8192), nullable=False)
    input_schema_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["McpServerModel", "McpToolModel"]
