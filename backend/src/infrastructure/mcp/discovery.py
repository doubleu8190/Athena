"""基于锁定 MCP SDK 的受控工具发现客户端。"""

from __future__ import annotations

import json
import logging
import math
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

import anyio
from mcp import Client, types
from mcp.client import Transport
from mcp.shared.inbound import find_invalid_x_mcp_header
from mcp.types import Tool
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from common.json_schema import check_json_schema, ensure_object_root_schema
from domain.mcp import (
    DiscoveredMcpTool,
    McpConnectionInput,
    McpDiscovery,
    McpDiscoveryCapabilityError,
    McpDiscoveryError,
    McpDiscoveryProtocolError,
    McpDiscoveryResultError,
    McpDiscoveryTimeoutError,
    StreamableHttpConfig,
)
from infrastructure.mcp.http import DEFAULT_MAX_RESPONSE_BYTES
from infrastructure.mcp.transport import build_controlled_mcp_transport

logger = logging.getLogger(__name__)

DISCOVERY_DEADLINE_SECONDS = 30.0
MAX_DISCOVERY_PAGES = 100
MAX_DISCOVERED_TOOLS = 200
MAX_DISCOVERY_RESPONSE_BYTES = 2 * 1024 * 1024
_SAFE_INTEGER = 2**53 - 1
_TOOL_NAME_MAX_LOG_CHARS = 96


class _DiscoveryClient(Protocol):
    """发现流程所需的窄 MCP Client 能力。"""

    @property
    def server_capabilities(self) -> Any: ...

    @property
    def protocol_version(self) -> str: ...

    @property
    def session(self) -> _DiscoverySession: ...

    async def __aenter__(self) -> _DiscoveryClient: ...

    async def __aexit__(self, *args: object) -> bool | None: ...


class _DiscoverySession(Protocol):
    """绕过 SDK 工具吸收/过滤逻辑的原始请求能力。"""

    async def send_request(self, request: Any, result_type: Any) -> Any: ...


ClientFactory = Callable[[Transport], _DiscoveryClient]
TransportFactory = Callable[[McpConnectionInput], Transport]


class _InvalidMcpHeaderAnnotation(ValueError):
    """仅用于筛除带有无效 x-mcp-header 的单个工具。"""

    def __init__(self) -> None:
        super().__init__("mcp_discovery_header_annotation_invalid")


class McpDiscoveryClient(McpDiscovery):
    """用 SDK auto 协商读取所有工具页，并确保受控传输已清理。"""

    def __init__(
        self,
        *,
        stdio_base_environment: Mapping[str, str] | None = None,
        stdio_allowed_environment_names: frozenset[str] = frozenset(),
        client_factory: ClientFactory | None = None,
        transport_factory: TransportFactory | None = None,
        deadline_seconds: float = DISCOVERY_DEADLINE_SECONDS,
        maximum_response_bytes: int = MAX_DISCOVERY_RESPONSE_BYTES,
    ) -> None:
        """保存部署环境策略与可替换的测试依赖。

        参数：stdio_base_environment 是部署提供的最小环境映射；
        stdio_allowed_environment_names 是允许出现的环境变量名；client_factory 与
        transport_factory 仅供测试注入；deadline_seconds 和 maximum_response_bytes 是单次上限。
        返回：可复用发现服务。
        异常：环境映射越过变量名白名单或资源上限不合法时抛 ValueError。
        """
        base_environment = dict(stdio_base_environment or {})
        if (
            not set(base_environment) <= stdio_allowed_environment_names
            or deadline_seconds <= 0
            or maximum_response_bytes < 1
            or maximum_response_bytes > DEFAULT_MAX_RESPONSE_BYTES
        ):
            raise ValueError("mcp_discovery_policy_invalid")
        self._stdio_base_environment = base_environment
        self._stdio_allowed_environment_names = stdio_allowed_environment_names
        self._client_factory = client_factory or (
            lambda transport: cast(_DiscoveryClient, Client(transport, mode="auto"))
        )
        self._transport_factory = transport_factory
        self._deadline_seconds = deadline_seconds
        self._maximum_response_bytes = maximum_response_bytes

    async def discover(self, connection: McpConnectionInput) -> tuple[DiscoveredMcpTool, ...]:
        """在单一 deadline 内协商并完整读取工具目录。

        参数：connection 是不含数据库身份的配置和本 Server 秘密。
        返回：经过结构验证并按协议注解筛选后的远端工具快照。
        异常：超时、协议、能力或结果错误使用固定安全领域异常；清理失败不返回结果。
        """
        try:
            with anyio.fail_after(self._deadline_seconds):
                transport = self._make_transport(connection)
                client = self._client_factory(transport)
                async with client:
                    capabilities = client.server_capabilities
                    if getattr(capabilities, "tools", None) is None:
                        raise McpDiscoveryCapabilityError("mcp_tools_capability_missing")
                    enforce_header_annotations = (
                        isinstance(connection.config, StreamableHttpConfig)
                        and client.protocol_version in MODERN_PROTOCOL_VERSIONS
                    )
                    return await self._read_all_tools(
                        client,
                        connection,
                        enforce_header_annotations=enforce_header_annotations,
                    )
        except anyio.get_cancelled_exc_class():
            raise
        except TimeoutError:
            raise McpDiscoveryTimeoutError("mcp_discovery_timeout") from None
        except McpDiscoveryError:
            raise
        except Exception:
            raise McpDiscoveryProtocolError("mcp_discovery_protocol_failed") from None

    async def _read_all_tools(
        self,
        client: _DiscoveryClient,
        connection: McpConnectionInput,
        *,
        enforce_header_annotations: bool,
    ) -> tuple[DiscoveredMcpTool, ...]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        discovered: list[DiscoveredMcpTool] = []
        total_tool_count = 0
        for _ in range(MAX_DISCOVERY_PAGES):
            request = types.ListToolsRequest(params=types.PaginatedRequestParams(cursor=cursor))
            result = await client.session.send_request(request, types.ListToolsResult)
            page_tools = getattr(result, "tools", None)
            next_cursor = getattr(result, "next_cursor", None)
            if not isinstance(page_tools, list) or (
                next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor)
            ):
                raise McpDiscoveryResultError("mcp_discovery_result_invalid")
            total_tool_count += len(page_tools)
            if total_tool_count > MAX_DISCOVERED_TOOLS:
                raise McpDiscoveryResultError("mcp_discovery_tool_limit")
            for tool in page_tools:
                try:
                    definition = self._convert_tool(
                        tool,
                        connection,
                        enforce_header_annotations=enforce_header_annotations,
                    )
                except _InvalidMcpHeaderAnnotation as error:
                    name = _safe_log_name(getattr(tool, "name", "invalid"))
                    logger.warning("MCP 工具被安全筛除 name=%s reason=%s", name, str(error))
                    continue
                discovered.append(definition)
            if next_cursor is None:
                return tuple(discovered)
            if next_cursor in seen_cursors:
                raise McpDiscoveryResultError("mcp_discovery_cursor_loop")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise McpDiscoveryResultError("mcp_discovery_page_limit")

    def _convert_tool(
        self,
        tool: Tool,
        connection: McpConnectionInput,
        *,
        enforce_header_annotations: bool,
    ) -> DiscoveredMcpTool:
        """校验 SDK 工具对象及 HTTP x-mcp-header 注解。"""
        if not isinstance(tool.input_schema, dict) or not isinstance(tool.description or "", str):
            raise McpDiscoveryResultError("mcp_discovery_tool_invalid")
        _validate_tool_schema(tool.input_schema)
        description = "".join(
            character
            for character in (tool.description or "")
            if unicodedata.category(character) != "Cc"
        )
        if len(description) > 8192:
            raise McpDiscoveryResultError("mcp_discovery_tool_invalid")
        if enforce_header_annotations:
            try:
                if find_invalid_x_mcp_header(tool.input_schema) is not None:
                    raise _InvalidMcpHeaderAnnotation
                if not _safe_integer_header_schemas(tool.input_schema):
                    raise _InvalidMcpHeaderAnnotation
            except RecursionError:
                raise McpDiscoveryResultError("mcp_discovery_tool_invalid") from None
        if (
            not isinstance(tool.name, str)
            or not tool.name
            or len(tool.name) > 256
            or any(unicodedata.category(character) == "Cc" for character in tool.name)
        ):
            raise McpDiscoveryResultError("mcp_discovery_tool_invalid")
        try:
            return DiscoveredMcpTool(
                remote_name=tool.name,
                description=description,
                input_schema=tool.input_schema,
            )
        except (TypeError, ValueError):
            raise McpDiscoveryResultError("mcp_discovery_tool_invalid") from None

    def _make_transport(self, connection: McpConnectionInput) -> Transport:
        """只通过已受控的 HTTP/stdio transport 建立连接。"""
        try:
            return build_controlled_mcp_transport(
                connection,
                maximum_response_bytes=self._maximum_response_bytes,
                stdio_base_environment=self._stdio_base_environment,
                transport_factory=self._transport_factory,
            )
        except ValueError as error:
            raise McpDiscoveryProtocolError("mcp_transport_unsupported") from error


def _safe_log_name(value: object) -> str:
    """转义并限制远端工具名，避免控制字符或超长日志内容。"""
    if not isinstance(value, str):
        return "<invalid>"
    return value.encode("unicode_escape", errors="backslashreplace").decode("ascii")[
        :_TOOL_NAME_MAX_LOG_CHARS
    ]


def _validate_tool_schema(schema: Mapping[str, Any]) -> None:
    """验证完整 JSON Schema 结构及大小，并要求根 schema 描述 object。"""
    try:
        ensure_object_root_schema(schema)
        encoded_schema = json.dumps(
            dict(schema),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_schema) > 256 * 1024:
            raise ValueError("schema_too_large")
        check_json_schema(schema, allow_declared_draft=True)
    except Exception:
        raise McpDiscoveryResultError("mcp_discovery_tool_invalid") from None


def _safe_integer_header_schemas(schema: Mapping[str, Any]) -> bool:
    """沿静态 properties 路径额外限制整数 Header 的可声明数值范围。"""
    for property_schema in (schema.get("properties") or {}).values():
        if not isinstance(property_schema, dict):
            continue
        if property_schema.get("x-mcp-header") is not None:
            if property_schema.get("type") == "integer" and not _integer_schema_safe(
                property_schema
            ):
                return False
        if isinstance(property_schema.get("properties"), dict) and not _safe_integer_header_schemas(
            property_schema
        ):
            return False
    return True


def _integer_schema_safe(schema: Mapping[str, Any]) -> bool:
    """拒绝静态声明超出 JavaScript safe integer 的整数范围。"""
    for bound in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        value = schema.get(bound)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or abs(value) > _SAFE_INTEGER
        ):
            return False
    enum_values = schema.get("enum")
    return not isinstance(enum_values, list) or all(
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or (math.isfinite(value) and abs(value) <= _SAFE_INTEGER)
        for value in enum_values
    )


__all__ = ["McpDiscoveryClient"]
