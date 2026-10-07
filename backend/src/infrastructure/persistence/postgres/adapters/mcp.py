"""PostgreSQL-backed MCP configuration adapter."""

from __future__ import annotations

from datetime import datetime, timezone

from domain.tools import MCPPort, MCPServer, MCPServerConfig
from ..repositories.mcp_repository import PostgresMCPServerRepository


class PostgresMCPAdapter(MCPPort):
    """Persist MCP registrations through the PostgreSQL repository."""

    def __init__(self, repository: PostgresMCPServerRepository) -> None:
        """保存 MCP 配置 repository。"""
        self._repository = repository

    async def register(self, name: str, config: MCPServerConfig) -> dict[str, object]:
        """写入 MCP 配置并返回稳定的注册摘要。"""
        await self._repository.upsert(MCPServer(name, config, datetime.now(timezone.utc)))
        return {"name": name, "status": "registered", "enabled": config.enabled}

    async def unregister(self, name: str) -> None:
        """软删除指定 MCP 配置。"""
        await self._repository.delete(name)

    async def list_servers(self) -> list[dict[str, object]]:
        """返回当前未删除 MCP 配置的摘要列表。"""
        values = await self._repository.list_all()
        return [
            {"name": item.name, "status": "registered", "enabled": item.config.enabled}
            for item in values
        ]

    async def shutdown(self) -> None:
        """关闭 MCP 运行资源；配置 adapter 本身没有进程资源。"""
        return None


__all__ = ["PostgresMCPAdapter"]
