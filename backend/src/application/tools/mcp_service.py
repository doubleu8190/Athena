"""MCP 服务端管理应用服务。"""

from __future__ import annotations

from domain.tools import MCPPort, MCPServerConfig


class MCPService:
    """协调 MCP 服务端注册、注销、查询和关闭。"""

    def __init__(self, port: MCPPort) -> None:
        self._port = port

    async def register(self, name: str, config: MCPServerConfig) -> dict[str, object]:
        """注册 MCP 服务端。"""
        return await self._port.register(name, config)

    async def unregister(self, name: str) -> None:
        """注销 MCP 服务端。"""
        await self._port.unregister(name)

    async def list_servers(self) -> list[dict[str, object]]:
        """列出 MCP 服务端状态。"""
        return await self._port.list_servers()

    async def shutdown(self) -> None:
        """关闭全部 MCP 连接。"""
        await self._port.shutdown()


__all__ = ["MCPService"]
