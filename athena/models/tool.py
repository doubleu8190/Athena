"""Tool model — tools table.

Registered tools from all MCP servers. Each tool has an independent state
machine (active/stale/disabled) separate from its parent server's connection
status. Global uniqueness is by (name, source_server_id) combination.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, String, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class Tool(Base):
    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    version: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parameters_schema: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    risk_level: Mapped[str] = mapped_column(
        String, default="medium"
    )  # 'low', 'medium', 'high', 'critical'
    idempotent: Mapped[bool] = mapped_column(Boolean, default=True)
    capability_tags: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON array, e.g. '["web_search", "text_retrieval"]'
    source: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'builtin', 'skill', 'external'
    source_server_id: Mapped[str] = mapped_column(
        String, ForeignKey("mcp_servers.server_id"), nullable=False
    )
    handler_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Internal routing: depends on source:
    #   'builtin': module path e.g. "athena.tools.filesystem.read_file"
    #   'skill': MCP tool name e.g. "pdf_extract"
    #   'external': remote tool name e.g. "github_search_repos"
    status: Mapped[str] = mapped_column(
        String, default="active"
    )  # 'active', 'stale', 'disabled'
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
