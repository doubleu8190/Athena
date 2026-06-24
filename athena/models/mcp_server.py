"""MCPServer model — mcp_servers table.

Registration records for MCP servers (builtin, skill, external).
connection_config is a JSON column storing transport and auth details.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    server_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    transport: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'stdio', 'http', 'sse'
    connection_config: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # JSON — transport + auth config
    # Common JSON fields:
    #   auth_type: "none" | "static" | "oauth2_client"
    #   static: auth_token_env (env var name)
    #   oauth2_client: authorization_endpoint, token_endpoint,
    #                  client_id, client_secret_env, scopes, encrypted_token (AES-256-GCM)
    #   stdio: command
    #   http: url
    enabled: Mapped[bool] = mapped_column(
        default=True
    )  # Admin intent: whether this server should be connected
    source: Mapped[str] = mapped_column(
        String, default="external"
    )  # 'builtin', 'skill', 'external'
    registered_at: Mapped[datetime] = mapped_column(server_default=func.now())
