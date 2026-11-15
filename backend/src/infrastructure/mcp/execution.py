"""Single-call MCP client with mandatory connection teardown."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from mcp import Client, MCPError, types
from mcp.client._transport import Transport

from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolTarget,
    McpToolExecutor,
    mcp_content_projection,
    mcp_message_from_result,
)
from infrastructure.mcp.http import DEFAULT_MAX_RESPONSE_BYTES
from infrastructure.mcp.transport import build_controlled_mcp_transport

logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_TIMEOUT_SECONDS = 10.0
MAX_LIST_PAGES = 100


class _ClientSession(Protocol):
    async def list_tools(self, *, params: Any = None) -> Any: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: float | None = None,
        progress_callback: Any = None,
        *,
        input_responses: Any = None,
        request_state: str | None = None,
        meta: Any = None,
        allow_input_required: bool = False,
        allow_claimed: bool = False,
    ) -> Any: ...


class _CallClient(Protocol):
    @property
    def session(self) -> _ClientSession: ...

    @property
    def server_capabilities(self) -> Any: ...

    async def __aenter__(self) -> _CallClient: ...

    async def __aexit__(self, *args: object) -> bool | None: ...


ClientFactory = Callable[[Transport, float], _CallClient]
TransportFactory = Callable[[McpConnectionInput], Transport]


class McpToolCallClient(McpToolExecutor):
    """Use ``ClientSession.call_tool`` directly so no SDK layer resends a call."""

    def __init__(
        self,
        *,
        allowed_private_hosts: frozenset[str] = frozenset(),
        stdio_base_environment: Mapping[str, str] | None = None,
        stdio_allowed_environment_names: frozenset[str] = frozenset(),
        client_factory: ClientFactory | None = None,
        transport_factory: TransportFactory | None = None,
        cleanup_timeout_seconds: float = DEFAULT_CLEANUP_TIMEOUT_SECONDS,
        maximum_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        base_environment = dict(stdio_base_environment or {})
        if (
            not set(base_environment) <= stdio_allowed_environment_names
            or not 0 < cleanup_timeout_seconds <= 10
            or not 1 <= maximum_response_bytes <= DEFAULT_MAX_RESPONSE_BYTES
        ):
            raise ValueError("mcp_execution_policy_invalid")
        self._allowed_private_hosts = allowed_private_hosts
        self._stdio_base_environment = base_environment
        self._stdio_allowed_environment_names = stdio_allowed_environment_names
        self._client_factory = client_factory or self._default_client
        self._transport_factory = transport_factory
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes

    async def call(
        self,
        target: McpRuntimeToolTarget,
        arguments: dict[str, object],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        """Connect once, list header state, send one tools/call, then close."""
        if deadline_seconds <= 0:
            return McpCallResult(McpCallStatus.ERROR, "mcp_deadline_exhausted")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_seconds
        client: _CallClient | None = None
        entered = False
        call_started = False
        result: Any = None
        failure: tuple[McpCallStatus, str, str] | None = None
        cancelled = False
        try:
            try:
                async with asyncio.timeout(deadline_seconds):
                    transport = self._make_transport(target.connection)
                    client = self._client_factory(transport, deadline_seconds)
                    await client.__aenter__()
                    entered = True
                    if getattr(client.server_capabilities, "tools", None) is None:
                        failure = (
                            McpCallStatus.ERROR,
                            "mcp_tools_capability_missing",
                            "",
                        )
                    else:
                        await self._prime_tool_listing(
                            client.session,
                            target.remote_name,
                            deadline,
                        )
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise TimeoutError
                        call_started = True
                        result = await client.session.call_tool(
                            target.remote_name,
                            dict(arguments),
                            read_timeout_seconds=remaining,
                            allow_input_required=True,
                            allow_claimed=False,
                        )
            except asyncio.CancelledError:
                cancelled = True
                raise
            except TimeoutError:
                failure = (
                    (
                        McpCallStatus.UNKNOWN,
                        "mcp_tool_timeout",
                        "MCP 工具在截止时间前没有返回确定结果。",
                    )
                    if call_started
                    else (
                        McpCallStatus.ERROR,
                        "mcp_tool_timeout",
                        "MCP 工具连接在截止时间前未建立。",
                    )
                )
            except MCPError:
                # A protocol error after sending tools/call cannot prove that the
                # remote side performed no business effect. Only connect/list
                # failures before admission are definite errors.
                failure = (
                    McpCallStatus.UNKNOWN if call_started else McpCallStatus.ERROR,
                    "mcp_tool_protocol_unknown" if call_started else "mcp_tool_error",
                    "MCP 请求可能已经发出，但协议错误使结果无法确认。"
                    if call_started
                    else "MCP Server 返回了明确错误。",
                )
            except Exception:
                failure = (
                    (
                        McpCallStatus.UNKNOWN,
                        "mcp_response_unknown",
                        "MCP 请求可能已经发出，但结果无法确认。",
                    )
                    if call_started
                    else (
                        McpCallStatus.ERROR,
                        "mcp_connection_failed",
                        "MCP 连接或工具目录读取失败。",
                    )
                )
        finally:
            if cancelled:
                if entered and client is not None:
                    await self._close_client(client)
        cleanup_error = None if client is None or not entered else await self._close_client(client)
        if isinstance(result, types.InputRequiredResult):
            result = None
            failure = (
                McpCallStatus.UNKNOWN if call_started else McpCallStatus.ERROR,
                "mcp_input_required_unsupported",
                "MCP 工具要求额外交互，首期无法确认最终执行结果。",
            )
        elif result is not None and not isinstance(result, types.CallToolResult):
            result = None
            failure = (
                McpCallStatus.UNKNOWN,
                "mcp_response_unknown",
                "MCP 工具返回了首期不支持的结果，无法确认执行状态。",
            )
        if result is not None:
            return self._project_result(result, cleanup_error)
        status, code, message = failure or (
            McpCallStatus.UNKNOWN,
            "mcp_response_unknown",
            "MCP 工具没有返回可用结果。",
        )
        return McpCallResult(
            status,
            code,
            message,
            cleanup_error_code=cleanup_error,
        )

    def _default_client(self, transport: Transport, deadline_seconds: float) -> _CallClient:
        return cast(
            _CallClient,
            Client(
                transport,
                mode="auto",
                read_timeout_seconds=deadline_seconds,
                cache=None,
            ),
        )

    async def _prime_tool_listing(
        self,
        session: _ClientSession,
        remote_name: str,
        deadline: float,
    ) -> None:
        """Populate SDK header/output-schema state while locating the target tool."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_LIST_PAGES):
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
            tools = getattr(page, "tools", None)
            next_cursor = getattr(page, "next_cursor", None)
            if not isinstance(tools, list) or (
                next_cursor is not None and not isinstance(next_cursor, str)
            ):
                raise ValueError("mcp_tool_listing_invalid")
            if any(getattr(tool, "name", None) == remote_name for tool in tools):
                return
            if next_cursor is None:
                raise ValueError("mcp_tool_not_listed")
            if not next_cursor or next_cursor in seen_cursors:
                raise ValueError("mcp_tool_listing_invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise ValueError("mcp_tool_listing_page_limit")

    def _project_result(self, result: Any, cleanup_error: str | None) -> McpCallResult:
        message = mcp_message_from_result(result)
        text, structured, unsupported = mcp_content_projection(result)
        if bool(getattr(result, "is_error", False)):
            return McpCallResult(
                McpCallStatus.ERROR,
                "mcp_tool_error",
                message or "MCP 工具返回了业务错误。",
                text=text,
                structured=structured,
                unsupported_content=unsupported,
                cleanup_error_code=cleanup_error,
            )
        return McpCallResult(
            McpCallStatus.OK,
            "mcp_tool_completed",
            message,
            text=text,
            structured=structured,
            unsupported_content=unsupported,
            cleanup_error_code=cleanup_error,
        )

    def _make_transport(self, connection: McpConnectionInput) -> Transport:
        return build_controlled_mcp_transport(
            connection,
            allowed_private_hosts=self._allowed_private_hosts,
            maximum_response_bytes=self._maximum_response_bytes,
            stdio_base_environment=self._stdio_base_environment,
            transport_factory=self._transport_factory,
        )

    async def _close_client(self, client: _CallClient) -> str | None:
        """Bound cleanup in the entering task, as required by anyio cancel scopes."""
        try:
            async with asyncio.timeout(self._cleanup_timeout_seconds):
                await client.__aexit__(None, None, None)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("mcp_client_cleanup_failed code=mcp_cleanup_failed")
            return "mcp_cleanup_failed"
        except Exception:
            logger.warning("mcp_client_cleanup_failed code=mcp_cleanup_failed")
            return "mcp_cleanup_failed"
        return None


__all__ = [
    "DEFAULT_CLEANUP_TIMEOUT_SECONDS",
    "McpToolCallClient",
]
