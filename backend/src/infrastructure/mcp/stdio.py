"""受控 stdio MCP transport。"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from types import TracebackType
from typing import cast

import anyio
from mcp.client._transport import TransportStreams
from mcp.shared._context_streams import (
    ContextReceiveStream,
    ContextSendStream,
    create_context_streams,
)
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

DEFAULT_STDIO_MAX_LINE_BYTES = 2 * 1024 * 1024
DEFAULT_STDIO_MAX_TOTAL_BYTES = 2 * 1024 * 1024
DEFAULT_STDIO_GRACE_SECONDS = 1.0
DEFAULT_STDIO_TERM_SECONDS = 1.0
DEFAULT_STDIO_KILL_SECONDS = 1.0
_READ_CHUNK_BYTES = 64 * 1024


class McpStdioConfigurationError(ValueError):
    """stdio MCP 启动配置无效。"""


class McpStdioCleanupError(RuntimeError):
    """stdio MCP 进程或其子进程未能在时限内确认回收。"""


class McpStdioProcessError(RuntimeError):
    """stdio MCP Server 在操作期间以非零状态退出。"""


class McpStdioTransport:
    """用隔离环境启动 MCP 子进程并通过标准输入输出传输 JSON-RPC。"""

    def __init__(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        base_environment: Mapping[str, str] | None = None,
        maximum_line_bytes: int = DEFAULT_STDIO_MAX_LINE_BYTES,
        maximum_total_bytes: int = DEFAULT_STDIO_MAX_TOTAL_BYTES,
        grace_seconds: float = DEFAULT_STDIO_GRACE_SECONDS,
        term_seconds: float = DEFAULT_STDIO_TERM_SECONDS,
        kill_seconds: float = DEFAULT_STDIO_KILL_SECONDS,
    ) -> None:
        """保存已由部署策略批准的启动参数和资源上限。"""
        executable = Path(command)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(command, os.X_OK)
        ):
            raise McpStdioConfigurationError("mcp_stdio_command_invalid")
        if cwd is not None and not Path(cwd).is_absolute():
            raise McpStdioConfigurationError("mcp_stdio_cwd_invalid")
        environment = dict(base_environment or {}) | dict(env or {})
        if any(
            not isinstance(value, str) or "\x00" in value
            for value in (*args, *environment.values())
        ):
            raise McpStdioConfigurationError("mcp_stdio_argument_invalid")
        if any(not key or "=" in key or "\x00" in key for key in environment):
            raise McpStdioConfigurationError("mcp_stdio_environment_invalid")
        if maximum_line_bytes < 1 or maximum_total_bytes < 1:
            raise ValueError("mcp_stdio_limit_invalid")
        if min(grace_seconds, term_seconds, kill_seconds) < 0:
            raise ValueError("mcp_stdio_timeout_invalid")
        self._command = command
        self._args = tuple(args)
        self._cwd = cwd
        self._environment = environment
        self._maximum_line_bytes = maximum_line_bytes
        self._maximum_total_bytes = maximum_total_bytes
        self._grace_seconds = grace_seconds
        self._term_seconds = term_seconds
        self._kill_seconds = kill_seconds
        self._context: AbstractAsyncContextManager[TransportStreams] | None = None

    async def __aenter__(self) -> TransportStreams:
        """启动进程并为 MCP Client 打开双向消息流。"""
        self._context = self._run()
        return await self._context.__aenter__()

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """关闭连接并确认进程组已回收。"""
        if self._context is None:
            return False
        return cast(bool, await self._context.__aexit__(exception_type, exception, traceback))

    @asynccontextmanager
    async def _run(self) -> AsyncGenerator[TransportStreams, None]:
        process = await anyio.open_process(
            [self._command, *self._args],
            stdin=-1,
            stdout=-1,
            stderr=subprocess.DEVNULL,
            cwd=self._cwd,
            env=self._environment,
            start_new_session=(os.name == "posix"),
        )
        server_send, server_receive = create_context_streams[SessionMessage | Exception](0)
        client_send, client_receive = create_context_streams[SessionMessage](0)
        async with AsyncExitStack() as stack:
            task_group = await stack.enter_async_context(anyio.create_task_group())
            task_group.start_soon(
                self._read_stdout,
                process,
                server_send,
            )
            task_group.start_soon(
                self._write_stdin,
                process,
                client_receive,
            )
            try:
                yield cast(TransportStreams, (server_receive, client_send))
            finally:
                try:
                    with anyio.CancelScope(shield=True):
                        await self._shutdown(process)
                finally:
                    task_group.cancel_scope.cancel()

    async def _read_stdout(
        self,
        process: anyio.abc.Process,
        output: ContextSendStream[SessionMessage | Exception],
    ) -> None:
        buffer = bytearray()
        total_bytes = 0
        try:
            assert process.stdout is not None
            async with output:
                while True:
                    chunk = await process.stdout.receive(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > self._maximum_total_bytes:
                        raise McpStdioConfigurationError("mcp_stdio_output_too_large")
                    buffer.extend(chunk)
                    if len(buffer) > self._maximum_line_bytes and b"\n" not in buffer:
                        raise McpStdioConfigurationError("mcp_stdio_line_too_large")
                    while b"\n" in buffer:
                        line, _, remainder = buffer.partition(b"\n")
                        buffer = bytearray(remainder)
                        if len(line) > self._maximum_line_bytes:
                            raise McpStdioConfigurationError("mcp_stdio_line_too_large")
                        await output.send(self._parse_message(bytes(line)))
                if buffer:
                    await output.send(self._parse_message(bytes(buffer)))
        except (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError):
            return

    async def _write_stdin(
        self,
        process: anyio.abc.Process,
        incoming: ContextReceiveStream[SessionMessage],
    ) -> None:
        try:
            assert process.stdin is not None
            async with incoming:
                async for message in incoming:
                    data = (
                        message.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n"
                    ).encode()
                    if len(data) > self._maximum_line_bytes:
                        raise McpStdioConfigurationError("mcp_stdio_request_too_large")
                    await process.stdin.send(data)
        except (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            return

    @staticmethod
    def _parse_message(line: bytes) -> SessionMessage | Exception:
        try:
            message = jsonrpc_message_adapter.validate_json(line, by_name=False)
        except ValueError as error:
            return error
        return SessionMessage(message)

    async def _shutdown(self, process: anyio.abc.Process) -> None:
        controlled_process_signals: set[int] = set()
        if process.stdin is not None:
            with suppress(OSError, anyio.ClosedResourceError):
                await process.stdin.aclose()
        if process.returncode is None:
            with anyio.move_on_after(self._grace_seconds) as grace_scope:
                await process.wait()
            if grace_scope.cancelled_caught:
                # Attribute negative exit codes only to a signal sent while the
                # main process was still alive. Later group cleanup can target
                # descendants after the main process has already exited.
                if process.returncode is None and self._signal_process_group(
                    process, signal.SIGTERM
                ):
                    controlled_process_signals.add(signal.SIGTERM)
                with anyio.move_on_after(self._term_seconds) as term_scope:
                    await process.wait()
                if term_scope.cancelled_caught:
                    if process.returncode is None and self._signal_process_group(
                        process, signal.SIGKILL
                    ):
                        controlled_process_signals.add(signal.SIGKILL)
                    with anyio.move_on_after(self._kill_seconds) as kill_scope:
                        await process.wait()
                    if kill_scope.cancelled_caught or process.returncode is None:
                        raise McpStdioCleanupError("mcp_stdio_cleanup_failed")
        if os.name == "posix" and self._process_group_exists(process.pid):
            await self._cleanup_process_group(process)
        if process.returncode is None:
            raise McpStdioCleanupError("mcp_stdio_cleanup_failed")
        if process.returncode > 0 or (
            process.returncode < 0 and -process.returncode not in controlled_process_signals
        ):
            raise McpStdioProcessError("mcp_stdio_process_failed")

    async def _cleanup_process_group(self, process: anyio.abc.Process) -> None:
        group_cleanup_signals: set[int] = set()
        if self._signal_process_group(process, signal.SIGTERM):
            group_cleanup_signals.add(signal.SIGTERM)
        if not await self._wait_for_process_group_exit(process.pid, self._term_seconds):
            if self._signal_process_group(process, signal.SIGKILL):
                group_cleanup_signals.add(signal.SIGKILL)
            if not await self._wait_for_process_group_exit(process.pid, self._kill_seconds):
                raise McpStdioCleanupError("mcp_stdio_cleanup_failed")
        if not group_cleanup_signals and self._process_group_exists(process.pid):
            raise McpStdioCleanupError("mcp_stdio_cleanup_failed")

    async def _wait_for_process_group_exit(self, process_id: int, timeout: float) -> bool:
        deadline = anyio.current_time() + timeout
        while self._process_group_exists(process_id):
            if anyio.current_time() >= deadline:
                return False
            await anyio.sleep(0.01)
        return True

    @staticmethod
    def _process_group_exists(process_id: int) -> bool:
        try:
            os.killpg(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _signal_process_group(process: anyio.abc.Process, signal_number: int) -> bool:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal_number)
            elif signal_number == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            return False
        return True


__all__ = [
    "DEFAULT_STDIO_MAX_LINE_BYTES",
    "DEFAULT_STDIO_MAX_TOTAL_BYTES",
    "McpStdioCleanupError",
    "McpStdioConfigurationError",
    "McpStdioProcessError",
    "McpStdioTransport",
]
