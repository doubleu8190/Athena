"""受控 MCP 发现客户端的分页与协议边界测试。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest
from mcp import Client, types
from mcp.client._transport import Transport
from mcp.server import Server
from mcp.types import Tool

from domain.mcp import (
    McpConnectionInput,
    McpDiscoveryCapabilityError,
    McpDiscoveryProtocolError,
    McpDiscoveryResultError,
    McpDiscoveryTimeoutError,
    StdioConfig,
    StreamableHttpConfig,
)
from infrastructure.mcp import McpDiscoveryClient, McpStdioTransport


class FakeClient:
    """以预设分页代替远端 MCP 对端。"""

    def __init__(
        self,
        pages: list[Any],
        *,
        tools_capability: bool = True,
        protocol_version: str = "2026-07-28",
    ) -> None:
        self.pages = pages
        self.server_capabilities = SimpleNamespace(tools=True if tools_capability else None)
        self.protocol_version = protocol_version
        self.cursors: list[str | None] = []
        self.closed = False
        self.session = self

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    async def list_tools(self, *, cursor: str | None = None) -> Any:
        raise AssertionError("SDK list_tools performs protocol filtering")

    async def send_request(self, request: Any, result_type: Any) -> Any:
        assert result_type is types.ListToolsResult
        cursor = request.params.cursor if request.params is not None else None
        self.cursors.append(cursor)
        page = self.pages[len(self.cursors) - 1]
        if isinstance(page, Exception):
            raise page
        return page


class FakeTransport:
    """供 fake Client 生命周期引用的空 transport。"""

    async def __aenter__(self) -> tuple[object, object]:
        return object(), object()

    async def __aexit__(self, *_args: object) -> None:
        return None


def _tool(name: str, schema: Any = None, description: str = "desc") -> Tool:
    return Tool(
        name=name,
        description=description,
        input_schema=schema or {"type": "object", "properties": {}},
    )


def _page(tools: list[Tool], cursor: str | None = None) -> Any:
    return SimpleNamespace(tools=tools, next_cursor=cursor)


def _discover(
    client: FakeClient,
    *,
    config: StreamableHttpConfig | StdioConfig | None = None,
    deadline: float = 1.0,
) -> tuple[McpDiscoveryClient, McpConnectionInput]:
    discovery = McpDiscoveryClient(
        client_factory=lambda _transport: client,
        transport_factory=lambda _connection: cast(Transport, FakeTransport()),
        deadline_seconds=deadline,
    )
    connection = McpConnectionInput(
        config=config or StdioConfig("stdio", "/bin/echo"),
        secrets={},
    )
    return discovery, connection


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def test_discovery_reads_all_pages_and_uses_sdk_auto_without_call_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [_page([_tool("one")], "page-2"), _page([_tool("two")])]
    fake = FakeClient(pages)
    modes: list[str] = []

    def client_ctor(transport: object, *, mode: str) -> FakeClient:
        assert transport is not None
        modes.append(mode)
        return fake

    from infrastructure.mcp import discovery as discovery_module

    monkeypatch.setattr(discovery_module, "Client", client_ctor)
    discovery = McpDiscoveryClient(
        transport_factory=lambda _connection: cast(Transport, FakeTransport())
    )
    connection = McpConnectionInput(StdioConfig("stdio", "/bin/echo"), {})
    found = _run(discovery.discover(connection))
    assert [tool.remote_name for tool in found] == ["one", "two"]
    assert fake.cursors == [None, "page-2"]
    assert fake.closed
    assert modes == ["auto"]


def test_empty_discovery_and_missing_tools_capability() -> None:
    empty, connection = _discover(FakeClient([_page([])]))
    assert _run(empty.discover(connection)) == ()
    missing, connection = _discover(FakeClient([_page([])], tools_capability=False))
    with pytest.raises(McpDiscoveryCapabilityError, match="mcp_tools_capability_missing"):
        _run(missing.discover(connection))


@pytest.mark.parametrize(
    "pages, expected",
    [
        ([_page([], "again"), _page([], "again")], "mcp_discovery_cursor_loop"),
        ([_page([], "next")], "mcp_discovery_page_limit"),
    ],
)
def test_discovery_rejects_cursor_loop_and_page_limit(pages: list[Any], expected: str) -> None:
    discovery, connection = _discover(FakeClient(pages))
    if expected == "mcp_discovery_page_limit":
        from infrastructure.mcp.discovery import MAX_DISCOVERY_PAGES

        pages = [_page([], f"p{index}") for index in range(MAX_DISCOVERY_PAGES)]
        discovery, connection = _discover(FakeClient(pages))
    with pytest.raises(McpDiscoveryResultError, match=expected):
        _run(discovery.discover(connection))


def test_discovery_rejects_more_than_two_hundred_tools() -> None:
    discovery, connection = _discover(
        FakeClient([_page([_tool(f"tool-{index}") for index in range(201)])])
    )
    with pytest.raises(McpDiscoveryResultError, match="mcp_discovery_tool_limit"):
        _run(discovery.discover(connection))

    invalid_header_tools = [
        _tool(f"invalid-{index}", {"type": "object", "x-mcp-header": "invalid"})
        for index in range(201)
    ]
    http_discovery, http_connection = _discover(
        FakeClient([_page(invalid_header_tools)]),
        config=StreamableHttpConfig("streamable_http", "https://mcp.example.test/rpc"),
    )
    with pytest.raises(McpDiscoveryResultError, match="mcp_discovery_tool_limit"):
        _run(http_discovery.discover(http_connection))


def test_invalid_tool_structure_fails_entire_discovery_and_description_is_cleaned() -> None:
    invalid, connection = _discover(FakeClient([_page([_tool("safe"), _tool("bad\x7fname")])]))
    with pytest.raises(McpDiscoveryResultError, match="mcp_discovery_tool_invalid"):
        _run(invalid.discover(connection))

    cleaned, connection = _discover(FakeClient([_page([_tool("safe", description="a\x00b")])]))
    assert _run(cleaned.discover(connection))[0].description == "ab"


def test_json_schema_2020_12_and_draft_07_are_checked_and_malformed_schema_fails() -> None:
    valid_schemas = [
        {"type": "object", "properties": {"q": {"type": "string"}}},
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"q": {"type": "string"}},
        },
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"q": {"type": "string"}},
        },
    ]
    tools = [_tool(f"valid-{index}", schema) for index, schema in enumerate(valid_schemas)]
    discovery, connection = _discover(FakeClient([_page(tools)]))
    assert len(_run(discovery.discover(connection))) == 3

    invalid_schemas = [
        {"type": "object", "properties": "invalid"},
        {"type": "object", "properties": {"nested": {"type": "array", "items": "bad"}}},
        {
            "$schema": "https://schemas.example.invalid/unknown",
            "type": "object",
        },
    ]
    for schema in invalid_schemas:
        discovery, connection = _discover(FakeClient([_page([_tool("invalid", schema)])]))
        with pytest.raises(McpDiscoveryResultError, match="mcp_discovery_tool_invalid"):
            _run(discovery.discover(connection))


def test_discovery_timeout_still_closes_client() -> None:
    class SlowClient(FakeClient):
        async def send_request(self, request: Any, result_type: Any) -> Any:
            await anyio.sleep(0.05)
            return _page([])

    client = SlowClient([])
    discovery, connection = _discover(client, deadline=0.001)
    with pytest.raises(McpDiscoveryTimeoutError, match="mcp_discovery_timeout"):
        _run(discovery.discover(connection))
    assert client.closed


def test_external_cancellation_closes_client_and_system_exit_is_not_wrapped() -> None:
    async def cancellation_scenario() -> None:
        started = anyio.Event()

        class WaitingClient(FakeClient):
            async def send_request(self, request: Any, result_type: Any) -> Any:
                started.set()
                await anyio.sleep_forever()

        client = WaitingClient([])
        discovery, connection = _discover(client)
        task = asyncio.create_task(discovery.discover(connection))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed

    _run(cancellation_scenario())

    class ExitClient(FakeClient):
        async def send_request(self, request: Any, result_type: Any) -> Any:
            raise SystemExit(23)

    discovery, connection = _discover(ExitClient([]))
    with pytest.raises(SystemExit, match="23"):
        _run(discovery.discover(connection))


def test_discovery_wraps_sdk_and_cleanup_errors_without_echoing_details() -> None:
    class PrivateErrorClient(FakeClient):
        async def send_request(self, request: Any, result_type: Any) -> Any:
            raise RuntimeError("Authorization: super-secret-value")

    discovery, connection = _discover(PrivateErrorClient([]))
    with pytest.raises(McpDiscoveryProtocolError) as error:
        _run(discovery.discover(connection))
    assert str(error.value) == "mcp_discovery_protocol_failed"
    assert "super-secret-value" not in str(error.value)

    class CleanupFailureClient(FakeClient):
        async def __aexit__(self, *_args: object) -> None:
            raise RuntimeError("cleanup secret-value")

    discovery, connection = _discover(CleanupFailureClient([_page([])]))
    with pytest.raises(McpDiscoveryProtocolError) as error:
        _run(discovery.discover(connection))
    assert str(error.value) == "mcp_discovery_protocol_failed"
    assert "secret-value" not in str(error.value)


def test_http_header_annotations_accept_safe_properties_and_drop_invalid_tools() -> None:
    config = StreamableHttpConfig("streamable_http", "https://mcp.example.test/rpc")
    valid_schema = {
        "type": "object",
        "properties": {
            "tenant": {"type": "string", "x-mcp-header": "X-Tenant"},
            "count": {
                "type": "integer",
                "exclusiveMinimum": 0.5,
                "maximum": 10,
                "x-mcp-header": "X-Count",
            },
            "enabled": {"type": "boolean", "x-mcp-header": "X-Enabled"},
        },
    }
    invalid_schemas = [
        {"type": "object", "x-mcp-header": "X-Root"},
        {"type": "object", "properties": {"a": {"type": "number", "x-mcp-header": "X-A"}}},
        {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": ""}}},
        {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "Bad Header"}}},
        {
            "type": "object",
            "properties": {"a": {"type": ["string", "integer"], "x-mcp-header": "X-Union"}},
        },
        {
            "type": "object",
            "properties": {
                "a": {"type": "string", "x-mcp-header": "X-Same"},
                "b": {"type": "boolean", "x-mcp-header": "x-same"},
            },
        },
        {
            "type": "object",
            "properties": {"a": {"type": "integer", "maximum": 2**53, "x-mcp-header": "X-Count"}},
        },
        {
            "type": "object",
            "properties": {
                "a": {"type": "array", "items": {"type": "string", "x-mcp-header": "X-Item"}}
            },
        },
        {
            "type": "object",
            "properties": {"a": {"type": "string", "allOf": [{"x-mcp-header": "X-Combo"}]}},
        },
    ]
    fake = FakeClient(
        [
            _page(
                [
                    _tool("safe", valid_schema),
                    *[_tool(f"bad-{i}", schema) for i, schema in enumerate(invalid_schemas)],
                ]
            )
        ]
    )
    discovery, connection = _discover(fake, config=config)
    found = _run(discovery.discover(connection))
    assert [tool.remote_name for tool in found] == ["safe"]


def test_http_header_filter_logs_only_escaped_limited_tool_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeClient(
        [_page([_tool("malicious\nname", {"type": "object", "x-mcp-header": "bad"})])]
    )
    discovery, connection = _discover(
        fake,
        config=StreamableHttpConfig("streamable_http", "https://mcp.example.test/rpc"),
    )
    assert _run(discovery.discover(connection)) == ()
    assert "malicious\\nname" in caplog.text
    assert "malicious\nname" not in caplog.text
    assert "x-mcp-header" not in caplog.text


def test_real_sdk_session_request_applies_annotation_rule_only_to_modern_http() -> None:
    async def list_tools(_context: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                _tool(
                    "stdio-invalid-annotation",
                    {"type": "object", "x-mcp-header": "invalid-at-root"},
                )
            ]
        )

    def real_client(_transport: Transport, *, mode: str) -> Any:
        server = Server("in-process-discovery", on_list_tools=list_tools)
        return cast(Any, Client(server, mode=mode))

    def discover(mode: str, config: StdioConfig | StreamableHttpConfig) -> tuple[Any, ...]:
        discovery = McpDiscoveryClient(
            client_factory=lambda transport: real_client(transport, mode=mode),
            transport_factory=lambda _connection: cast(Transport, FakeTransport()),
        )
        return _run(discovery.discover(McpConnectionInput(config, {})))

    stdio_tools = discover("auto", StdioConfig("stdio", "/bin/echo"))
    modern_http_tools = discover(
        "auto", StreamableHttpConfig("streamable_http", "https://mcp.example.test/rpc")
    )
    legacy_http_tools = discover(
        "legacy", StreamableHttpConfig("streamable_http", "https://mcp.example.test/rpc")
    )
    assert [tool.remote_name for tool in stdio_tools] == ["stdio-invalid-annotation"]
    assert modern_http_tools == ()
    assert [tool.remote_name for tool in legacy_http_tools] == ["stdio-invalid-annotation"]


def test_stdio_ignores_header_annotation_and_enforces_environment_allowlist() -> None:
    schema = {"type": "object", "x-mcp-header": "invalid-at-root"}
    fake = FakeClient([_page([_tool("stdio-safe", schema)])])
    discovery, connection = _discover(fake, config=StdioConfig("stdio", "/bin/echo"))
    assert [tool.remote_name for tool in _run(discovery.discover(connection))] == ["stdio-safe"]

    configured = McpDiscoveryClient(
        stdio_base_environment={"LANG": "C.UTF-8"},
        stdio_allowed_environment_names=frozenset({"LANG"}),
    )
    transport = configured._make_transport(  # noqa: SLF001
        McpConnectionInput(StdioConfig("stdio", "/bin/echo"), {"API_TOKEN": "secret"})
    )
    assert isinstance(transport, McpStdioTransport)
    assert transport._environment == {"LANG": "C.UTF-8", "API_TOKEN": "secret"}  # noqa: SLF001
    assert "PATH" not in transport._environment  # noqa: SLF001


def test_stdio_discovery_applies_small_output_limit_and_rejects_oversized_server() -> None:
    connection = McpConnectionInput(
        StdioConfig(
            "stdio",
            sys.executable,
            ("-c", "import sys; sys.stdout.write('x' * 4096); sys.stdout.flush()"),
        ),
        {},
    )
    discovery = McpDiscoveryClient(maximum_response_bytes=64, deadline_seconds=5)
    transport = discovery._make_transport(connection)  # noqa: SLF001
    assert isinstance(transport, McpStdioTransport)
    assert transport._maximum_line_bytes == 64  # noqa: SLF001
    assert transport._maximum_total_bytes == 64  # noqa: SLF001
    with pytest.raises(McpDiscoveryProtocolError, match="mcp_discovery_protocol_failed"):
        _run(discovery.discover(connection))


def test_http_transport_receives_only_current_operation_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, str]] = []

    def pinned_transport(endpoint: str, **kwargs: Any) -> FakeTransport:
        assert endpoint == "https://mcp.example.test/rpc"
        captured.append(kwargs["headers"])
        return FakeTransport()

    from infrastructure.mcp import transport as transport_module

    monkeypatch.setattr(transport_module, "pinned_streamable_http_transport", pinned_transport)
    discovery = McpDiscoveryClient(
        client_factory=lambda _transport: FakeClient([_page([])]),
        allowed_private_hosts=frozenset({"mcp.example.test"}),
    )

    async def discover_token(token: str) -> None:
        connection = McpConnectionInput(
            StreamableHttpConfig("streamable_http", "https://mcp.example.test/rpc"),
            {"Authorization": token},
        )
        await discovery.discover(connection)

    async def scenario() -> None:
        await asyncio.gather(discover_token("alpha"), discover_token("beta"))

    _run(scenario())
    assert sorted(captured, key=lambda item: item["Authorization"]) == [
        {"Authorization": "alpha"},
        {"Authorization": "beta"},
    ]
