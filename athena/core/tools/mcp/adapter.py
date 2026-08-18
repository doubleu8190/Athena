"""MCP 工具适配器 — 将 MCP 工具转换为 UnifiedToolManager 可用的格式.

职责：
- MCP 工具 Schema 转换为 ToolSchema
- 工具名称前缀化，避免多 Server 之间冲突
- 批量注册到 UnifiedToolManager
"""

from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

from athena.core.tools.base import MCPTool
from athena.core.tools.mcp.client import MCPClient
from athena.core.tools.catalog import ToolCatalogService
from athena.models.tool import RiskLevel
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.core.tools.manager import UnifiedToolManager

logger = get_logger(__name__)

# 本地注册名只允许 [a-zA-Z0-9_-]：服务器名/工具名可能含 @ / 等字符
# （如 "@mendableai/firecrawl-mcp-server"），会生成 bind_tools 无法接受的工具名。
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _sanitize(name: str) -> str:
    """将名称中的非法字符替换为下划线，用于本地工具名前缀."""
    return _NAME_RE.sub("_", name)


class MCPToolAdapter:
    """MCP 工具适配器 — 注册 MCP Server 的工具到 UnifiedToolManager.

    使用方式：
        adapter = MCPToolAdapter(tool_manager, catalog)
        await adapter.register_server(
            server_name="github",
            server_command=["npx", "@modelcontextprotocol/server-github"],
        )
    """

    def __init__(
        self,
        tool_manager: UnifiedToolManager,
        catalog: ToolCatalogService,
    ) -> None:
        self._tool_manager = tool_manager
        self._catalog = catalog
        self._clients: dict[str, MCPClient] = {}

    async def register_server(
        self,
        server_name: str,
        server_command: list[str] | None = None,
        server_url: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
        connect_timeout: float = 60.0,
    ) -> list[str]:
        """注册 MCP Server 的所有工具.

        注册时集成 DB：已存在的工具以 DB 治理参数为准，不存在则写入 DB。
        description / parameters 以远端最新为准（MCP Server 可能升级）。

        Args:
            server_name: 服务器名称（原始名，含 @ / 等字符也保留）
            server_command: 启动命令（stdio 传输）
            server_url: 服务器 URL（SSE 传输）
            env: 环境变量（增量注入，client 会与 os.environ 合并）
            timeout: 工具调用超时
            connect_timeout: 握手阶段超时（npx -y 冷启动可能较慢）

        Returns:
            注册的工具名称列表
        """
        client = MCPClient(
            server_name=server_name,
            server_command=server_command,
            server_url=server_url,
            env=env,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )

        try:
            await client.connect()
        except Exception as e:
            # 连接失败时 client 可能已拉起子进程但未置 _connected，显式断开防泄漏
            try:
                await client.disconnect()
            except Exception as disconnect_error:
                logger.warning(
                    "mcp_disconnect_after_connect_failed",
                    server=server_name,
                    error=str(disconnect_error),
                )
            logger.error("mcp_server_connect_failed", server=server_name, error=str(e))
            raise

        self._clients[server_name] = client

        # 获取工具列表并构建注册信息
        tools = await client.list_tools()
        mcp_tools: list[MCPTool] = []
        registered_names: list[str] = []

        for tool_def in tools:
            tool_name = tool_def.get("name", "")
            if not tool_name:
                continue

            # 本地注册名加服务器前缀避免冲突，并对非法字符清洗；
            # remote_name 保留原始工具名供远程调用（客户端用原名调用远端）
            full_name = f"mcp_{_sanitize(server_name)}_{_sanitize(tool_name)}"

            mcp_tools.append(
                MCPTool(
                    name=full_name,
                    description=tool_def.get("description", ""),
                    parameters=tool_def.get(
                        "inputSchema", {"type": "object", "properties": {}}
                    ),
                    mcp_client=client,
                    server_name=server_name,
                    remote_name=tool_def.get("remote_name"),
                    risk_level=RiskLevel.MEDIUM,
                    require_approval=True,
                )
            )
            registered_names.append(full_name)

            logger.info("mcp_tool_registered", server=server_name, tool=full_name)

        # 批量注册到工具管理器
        self._tool_manager.register_mcp_tools(mcp_tools)
        await self._catalog.reconcile(
            self._tool_manager,
            names=registered_names,
            registrations={
                item.schema.name: {
                    "server_name": item._server_name,
                    "remote_name": item._remote_name,
                }
                for item in mcp_tools
            },
        )

        return registered_names

    async def unregister_server(self, server_name: str) -> None:
        """断开指定服务器的连接并从连接池移除."""
        client = self._clients.pop(server_name, None)
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:
            logger.warning("mcp_disconnect_failed", server=server_name, error=str(e))

    async def disconnect_all(self) -> None:
        """断开所有 MCP Server 连接."""
        for name, client in self._clients.items():
            try:
                await client.disconnect()
            except Exception as e:
                logger.warning("mcp_disconnect_failed", server=name, error=str(e))
        self._clients.clear()

    def get_client(self, server_name: str) -> MCPClient | None:
        """获取指定服务器的客户端."""
        return self._clients.get(server_name)
