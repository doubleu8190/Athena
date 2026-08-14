"""MCP 客户端 — 基于官方 mcp SDK 管理与 MCP Server 的连接和通信.

MCP (Model Context Protocol) 是 Anthropic 提出的工具协议标准，
允许 AI Agent 通过标准化接口调用外部工具和服务。

使用官方 `mcp` SDK 的 ClientSession 替代手写 JSON-RPC 实现，自动获得：
- 协议版本协商：initialize 发送最新协议版本（2025-11-25），并按服务端返回值
  校验是否在 SDK 支持范围内（2024-11-05 ~ 2025-11-25 全兼容）
- 握手后自动发送 notifications/initialized（部分严格服务端依赖此通知才处理请求）
- 三种传输：stdio（本地进程）/ Streamable HTTP / SSE（远程服务）
- 递增 JSON-RPC id、会话生命周期与子进程优雅关闭（stdin 关闭 → SIGTERM → SIGKILL）

会话生命周期：SDK 的传输与会话上下文（anyio task group）必须在同一任务内
enter/exit。而 connect/call_tool/disconnect 可能来自不同的 HTTP 请求任务，
因此由一个后台任务（`_session_worker`）独占持有整个会话上下文，其它方法通过
memory stream / Event 与之协作。

对外接口与旧实现保持一致，adapter / manager / 路由层无需改动。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any, AsyncContextManager

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from athena.utils.logging import get_logger

logger = get_logger(__name__)


class MCPClient:
    """MCP 客户端 — 基于官方 SDK 管理 MCP Server 连接.

    支持：
    - stdio 传输（本地进程，server_command）
    - Streamable HTTP 传输（远程服务，server_url 为 http/https）
    - SSE 传输（远程服务，server_url 为其它协议）

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
        connect_timeout: float = 60.0,
    ) -> None:
        self._server_name = server_name
        self._server_command = server_command
        self._server_url = server_url
        self._env = env
        # 工具调用超时（tools/call 单次请求）
        self._timeout = timeout
        # 握手阶段（initialize + tools/list）超时，独立于后续 tools/call 的 _timeout：
        # npx -y 冷启动（下载包 + 启动进程）可能远超单请求 30s
        self._connect_timeout = connect_timeout
        # 会话生命周期由后台任务独占持有（见模块 docstring 的说明）
        self._session_task: asyncio.Task[None] | None = None
        self._session: ClientSession | None = None
        self._ready: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None
        self._connect_error: BaseException | None = None
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

        await self._spawn_session()

        self._connected = True
        logger.info("mcp_connected", server=self._server_name)

    async def _spawn_session(self) -> None:
        """构建传输并启动后台会话任务，等待握手完成（成功或失败）."""
        if self._server_command:
            # env 由 SDK 增量合并：{**get_default_environment(), **server.env}，
            # 默认环境含 PATH，保证 npx 等依赖 PATH 的命令可正常启动。
            params = StdioServerParameters(
                command=self._server_command[0],
                args=self._server_command[1:],
                env=self._env,
            )
            transport: AsyncContextManager[Any] = stdio_client(params)
        elif self._server_url:
            url = self._server_url
            if url.startswith(("http://", "https://")):
                # Streamable HTTP：现代远程服务标准
                transport = streamable_http_client(url)
            else:
                transport = sse_client(url)
        else:
            raise ValueError("Must specify either server_command or server_url")

        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._connect_error = None
        self._session_task = asyncio.create_task(self._session_worker(transport))

        try:
            await self._ready.wait()
        except BaseException:
            # connect 被取消 / 异常：终止后台任务，防止子进程 / 连接泄漏
            self._session_task.cancel()
            await asyncio.gather(self._session_task, return_exceptions=True)
            self._session_task = None
            raise

        if self._connect_error is not None:
            error = self._connect_error
            self._connect_error = None
            raise error

    async def _session_worker(self, transport: AsyncContextManager[Any]) -> None:
        """在单一任务内持有传输与会话上下文，完成握手后等待停止信号.

        - 成功：置 _session/_tools，通知 _ready，等待 _stop 后关闭上下文
        - 失败：记录 _connect_error 通知 connect，随后关闭上下文
        无论成败，enter 与 exit 都在本任务内完成（anyio cancel scope 约束）。
        """
        stack = contextlib.AsyncExitStack()
        # 只在 _spawn_session 创建 _ready/_stop 之后才可能启动本任务，故必非 None
        ready = self._ready
        stop = self._stop
        if ready is None or stop is None:
            raise RuntimeError("session worker started without _ready/_stop")
        try:
            read, write = await stack.enter_async_context(transport)
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    # 握手阶段（initialize + 版本协商 + initialized 通知 + tools/list）
                    # 用宽松的 connect_timeout；tools/call 在 call_tool 中单独收紧。
                    read_timeout_seconds=timedelta(seconds=self._connect_timeout),
                )
            )
            # initialize 由 SDK 完成：自动版本协商 + 发送 notifications/initialized
            await session.initialize()
            tools = await session.list_tools()
        except BaseException as e:
            self._connect_error = e
        else:
            self._session = session
            self._tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                }
                for t in tools.tools
            ]
        finally:
            ready.set()
            if self._connect_error is None:
                # 成功：会话保持活跃，直到 disconnect 置 _stop
                try:
                    await stop.wait()
                except asyncio.CancelledError:
                    pass  # 外部取消（如应用退出）：同样进入关闭流程
            # 在同一任务内关闭（transport 的 task group 在此退出）
            try:
                await stack.aclose()
            except BaseException as e:
                # 关闭会话/传输时 SDK 内部 task group 取消可能抛 CancelledError；
                # 清理异常不得逃逸出 worker 任务，否则 disconnect 的 await task 会重抛。
                logger.debug(
                    "mcp_session_close_failed",
                    server=self._server_name,
                    error=str(e),
                )
            self._session = None
            self._session_task = None

    async def disconnect(self) -> None:
        """断开连接并释放子进程 / 网络资源."""
        self._stop.set()
        task = self._session_task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                # worker 被外部取消（如事件循环关闭）：断开尽力而为
                logger.debug(
                    "mcp_disconnect_worker_cancelled", server=self._server_name
                )
            except Exception as e:
                logger.warning(
                    "mcp_disconnect_failed", server=self._server_name, error=str(e)
                )
        self._session_task = None
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
            tools/call 结果（普通 dict，含 content / isError 等字段）
        """
        if not self._connected:
            await self.connect()

        session = self._session
        if session is None:
            raise RuntimeError("MCP Server not connected")

        try:
            result = await session.call_tool(
                tool_name,
                arguments,
                # 单次调用用收紧的 _timeout（优先于会话默认的 connect_timeout）
                read_timeout_seconds=timedelta(seconds=self._timeout),
            )
        except Exception as e:
            logger.error("mcp_tool_call_failed", tool=tool_name, error=str(e))
            raise

        # 转为普通 dict 返回，与旧实现（原始 JSON-RPC result）形状一致：
        # base.py 的 dict 分支可干净提取 content 文本；model_dump 使 content
        # 块变为 dict 而非 pydantic 模型，避免 extract_message_text 退化 repr。
        return result.model_dump(mode="python")
