"""Tools/MCP 的 PostgreSQL ORM 模型。"""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
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
    parameters_schema_json: Mapped[str] = mapped_column(Text, default="{}")
    risk_level: Mapped[str] = mapped_column(String, default="medium")
    require_approval: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class MCPServerModel(Base):
    """mcp_servers 表映射。"""

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)
