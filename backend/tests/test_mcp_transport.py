"""MCP transport 的受控内网策略、协议协商和子进程边界测试。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import signal
import ssl
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import anyio
import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import (
    DiscoverResult,
    Implementation,
    InitializeResult,
    JSONRPCResponse,
    ListToolsResult,
    ServerCapabilities,
    ToolsCapability,
)

from infrastructure.mcp.http import (
    McpAccessDeniedError,
    McpEndpointError,
    McpRedirectRejectedError,
    build_mcp_http_client,
    validate_mcp_endpoint,
)
from infrastructure.mcp.stdio import (
    McpStdioConfigurationError,
    McpStdioTransport,
)


class _RecordingTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        *,
        status_code: int = 200,
        body: bytes = b"{}",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.extra_headers = extra_headers or {}
        self.requests: list[httpx2.Request] = []
        self.methods: list[str] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        try:
            message = json.loads(await request.aread())
            if isinstance(message, dict) and isinstance(message.get("method"), str):
                self.methods.append(message["method"])
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return httpx2.Response(
            self.status_code,
            headers={"content-type": "application/json", **self.extra_headers},
            content=self.body,
            request=request,
        )


class _BlockingStream(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> Any:
        await anyio.sleep_forever()
        yield b""

    async def aclose(self) -> None:
        self.closed = True


class _McpPeerTransport(httpx2.AsyncBaseTransport):
    def __init__(self, *, legacy: bool = False) -> None:
        self.legacy = legacy
        self.methods: list[str] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        message = json.loads(await request.aread())
        method = message["method"]
        self.methods.append(method)
        if method == "server/discover":
            if self.legacy:
                response_body = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                }
            else:
                result: dict[str, Any] = DiscoverResult(
                    supported_versions=["2026-07-28"],
                    capabilities=ServerCapabilities(tools=ToolsCapability()),
                ).model_dump(by_alias=True, mode="json", exclude_none=True)
                response_body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        elif method == "initialize":
            if not self.legacy:
                response_body = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                }
            else:
                result = InitializeResult(
                    protocol_version="2025-11-25",
                    capabilities=ServerCapabilities(tools=ToolsCapability()),
                    server_info=Implementation(name="test", version="1"),
                ).model_dump(by_alias=True, mode="json", exclude_none=True)
                response_body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        elif method == "tools/list":
            result = ListToolsResult(tools=[]).model_dump(
                by_alias=True,
                mode="json",
                exclude_none=True,
            )
            response_body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        elif method == "notifications/initialized":
            return httpx2.Response(202, request=request)
        else:
            response_body = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "Method not found"},
            }
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(response_body).encode(),
            request=request,
        )


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _exception_leaf(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return error


def test_endpoint_requires_https_rejects_credentials_query_and_cloud_metadata_ips() -> None:
    """必须使用 HTTPS、禁止 URL 凭据/查询串/片段与云元数据 IP，放行受控内网地址。"""
    for bad_endpoint in (
        "http://example.com/mcp",
        "https://user:secret@example.com/mcp",
        "https://example.com/mcp?token=secret",
        "https://example.com/mcp?",
        "https://example.com/mcp#section",
        "https://example.com/mcp#",
        "https://169.254.169.254/latest/meta-data",
        "https://169.254.170.2/v2/credentials",
        "https://100.100.100.200/latest/meta-data",
        "https://[fd00:ec2::254]/latest/meta-data",
        "https://[::ffff:169.254.169.254]/latest/meta-data",
        "https://example.com/mcp\r\nX-Inject: 1",
    ):
        with pytest.raises(McpEndpointError):
            validate_mcp_endpoint(bad_endpoint)

    # 超管受控内网 MCP 与公网 HTTPS 地址均可合法通过。
    for ok_endpoint in (
        "https://mcp.internal/mcp",
        "https://10.0.0.2:8443/mcp",
        "https://127.0.0.1:8443/mcp",
        "https://example.com/mcp",
    ):
        parsed = validate_mcp_endpoint(ok_endpoint)
        assert parsed.url.scheme == "https"


def test_local_tls_uses_custom_ca_and_enforces_timeout(tmp_path: Any) -> None:
    """标准 httpx2 TLS 连接应严格校验 CA 证书并在超慢响应时触发超时。"""

    async def scenario() -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
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
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        client_context = ssl.create_default_context(cafile=str(cert_path))

        async def serve_connection(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            while (await reader.readline()) not in {b"\r\n", b"\n", b""}:
                pass
            if b"/slow " in request_line:
                await asyncio.sleep(0.2)
            with suppress(OSError):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                await writer.drain()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

        listener = await asyncio.start_server(
            serve_connection,
            host="127.0.0.1",
            port=0,
            ssl=server_context,
        )
        port = cast(Any, listener.sockets[0]).getsockname()[1]
        async with listener:
            async with build_mcp_http_client(
                f"https://127.0.0.1:{port}/mcp",
                transport=httpx2.AsyncHTTPTransport(verify=client_context, trust_env=False),
            ) as client:
                response = await client.get(f"https://127.0.0.1:{port}/mcp")
                assert response.status_code == 200

            async with build_mcp_http_client(
                f"https://127.0.0.1:{port}/slow",
                transport=httpx2.AsyncHTTPTransport(verify=client_context, trust_env=False),
                timeout=httpx2.Timeout(0.05),
            ) as slow_client:
                with pytest.raises(httpx2.ReadTimeout):
                    await slow_client.get(f"https://127.0.0.1:{port}/slow")

    _run(scenario())


@pytest.mark.parametrize("status_code", [301, 302, 307, 308])
def test_redirects_fail_before_a_second_request(status_code: int) -> None:
    """所有 3xx（含同源重定向）均由 response hook 立即拒绝，绝不发出第二次请求。"""

    async def scenario() -> None:
        inner = _RecordingTransport(
            status_code=status_code,
            extra_headers={"location": "https://example.com/mcp/"},
        )
        async with build_mcp_http_client("https://example.com/mcp", transport=inner) as client:
            with pytest.raises(McpRedirectRejectedError):
                await client.get("https://example.com/mcp")
        assert len(inner.requests) == 1

        # 经由官方 streamable_http_client 调用时，同源 3xx 同样被拦截，不会触发自动跟随。
        sdk_inner = _RecordingTransport(
            status_code=status_code,
            extra_headers={"location": "https://example.com/mcp/"},
        )
        client2 = build_mcp_http_client("https://example.com/mcp", transport=sdk_inner)
        transport = streamable_http_client("https://example.com/mcp", http_client=client2)
        try:
            with pytest.raises(ExceptionGroup) as error:
                async with Client(transport, mode="auto"):
                    pass
        finally:
            await client2.aclose()
        assert len(sdk_inner.requests) == 1
        assert isinstance(_exception_leaf(error.value), McpRedirectRejectedError)

    _run(scenario())


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_errors_are_not_mcp_errors_and_do_not_retry(status_code: int) -> None:
    async def scenario() -> None:
        inner = _RecordingTransport(status_code=status_code)
        client = build_mcp_http_client("https://example.com/mcp", transport=inner)
        transport = streamable_http_client("https://example.com/mcp", http_client=client)
        try:
            with pytest.raises(ExceptionGroup) as error:
                async with Client(transport, mode="auto"):
                    pass
        finally:
            await client.aclose()
        assert len(inner.requests) == 1
        assert inner.methods == ["server/discover"]
        assert isinstance(_exception_leaf(error.value), McpAccessDeniedError)

    _run(scenario())


@pytest.mark.parametrize("legacy", [False, True])
def test_sdk_auto_discovers_modern_and_legacy_without_tools_call(legacy: bool) -> None:
    async def scenario() -> None:
        peer = _McpPeerTransport(legacy=legacy)
        async with build_mcp_http_client("https://example.com/mcp", transport=peer) as client:
            transport = streamable_http_client("https://example.com/mcp", http_client=client)
            async with Client(transport, mode="auto") as mcp_client:
                tools = await mcp_client.list_tools()
                assert tools.tools == []
        assert "tools/list" in peer.methods
        assert "tools/call" not in peer.methods
        if legacy:
            assert peer.methods[:2] == ["server/discover", "initialize"]
        else:
            assert peer.methods[0] == "server/discover"

    _run(scenario())


def test_cancelled_http_response_read_closes_the_underlying_stream() -> None:
    async def scenario() -> None:
        blocking_stream = _BlockingStream()

        class StreamTransport(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
                return httpx2.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=blocking_stream,
                    request=request,
                )

        async with build_mcp_http_client(
            "https://example.com/mcp", transport=StreamTransport()
        ) as client:
            with anyio.move_on_after(0.02):
                async with client.stream("GET", "https://example.com/mcp") as response:
                    await response.aread()
        assert blocking_stream.closed

    _run(scenario())


def test_stdio_requires_absolute_executable_and_cwd() -> None:
    with pytest.raises(McpStdioConfigurationError):
        McpStdioTransport("python")
    with pytest.raises(McpStdioConfigurationError):
        McpStdioTransport(sys.executable, cwd="relative")


def test_stdio_uses_only_whitelisted_environment_and_argv_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """委托官方 SDK stdio_client 时不透传 ATHENA_* 等宿主机密钥，且不经 shell 展开参数。"""
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "must-not-leak-into-stdio")
    monkeypatch.setenv("LEAK", "must-not-leak")

    async def scenario() -> None:
        command = [
            sys.executable,
            "-c",
            "import json, os, sys; result = {'jsonrpc': '2.0', 'id': 1, 'result': {"
            "'env': os.getenv('ALLOWED', '') + '|' + os.getenv('LEAK', '') + '|' "
            "+ os.getenv('ATHENA_MCP_SECRET_KEY', ''), "
            "'arg': sys.argv[1]}}; print(json.dumps(result), flush=True)",
            "$(touch /tmp/athena-mcp-shell-marker)",
        ]
        transport = McpStdioTransport(
            command[0],
            command[1:],
            env={"ALLOWED": "yes"},
            base_environment={"PATH": os.defpath},
        )
        read_stream, _ = await transport.__aenter__()
        try:
            message = await read_stream.receive()
            assert not isinstance(message, Exception)
            assert isinstance(message.message, JSONRPCResponse)
            assert message.message.result["env"] == "yes||"
            assert message.message.result["arg"] == "$(touch /tmp/athena-mcp-shell-marker)"
        finally:
            await transport.__aexit__(None, None, None)

    _run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group signaling required")
@pytest.mark.parametrize("ignore_term", [False, True])
def test_stdio_closes_stdin_and_reaps_terminated_process_group(
    tmp_path: Any,
    ignore_term: bool,
) -> None:
    """官方 SDK stdio_client 在子进程忽略 SIGTERM 时也应升级 SIGKILL 彻底回收进程组。"""

    async def scenario() -> None:
        pid_path = tmp_path / "stdio-child.pid"
        signal_setup = "signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else ""
        script = (
            f"import os,signal,time; {signal_setup}"
            f"open({str(pid_path)!r},'w').write(str(os.getpid())); time.sleep(20)"
        )
        transport = McpStdioTransport(
            sys.executable,
            ("-c", script),
            base_environment={"PATH": os.defpath},
        )
        await transport.__aenter__()
        for _ in range(100):
            if pid_path.exists():
                break
            await asyncio.sleep(0.01)
        process_id = int(pid_path.read_text())
        await transport.__aexit__(None, None, None)
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, signal.SIGCONT)

    _run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group signaling required")
def test_stdio_cancellation_still_reaps_process(tmp_path: Any) -> None:
    async def scenario() -> None:
        pid_path = tmp_path / "cancelled-stdio-child.pid"
        script = (
            f"import os,time; open({str(pid_path)!r},'w').write(str(os.getpid())); time.sleep(20)"
        )
        transport = McpStdioTransport(
            sys.executable,
            ("-c", script),
            base_environment={"PATH": os.defpath},
        )
        with anyio.move_on_after(0.15) as cancellation_scope:
            async with transport:
                for _ in range(100):
                    if pid_path.exists():
                        break
                    await asyncio.sleep(0.01)
                await anyio.sleep_forever()
        assert cancellation_scope.cancelled_caught
        process_id = int(pid_path.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, signal.SIGCONT)

    _run(scenario())
