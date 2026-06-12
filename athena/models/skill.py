"""Skill model — skills table.

Tracks installed Docker-based skills. Each skill runs an MCP server
inside a Docker container. Network access is controlled via allowed_domains
and a global Squid proxy.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class Skill(Base):
    __tablename__ = "skills"

    skill_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    image_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    allowed_domains: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Comma-separated, empty = no network
    container_id: Mapped[str | None] = mapped_column(String, nullable=True)
    mcp_server_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Associated mcp_servers record
    status: Mapped[str] = mapped_column(
        String, default="installed"
    )  # 'installing', 'installed', 'running', 'error', 'uninstalling'
    installed_at: Mapped[datetime] = mapped_column(server_default=func.now())
