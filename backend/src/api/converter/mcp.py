"""将 MCP 目录领域投影转换为管理 API DTO。"""

from __future__ import annotations

from typing import Any, Literal, cast

from api.dto.access import grants_to_response
from api.dto.mcp import McpServerListResponse, McpServerResponse
from domain.mcp import McpServer, McpServerPage, StdioConfig, StreamableHttpConfig


def mcp_server_to_response(server: McpServer) -> McpServerResponse:
    """转换脱敏 Server 投影，不读取或恢复秘密值。"""
    config: dict[str, Any]
    if isinstance(server.config, StreamableHttpConfig):
        config = {"url": server.config.url}
    elif isinstance(server.config, StdioConfig):
        config = {
            "command": server.config.command,
            "args": list(server.config.args),
            "cwd": server.config.cwd,
        }
    else:
        raise ValueError("mcp_config_invalid")
    scope = cast(Literal["company", "department"], server.visibility_scope.value)
    return McpServerResponse(
        id=server.id,
        name=server.name,
        transport=server.transport.value,
        config=config,
        secrets=[
            {"name": entry.name, "has_value": entry.has_value} for entry in server.secret_metadata
        ],
        config_version=server.config_version,
        enabled=server.enabled,
        visibility_scope=scope,
        owner_department_id=server.owner_department_id,
        visibility_grants=grants_to_response(server.visibility_grants),
        created_by=server.created_by,
        created_at=server.created_at,
        updated_at=server.updated_at,
        tools_discovered_at=server.tools_discovered_at,
    )


def mcp_server_page_to_response(
    page: McpServerPage, *, page_number: int, page_size: int
) -> McpServerListResponse:
    """转换 Server 分页。"""
    return McpServerListResponse(
        items=[mcp_server_to_response(server) for server in page.items],
        total=page.total,
        page=page_number,
        page_size=page_size,
    )


__all__ = ["mcp_server_page_to_response", "mcp_server_to_response"]
