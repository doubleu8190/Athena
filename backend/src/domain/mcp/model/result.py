"""MCP Server 目录查询结果。"""

from __future__ import annotations

from dataclasses import dataclass

from domain.mcp.model.entity import McpServer


@dataclass(frozen=True, slots=True)
class McpServerPage:
    """按更新时间倒序返回的 Server 分页。"""

    items: tuple[McpServer, ...]
    total: int


__all__ = ["McpServerPage"]
