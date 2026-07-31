"""MCP 工具适配器 — 将 MCP 工具转换为 UnifiedToolManager 可用的格式.

职责：
- MCP 工具 Schema 转换为 ToolSchema
- MCP 工具远程代理执行
- 错误处理与超时控制
"""

from __future__ import annotations

import asyncio
from typing import Any

from athena.core.tools.base import BaseTool
from athena.core.tools.mcp.client import MCPClient
from athena.models.tool import ToolCallStatus, ToolResult, ToolSchema
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class MCPTool(BaseTool):
    """MCP 工具代理 — 通过 MCP 协议远程调用工具."""

    def __init__(
        self,
        schema: ToolSchema,
        mcp_client: MCPClient,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(schema)
        self._client = mcp_client
        self._timeout = timeout

    async def execute(self, **kwargs: Any) -> ToolResult:
        """通过 MCP 协议远程执行工具."""
        try:
            result = await asyncio.wait_for(
                self._client.call_tool(self.name, kwargs),
                timeout=self._timeout,
            )

            # 解析 MCP 返回结果
            if isinstance(result, dict):
                content = result.get("content", [])
                if isinstance(content, list):
                    # MCP 标准格式：[{type: "text", text: "..."}]
                    text_parts = [
                        item.get("text", str(item))
                        for item in content
                        if isinstance(item, dict)
                    ]
                    output = "\n".join(text_parts) if text_parts else str(result)
                else:
                    output = str(content)
            else:
                output = str(result)

            return ToolResult(
                tool_call_id="",
                name=self.name,
                status=ToolCallStatus.SUCCESS,
                output=output,
            )

        except asyncio.TimeoutError:
            return ToolResult(
                tool_call_id="",
                name=self.name,
                status=ToolCallStatus.TIMEOUT,
                error=f"工具 {self.name} 执行超时 ({self._timeout}s)",
            )
        except Exception as e:
            logger.error("mcp_tool_execute_failed", tool=self.name, error=str(e))
            return ToolResult(
                tool_call_id="",
                name=self.name,
                status=ToolCallStatus.FAILED,
                error=str(e),
            )


class MCPToolAdapter:
    """MCP 工具适配器 — 注册 MCP Server 的工具到 UnifiedToolManager.

    使用方式：
        adapter = MCPToolAdapter(tool_manager)
        await adapter.register_server(
            server_name="github",
            server_command=["npx", "@modelcontextprotocol/server-github"],
        )
    """

    def __init__(self, tool_manager: Any) -> None:
        self._tool_manager = tool_manager
        self._clients: dict[str, MCPClient] = {}

    async def register_server(
        self,
        server_name: str,
        server_command: list[str] | None = None,
        server_url: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> list[str]:
        """注册 MCP Server 的所有工具.

        Args:
            server_name: 服务器名称
            server_command: 启动命令（stdio 传输）
            server_url: 服务器 URL（SSE 传输）
            env: 环境变量
            timeout: 工具调用超时

        Returns:
            注册的工具名称列表
        """
        client = MCPClient(
            server_name=server_name,
            server_command=server_command,
            server_url=server_url,
            env=env,
            timeout=timeout,
        )

        try:
            await client.connect()
        except Exception as e:
            logger.error("mcp_server_connect_failed", server=server_name, error=str(e))
            raise

        self._clients[server_name] = client

        # 获取工具列表并注册
        tools = await client.list_tools()
        registered_names: list[str] = []

        for tool_def in tools:
            tool_name = tool_def.get("name", "")
            if not tool_name:
                continue

            # 添加服务器名称前缀避免冲突
            full_name = f"mcp_{server_name}_{tool_name}"

            schema = ToolSchema(
                name=full_name,
                description=tool_def.get("description", ""),
                parameters=tool_def.get("inputSchema", {}),
                require_approval=True,  # MCP 工具默认需要审批
                risk_level="medium",
            )

            mcp_tool = MCPTool(
                schema=schema,
                mcp_client=client,
                timeout=timeout,
            )

            self._tool_manager.register_tool(mcp_tool)
            registered_names.append(full_name)

            logger.info(
                "mcp_tool_registered",
                server=server_name,
                tool=full_name,
            )

        return registered_names

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
