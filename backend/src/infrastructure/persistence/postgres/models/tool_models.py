"""Tools/MCP 的 PostgreSQL ORM 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ToolModel(Base):
    """tools 表映射。"""

    __tablename__ = "tools"

    tool_name: Mapped[str] = mapped_column(String, primary_key=True)
    execution_mode: Mapped[str] = mapped_column(String)
    server_name: Mapped[str | None] = mapped_column(String, nullable=True)
    remote_name: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    parameters_schema_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    risk_level: Mapped[str] = mapped_column(String, default="medium")
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MCPServerModel(Base):
    """mcp_servers 表映射。"""

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
