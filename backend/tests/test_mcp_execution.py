"""Real MCP execution tests for one-call transport behavior."""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import sys
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import Client, MCPError, types
from mcp.server.mcpserver.context import Context

from domain.mcp import (
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolTarget,
    StdioConfig,
    StreamableHttpConfig,
)
from infrastructure.mcp.execution import McpToolCallClient
from infrastructure.mcp.http import pinned_streamable_http_transport
from infrastructure.mcp.stdio import McpStdioTransport


class _ObservedClient:
    """Delegate to the real SDK client while recording mandatory teardown."""

    def __init__(self, transport: Any, deadline_seconds: float) -> None:
        self._inner = Client(
            transport,
            mode="auto",
            read_timeout_seconds=deadline_seconds,
            cache=None,
        )
        self.exit_started = asyncio.Event()
        self.exit_finished = asyncio.Event()

    @property
    def session(self) -> Any:
        return self._inner.session

    @property
    def server_capabilities(self) -> Any:
        return self._inner.server_capabilities

    async def __aenter__(self) -> _ObservedClient:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exit_started.set()
        try:
            await self._inner.__aexit__(None, None, None)
        finally:
            self.exit_finished.set()


def _certificate_material(tmp_path: Path) -> tuple[Path, Path, ssl.SSLContext]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("mcp.test")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    cert_path = tmp_path / "mcp-cert.pem"
    key_path = tmp_path / "mcp-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path, ssl.create_default_context(cafile=str(cert_path))


def _free_tcp_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])
    finally:
        listener.close()


def _http_target(port: int) -> McpRuntimeToolTarget:
    return McpRuntimeToolTarget(
        tool_id="tool_test",
        server_id="server_test",
        remote_name="echo",
        config_version=1,
        connection=McpConnectionInput(
            StreamableHttpConfig(
                transport="streamable_http",
                url=f"https://mcp.test:{port}/mcp",
            ),
            {"Authorization": "Bearer test-secret"},
        ),
    )


@asynccontextmanager
async def _tls_http_server(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[
        int,
        dict[str, int],
        dict[str, asyncio.Event],
        dict[str, asyncio.Event],
        list[_ObservedClient],
    ]
]:
    from mcp.server import MCPServer

    cert_path, key_path, client_context = _certificate_material(tmp_path)
    calls: dict[str, int] = defaultdict(int)
    started = {"slow": asyncio.Event(), "cancel": asyncio.Event()}
    finished = {"slow": asyncio.Event(), "cancel": asyncio.Event()}
    clients: list[_ObservedClient] = []

    server = MCPServer("athena-execution-test")

    @server.tool()
    async def echo(query: str, ctx: Context) -> dict[str, object]:
        calls["echo"] += 1
        return {
            "query": query,
            "authorization": (ctx.headers or {}).get("authorization"),
        }

    @server.tool()
    async def slow(query: str) -> str:
        del query
        calls["slow"] += 1
        started["slow"].set()
        try:
            await asyncio.sleep(1.0)
            return "late-result"
        finally:
            finished["slow"].set()

    @server.tool()
    async def cancel(query: str) -> str:
        del query
        calls["cancel"] += 1
        started["cancel"].set()
        try:
            await asyncio.sleep(1.0)
            return "late-cancel-result"
        finally:
            finished["cancel"].set()

    port = _free_tcp_port()
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        host="mcp.test",
    )
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_level="error",
            access_log=False,
            lifespan="on",
            timeout_keep_alive=1,
        )
    )
    server_task = asyncio.create_task(uvicorn_server.serve())
    try:
        for _ in range(200):
            if uvicorn_server.started:
                break
            if server_task.done():
                await server_task
            await asyncio.sleep(0.01)
        if not uvicorn_server.started:
            raise AssertionError("TLS MCP server did not start")

        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("127.0.0.1",)

        def transport_factory(connection: McpConnectionInput) -> Any:
            assert isinstance(connection.config, StreamableHttpConfig)
            return pinned_streamable_http_transport(
                connection.config.url,
                headers=dict(connection.secrets),
                allowed_private_hosts=frozenset({"mcp.test"}),
                resolver=resolver,
                inner_transport=httpx2.AsyncHTTPTransport(
                    verify=client_context,
                    trust_env=False,
                ),
            )

        def client_factory(transport: Any, deadline_seconds: float) -> _ObservedClient:
            client = _ObservedClient(transport, deadline_seconds)
            clients.append(client)
            return client

        yield (
            port,
            calls,
            started,
            finished,
            clients,
        )
    finally:
        uvicorn_server.should_exit = True
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=5)


def test_real_tls_http_execution_is_single_shot_and_bounded(tmp_path: Path) -> None:
    """Each call opens one real TLS client; timeout/cancel never resends the business call."""

    async def scenario() -> None:
        async with _tls_http_server(tmp_path) as (
            port,
            calls,
            started,
            finished,
            clients,
        ):
            executor_clients: list[McpToolCallClient] = []

            async def resolver(_: str, __: int) -> Iterable[str]:
                return ("127.0.0.1",)

            cert_path = tmp_path / "mcp-cert.pem"
            client_context = ssl.create_default_context(cafile=str(cert_path))

            def transport_factory(connection: McpConnectionInput) -> Any:
                assert isinstance(connection.config, StreamableHttpConfig)
                return pinned_streamable_http_transport(
                    connection.config.url,
                    headers=dict(connection.secrets),
                    allowed_private_hosts=frozenset({"mcp.test"}),
                    resolver=resolver,
                    inner_transport=httpx2.AsyncHTTPTransport(
                        verify=client_context,
                        trust_env=False,
                    ),
                )

            def client_factory(transport: Any, deadline_seconds: float) -> _ObservedClient:
                client = _ObservedClient(transport, deadline_seconds)
                clients.append(client)
                return client

            executor = McpToolCallClient(
                allowed_private_hosts=frozenset({"mcp.test"}),
                client_factory=client_factory,
                transport_factory=transport_factory,
                cleanup_timeout_seconds=2,
            )
            executor_clients.append(executor)
            target = _http_target(port)

            first = await executor.call(target, {"query": "one"}, deadline_seconds=5)
            second = await executor.call(target, {"query": "two"}, deadline_seconds=5)
            assert first.status is McpCallStatus.OK
            assert second.status is McpCallStatus.OK
            assert first.structured == {
                "query": "one",
                "authorization": "Bearer test-secret",
            }
            assert second.structured == {
                "query": "two",
                "authorization": "Bearer test-secret",
            }
            assert calls["echo"] == 2
            assert len(clients) == 2
            assert all(client.exit_started.is_set() for client in clients)
            assert all(client.exit_finished.is_set() for client in clients)

            timeout_target = McpRuntimeToolTarget(
                tool_id="tool_slow",
                server_id="server_test",
                remote_name="slow",
                config_version=1,
                connection=target.connection,
            )
            timeout_result = await executor.call(
                timeout_target,
                {"query": "timeout"},
                deadline_seconds=0.15,
            )
            assert timeout_result.status is McpCallStatus.UNKNOWN
            assert timeout_result.code == "mcp_tool_timeout"
            await asyncio.wait_for(finished["slow"].wait(), timeout=3)
            assert calls["slow"] == 1
            assert len(clients) == 3
            assert clients[2].exit_finished.is_set()

            cancel_target = McpRuntimeToolTarget(
                tool_id="tool_cancel",
                server_id="server_test",
                remote_name="cancel",
                config_version=1,
                connection=target.connection,
            )
            cancelled_call = asyncio.create_task(
                executor.call(
                    cancel_target,
                    {"query": "cancel"},
                    deadline_seconds=5,
                )
            )
            await asyncio.wait_for(started["cancel"].wait(), timeout=2)
            cancelled_call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_call
            await asyncio.wait_for(finished["cancel"].wait(), timeout=3)
            assert calls["cancel"] == 1
            assert len(clients) == 4
            assert clients[3].exit_finished.is_set()
            await asyncio.sleep(0.05)
            assert calls["cancel"] == 1

    asyncio.run(scenario())


class _MappingSession:
    def __init__(self, *, phase: str) -> None:
        self._phase = phase

    async def list_tools(self, *, params: Any = None) -> Any:
        del params
        if self._phase == "before":
            raise MCPError(-32603, "protocol failure")
        return SimpleNamespace(
            tools=[SimpleNamespace(name="remote_tool")],
            next_cursor=None,
        )

    async def call_tool(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise MCPError(-32603, "protocol failure after send")


class _MappingClient:
    def __init__(self, *, phase: str) -> None:
        self.server_capabilities = SimpleNamespace(tools=object())
        self.session = _MappingSession(phase=phase)
        self.exited = False

    async def __aenter__(self) -> _MappingClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args
        self.exited = True


def _successful_call_result() -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="known result")],
        structured_content={"answer": "known"},
        is_error=False,
    )


class _SuccessfulSession:
    async def list_tools(self, *, params: Any = None) -> Any:
        del params
        return SimpleNamespace(
            tools=[SimpleNamespace(name="remote_tool")],
            next_cursor=None,
        )

    async def call_tool(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        return _successful_call_result()


def test_cleanup_failure_preserves_confirmed_mcp_success() -> None:
    """连接清理失败不能覆盖已经确认成功的业务结果。"""

    class _CleanupFailClient:
        server_capabilities = SimpleNamespace(tools=object())

        def __init__(self) -> None:
            self.session = _SuccessfulSession()
            self.exit_count = 0

        async def __aenter__(self) -> _CleanupFailClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args
            self.exit_count += 1
            raise RuntimeError("cleanup failed")

    async def scenario() -> None:
        clients: list[_CleanupFailClient] = []

        def factory(transport: Any, deadline_seconds: float) -> _CleanupFailClient:
            del transport, deadline_seconds
            client = _CleanupFailClient()
            clients.append(client)
            return client

        executor = McpToolCallClient(
            client_factory=factory,
            transport_factory=lambda _: cast(Any, object()),
        )
        target = McpRuntimeToolTarget(
            tool_id="tool_test",
            server_id="server_test",
            remote_name="remote_tool",
            config_version=1,
            connection=McpConnectionInput(
                StdioConfig(transport="stdio", command=sys.executable),
                {},
            ),
        )
        result = await executor.call(target, {}, deadline_seconds=1)
        assert result.status is McpCallStatus.OK
        assert result.code == "mcp_tool_completed"
        assert result.structured == {"answer": "known"}
        assert result.cleanup_error_code == "mcp_cleanup_failed"
        assert clients[0].exit_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("cleanup_timeout_seconds", "expected_cleanup_error"),
    [
        (0.2, None),
        (0.03, "mcp_cleanup_failed"),
    ],
)
def test_slow_cleanup_never_rewrites_confirmed_business_success(
    cleanup_timeout_seconds: float,
    expected_cleanup_error: str | None,
) -> None:
    """Business success is retained when bounded teardown outlives the call deadline."""

    class _SlowCleanupClient:
        server_capabilities = SimpleNamespace(tools=object())

        def __init__(self) -> None:
            self.session = _SuccessfulSession()
            self.exit_count = 0

        async def __aenter__(self) -> _SlowCleanupClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args
            self.exit_count += 1
            await asyncio.sleep(0.1)

    async def scenario() -> None:
        clients: list[_SlowCleanupClient] = []

        def factory(transport: Any, deadline_seconds: float) -> _SlowCleanupClient:
            del transport, deadline_seconds
            client = _SlowCleanupClient()
            clients.append(client)
            return client

        executor = McpToolCallClient(
            client_factory=factory,
            transport_factory=lambda _: cast(Any, object()),
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        )
        target = McpRuntimeToolTarget(
            tool_id="tool_test",
            server_id="server_test",
            remote_name="remote_tool",
            config_version=1,
            connection=McpConnectionInput(
                StdioConfig(transport="stdio", command=sys.executable),
                {},
            ),
        )
        started_at = asyncio.get_running_loop().time()
        result = await executor.call(target, {}, deadline_seconds=0.02)
        elapsed = asyncio.get_running_loop().time() - started_at

        assert elapsed > 0.02
        assert result.status is McpCallStatus.OK
        assert result.code == "mcp_tool_completed"
        assert result.structured == {"answer": "known"}
        assert result.cleanup_error_code == expected_cleanup_error
        assert clients[0].exit_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("phase", "expected", "not_executed_code"),
    [
        ("before", McpCallStatus.ERROR, "mcp_tool_error"),
        ("after", McpCallStatus.UNKNOWN, "mcp_tool_protocol_unknown"),
    ],
)
def test_mcp_protocol_errors_before_and_after_admission(
    phase: str,
    expected: McpCallStatus,
    not_executed_code: str,
) -> None:
    """A protocol error is definite only before tools/call admission."""

    async def scenario() -> None:
        client_holder: list[_MappingClient] = []

        def factory(transport: Any, deadline_seconds: float) -> _MappingClient:
            del transport, deadline_seconds
            client = _MappingClient(phase=phase)
            client_holder.append(client)
            return client

        executor = McpToolCallClient(
            client_factory=factory,
            transport_factory=lambda _: cast(Any, object()),
        )
        target = McpRuntimeToolTarget(
            tool_id="tool_test",
            server_id="server_test",
            remote_name="remote_tool",
            config_version=1,
            connection=McpConnectionInput(
                StdioConfig(transport="stdio", command=sys.executable),
                {},
            ),
        )
        result = await executor.call(target, {}, deadline_seconds=1)
        assert result.status is expected
        assert result.code == not_executed_code
        assert client_holder[0].exited

    asyncio.run(scenario())


_STDIO_SERVER_SCRIPT = r"""
import asyncio
import os
import signal
import subprocess
import sys

from mcp.server import MCPServer

log_path = sys.argv[1]
descendant = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
with open(log_path, "a", encoding="utf-8") as output:
    output.write(f"{os.getpid()} {descendant.pid}\n")

server = MCPServer("athena-stdio-test")
calls = {"count": 0}

@server.tool()
async def stdio_probe(query: str) -> dict[str, object]:
    calls["count"] += 1
    return {
        "query": query,
        "pid": os.getpid(),
        "calls": calls["count"],
    }

asyncio.run(server.run_stdio_async())
"""


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_process_exit(process_id: int, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _process_exists(process_id):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@pytest.mark.skipif(os.name != "posix", reason="process group cleanup requires POSIX")
def test_real_stdio_execution_uses_new_process_and_reaps_group(tmp_path: Path) -> None:
    """Two calls use two real stdio servers and each process group is reaped."""

    async def scenario() -> None:
        script_path = tmp_path / "stdio_server.py"
        log_path = tmp_path / "stdio-processes.log"
        script_path.write_text(_STDIO_SERVER_SCRIPT, encoding="utf-8")
        observed_clients: list[_ObservedClient] = []
        base_environment = {"PATH": os.defpath}

        def transport_factory(connection: McpConnectionInput) -> Any:
            assert isinstance(connection.config, StdioConfig)
            return McpStdioTransport(
                connection.config.command,
                (*connection.config.args, str(log_path)),
                cwd=connection.config.cwd,
                env=connection.secrets,
                base_environment=base_environment,
                maximum_line_bytes=2 * 1024 * 1024,
                maximum_total_bytes=2 * 1024 * 1024,
                grace_seconds=0.1,
                term_seconds=0.3,
                kill_seconds=0.3,
            )

        def client_factory(transport: Any, deadline_seconds: float) -> _ObservedClient:
            client = _ObservedClient(transport, deadline_seconds)
            observed_clients.append(client)
            return client

        executor = McpToolCallClient(
            stdio_base_environment=base_environment,
            stdio_allowed_environment_names=frozenset({"PATH"}),
            client_factory=client_factory,
            transport_factory=transport_factory,
            cleanup_timeout_seconds=3,
        )
        target = McpRuntimeToolTarget(
            tool_id="tool_stdio",
            server_id="server_stdio",
            remote_name="stdio_probe",
            config_version=1,
            connection=McpConnectionInput(
                StdioConfig(
                    transport="stdio",
                    command=sys.executable,
                    args=(str(script_path),),
                ),
                {},
            ),
        )

        first = await executor.call(target, {"query": "one"}, deadline_seconds=10)
        second = await executor.call(target, {"query": "two"}, deadline_seconds=10)
        assert first.status is McpCallStatus.OK
        assert second.status is McpCallStatus.OK
        assert first.structured is not None and second.structured is not None
        first_data = cast(dict[str, object], first.structured)
        second_data = cast(dict[str, object], second.structured)
        assert first_data["calls"] == second_data["calls"] == 1
        assert first_data["pid"] != second_data["pid"]
        assert len(observed_clients) == 2
        assert all(client.exit_finished.is_set() for client in observed_clients)

        process_rows = [
            tuple(int(value) for value in line.split())
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(process_rows) == 2
        assert process_rows[0][0] == first_data["pid"]
        assert process_rows[1][0] == second_data["pid"]
        for server_pid, descendant_pid in process_rows:
            assert await _wait_for_process_exit(server_pid)
            assert await _wait_for_process_exit(descendant_pid)

    asyncio.run(scenario())
