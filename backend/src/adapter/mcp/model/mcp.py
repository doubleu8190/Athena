"""MCP Server 与工具目录 PostgreSQL ORM 映射。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base, json_column_type

_JSON_DOCUMENT = json_column_type()


class McpServerModel(Base):
    """MCP Server 连接配置、加密凭据与组织可见范围。"""

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
        CheckConstraint(
            "visibility_scope IN ('company', 'department')",
            name="ck_mcp_servers_visibility_scope",
        ),
        CheckConstraint(
            "(visibility_scope = 'company' AND owner_department_id IS NULL) "
            "OR (visibility_scope = 'department' AND owner_department_id IS NOT NULL)",
            name="ck_mcp_servers_owner_department_scope",
        ),
        # 仅对未软删除行强制名称唯一，允许软删除后复用同名重新注册。
        Index(
            "uq_mcp_servers_name_active",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_mcp_servers_visibility_dept", "visibility_scope", "owner_department_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    secrets_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secrets_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    key_version: Mapped[int | None] = mapped_column(nullable=True)
    config_version: Mapped[int] = mapped_column(nullable=False, server_default="1")
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    visibility_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="company", server_default="company"
    )
    owner_department_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
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
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 面向业务审批人展示的中文别名、审批摘要模板与参数标签映射。
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approval_summary_template: Mapped[str | None] = mapped_column(String(500), nullable=True)
    parameter_labels_json: Mapped[dict[str, str]] = mapped_column(
        _JSON_DOCUMENT,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["McpServerModel", "McpToolModel"]
