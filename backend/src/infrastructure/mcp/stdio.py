"""受控 stdio MCP transport（基于官方 MCP SDK stdio_client 封装）。"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, cast

from mcp.client.stdio import StdioServerParameters, stdio_client


class McpStdioConfigurationError(ValueError):
    """stdio MCP 启动配置无效。"""


class McpStdioTransport:
    """校验绝对路径与白名单环境变量后，直接委托官方 SDK stdio_client 托管子进程。"""

    def __init__(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        """校验启动参数与环境白名单。"""
        executable = Path(command)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(command, os.X_OK)
        ):
            raise McpStdioConfigurationError("mcp_stdio_command_invalid")
        if cwd is not None and not Path(cwd).is_absolute():
            raise McpStdioConfigurationError("mcp_stdio_cwd_invalid")
        # 仅将显式 base_environment 与 Server 密钥合并传给 SDK；SDK 默认只补充 6 个基础 OS 变量，
        # 绝不透传宿主进程中的任何 ATHENA_* 密钥环境变量。
        environment = dict(base_environment or {}) | dict(env or {})
        if any(
            not isinstance(value, str) or "\x00" in value
            for value in (*args, *environment.values())
        ):
            raise McpStdioConfigurationError("mcp_stdio_argument_invalid")
        if any(not key or "=" in key or "\x00" in key for key in environment):
            raise McpStdioConfigurationError("mcp_stdio_environment_invalid")
        self._command = command
        self._args = tuple(args)
        self._cwd = cwd
        self._environment = environment
        self._context: AbstractAsyncContextManager[Any] | None = None

    async def __aenter__(self) -> Any:
        """启动子进程并打开 MCP SDK 双向 JSON-RPC 消息流。"""
        self._context = self._run()
        return await self._context.__aenter__()

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """关闭消息流并由 SDK 回收子进程及进程组。"""
        if self._context is None:
            return False
        return cast(bool, await self._context.__aexit__(exception_type, exception, traceback))

    @asynccontextmanager
    async def _run(self) -> AsyncGenerator[Any, None]:
        parameters = StdioServerParameters(
            command=self._command,
            args=list(self._args),
            env=self._environment,
            cwd=self._cwd,
        )
        # 将子进程 stderr 重定向到 os.devnull，既防日志污染也兼容无真实 fileno 的捕获流。
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            async with stdio_client(parameters, errlog=devnull) as streams:
                yield streams


__all__ = [
    "McpStdioConfigurationError",
    "McpStdioTransport",
]
