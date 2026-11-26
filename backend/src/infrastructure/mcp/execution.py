"""单次调用 MCP 执行客户端（共享标准 httpx2 连接池，独立无 Cookie 客户端隔离凭据）。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast

import httpx2
from mcp import Client, MCPError, types
from mcp.client import Transport
from mcp.client.streamable_http import streamable_http_client

from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolTarget,
    McpToolExecutor,
    StreamableHttpConfig,
    mcp_content_projection,
    mcp_message_from_result,
)
from infrastructure.mcp.http import DEFAULT_MAX_RESPONSE_BYTES, build_mcp_http_client
from infrastructure.mcp.transport import build_controlled_mcp_transport

logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_TIMEOUT_SECONDS = 10.0
MAX_LIST_PAGES = 100


class _BorrowedSharedTransport(httpx2.AsyncBaseTransport):
    """向单次调用 AsyncClient 借出共享底层连接池；关闭客户端时不关闭共享连接池。"""

    def __init__(self, shared: httpx2.AsyncBaseTransport) -> None:
        self._shared = shared

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._shared.handle_async_request(request)

    async def aclose(self) -> None:
        # 共享连接池生命周期由 McpToolCallClient.aclose() 统一管理。
        return None


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
    """直接调用 ClientSession.call_tool 确保单次发送不重放，并通过共享连接池复用 HTTP 连接。"""

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
        # allowed_private_hosts 保留兼容参数；超管受控内网 MCP 默认放行内网并仅拦截云元数据 IP。
        del allowed_private_hosts
        base_environment = dict(stdio_base_environment or {})
        if (
            not set(base_environment) <= stdio_allowed_environment_names
            or not 0 < cleanup_timeout_seconds <= 10
            or not 1 <= maximum_response_bytes <= DEFAULT_MAX_RESPONSE_BYTES
        ):
            raise ValueError("mcp_execution_policy_invalid")
        self._stdio_base_environment = base_environment
        self._stdio_allowed_environment_names = stdio_allowed_environment_names
        self._client_factory = client_factory or self._default_client
        self._transport_factory = transport_factory
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes
        # 共享单个标准 AsyncHTTPTransport，原生按 (scheme, host, port) 复用连接并安全校验真实 SNI。
        self._shared_http_transport: httpx2.AsyncBaseTransport | None = None

    async def call(
        self,
        target: McpRuntimeToolTarget,
        arguments: dict[str, object],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        """单次建连、通过公开 list_tools 准备元数据后发送唯一一次 tools/call。"""
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
                    transport = self._make_transport(target)
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
                            target,
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
                # 发出 tools/call 后协议错无法证明远端无副作用，按 UNKNOWN 收敛。
                failure = (
                    McpCallStatus.UNKNOWN if call_started else McpCallStatus.ERROR,
                    "mcp_tool_protocol_unknown" if call_started else "mcp_tool_error",
                    "MCP 请求可能已经发出，但协议错误使结果无法确认。"
                    if call_started
                    else "MCP Server 返回了明确错误。",
                )
            except Exception:
                # 覆盖网络、校验（含 Pydantic ValidationError）与其他运行期异常。
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
            if cancelled and entered and client is not None:
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

    async def aclose(self) -> None:
        """释放共享 HTTP 底层连接池，可安全重复调用。"""
        transport = self._shared_http_transport
        self._shared_http_transport = None
        if transport is not None:
            await transport.aclose()

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
        target: McpRuntimeToolTarget,
        deadline: float,
    ) -> None:
        """通过 MCP SDK 公开的 list_tools 分页校验目标工具并准备协议层元数据与 schema。"""
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
            matched_tool = next(
                (tool for tool in tools if getattr(tool, "name", None) == target.remote_name),
                None,
            )
            if matched_tool is not None:
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

    def _build_shared_http_transport(self) -> httpx2.AsyncBaseTransport:
        """构造共享标准 AsyncHTTPTransport（可由测试替换注入内存对端）。"""
        return httpx2.AsyncHTTPTransport(
            trust_env=False,
            retries=0,
            limits=httpx2.Limits(
                max_connections=32,
                max_keepalive_connections=16,
                keepalive_expiry=30.0,
            ),
        )

    def _ensure_shared_http_transport(self) -> httpx2.AsyncBaseTransport:
        if self._shared_http_transport is None:
            self._shared_http_transport = self._build_shared_http_transport()
        return self._shared_http_transport

    def _make_transport(self, target: McpRuntimeToolTarget) -> Transport:
        # 默认 HTTP 路径借用共享标准连接池；自定义工厂与 Stdio 走独立传输构造。
        if self._transport_factory is None and isinstance(
            target.connection.config, StreamableHttpConfig
        ):
            return cast(Transport, self._shared_streamable_http_transport(target))
        return build_controlled_mcp_transport(
            target.connection,
            maximum_response_bytes=self._maximum_response_bytes,
            stdio_base_environment=self._stdio_base_environment,
            transport_factory=self._transport_factory,
        )

    @asynccontextmanager
    async def _shared_streamable_http_transport(
        self,
        target: McpRuntimeToolTarget,
    ) -> AsyncGenerator[Any, None]:
        """复用共享底层连接池，但每次调用新建无 Cookie 的独立 AsyncClient 隔离凭据。"""
        config = target.connection.config
        if not isinstance(config, StreamableHttpConfig):
            raise ValueError("mcp_transport_unsupported")
        shared = self._ensure_shared_http_transport()
        # 安全边界：严禁跨调用复用 AsyncClient，避免 Set-Cookie 或请求头在调用间串读。
        async with build_mcp_http_client(
            config.url,
            headers=dict(target.connection.secrets),
            transport=_BorrowedSharedTransport(shared),
        ) as client:
            async with streamable_http_client(
                config.url,
                http_client=client,
                max_sse_event_size=self._maximum_response_bytes,
            ) as streams:
                yield streams

    async def _close_client(self, client: _CallClient) -> str | None:
        """在进入任务上下文内有界关闭 MCP Client，满足 anyio CancelScope 约束。"""
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
