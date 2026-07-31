"""MCP 客户端 — 管理与 MCP Server 的连接和通信.

MCP (Model Context Protocol) 是 Anthropic 提出的工具协议标准，
允许 AI Agent 通过标准化接口调用外部工具和服务。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from athena.utils.logging import get_logger

logger = get_logger(__name__)


class MCPClient:
    """MCP 客户端 — 管理与 MCP Server 的连接.

    支持：
    - stdio 传输（本地进程）
    - SSE 传输（远程服务）

    使用方式：
        client = MCPClient(server_command=["python", "my_mcp_server.py"])
        await client.connect()
        tools = await client.list_tools()
        result = await client.call_tool("tool_name", {"arg": "value"})
        await client.disconnect()
    """

    def __init__(
        self,
        server_name: str = "default",
        server_command: list[str] | None = None,
        server_url: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._server_name = server_name
        self._server_command = server_command
        self._server_url = server_url
        self._env = env
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._connected = False
        self._tools: list[dict[str, Any]] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def server_name(self) -> str:
        return self._server_name

    async def connect(self) -> None:
        """连接到 MCP Server."""
        if self._connected:
            return

        if self._server_command:
            await self._connect_stdio()
        elif self._server_url:
            await self._connect_sse()
        else:
            raise ValueError("Must specify either server_command or server_url")

        self._connected = True
        logger.info("mcp_connected", server=self._server_name)

    async def _connect_stdio(self) -> None:
        """通过 stdio 连接本地 MCP Server."""
        if not self._server_command:
            raise ValueError("server_command is required for stdio transport")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._server_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
            # 发送初始化请求
            await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "athena", "version": "0.1.0"},
            })
            # 获取工具列表
            self._tools = await self._list_tools_internal()
        except Exception as e:
            logger.error("mcp_stdio_connect_failed", error=str(e))
            raise

    async def _connect_sse(self) -> None:
        """通过 SSE 连接远程 MCP Server."""
        # TODO: 实现 SSE 传输
        raise NotImplementedError("SSE transport not yet implemented")

    async def disconnect(self) -> None:
        """断开连接."""
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                self._process.kill()
            self._process = None
        self._connected = False
        self._tools = []
        logger.info("mcp_disconnected", server=self._server_name)

    async def list_tools(self) -> list[dict[str, Any]]:
        """获取可用工具列表."""
        if not self._connected:
            await self.connect()
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """调用远程工具.

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            工具执行结果
        """
        if not self._connected:
            await self.connect()

        try:
            result = await self._send_request("tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })
            return result
        except Exception as e:
            logger.error("mcp_tool_call_failed", tool=tool_name, error=str(e))
            raise

    async def _send_request(self, method: str, params: dict[str, Any]) -> Any:
        """发送 JSON-RPC 请求到 MCP Server."""
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError("MCP Server not connected")

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }

        # 发送请求
        request_json = json.dumps(request) + "\n"
        self._process.stdin.write(request_json.encode())
        await self._process.stdin.drain()

        # 读取响应（带超时）
        try:
            response_line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"MCP request timed out after {self._timeout}s")

        if not response_line:
            raise ConnectionError("MCP Server closed connection")

        response = json.loads(response_line.decode())
        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"MCP error: {error.get('message', error)}")

        return response.get("result")

    async def _list_tools_internal(self) -> list[dict[str, Any]]:
        """内部方法：获取工具列表."""
        try:
            result = await self._send_request("tools/list", {})
            tools = result.get("tools", [])
            return [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {}),
                }
                for t in tools
            ]
        except Exception as e:
            logger.warning("mcp_list_tools_failed", error=str(e))
            return []
