"""MCP transport 的网络策略、协议协商和资源边界测试。"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import signal
import ssl
import sys
from collections.abc import Iterable
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
    DEFAULT_MAX_RESPONSE_BYTES,
    McpAccessDeniedError,
    McpEndpointError,
    McpRedirectRejectedError,
    McpResponseTooLargeError,
    PinnedHttpTransport,
    _is_allowed_address,
    build_pinned_http_client,
    validate_mcp_endpoint,
)
from infrastructure.mcp.stdio import (
    McpStdioCleanupError,
    McpStdioConfigurationError,
    McpStdioProcessError,
    McpStdioTransport,
)


class _RecordingTransport(httpx2.AsyncBaseTransport):
    def __init__(self, *, status_code: int = 200, body: bytes = b"{}") -> None:
        self.status_code = status_code
        self.body = body
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
            headers={"content-type": "application/json"},
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


class _UnconsumedStream(httpx2.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.iterated = False
        self.closed = False

    async def __aiter__(self) -> Any:
        self.iterated = True
        yield self._content

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


def test_endpoint_requires_https_and_rejects_url_credentials_and_parameters() -> None:
    for endpoint in (
        "http://example.com/mcp",
        "https://user:secret@example.com/mcp",
        "https://example.com/mcp?token=secret",
        "https://example.com/mcp#section",
    ):
        with pytest.raises(McpEndpointError):
            validate_mcp_endpoint(endpoint)


def test_dns_rejects_private_addresses_unless_exact_host_is_allowed() -> None:
    async def scenario() -> None:
        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("10.0.0.2",)

        transport = PinnedHttpTransport("https://mcp.internal/mcp", resolver=resolver)
        with pytest.raises(McpEndpointError, match="mcp_dns_address_rejected"):
            await transport._checked_addresses()
        await transport.aclose()

        allowed_transport = PinnedHttpTransport(
            "https://mcp.internal/mcp",
            resolver=resolver,
            allowed_private_hosts=frozenset({"mcp.internal"}),
        )
        assert await allowed_transport._checked_addresses() == ("10.0.0.2",)
        await allowed_transport.aclose()

    _run(scenario())


def test_dns_rejects_entire_result_if_any_address_is_forbidden() -> None:
    async def scenario() -> None:
        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("8.8.8.8", "127.0.0.1")

        transport = PinnedHttpTransport("https://example.com/mcp", resolver=resolver)
        with pytest.raises(McpEndpointError, match="mcp_dns_address_rejected"):
            await transport._checked_addresses()
        await transport.aclose()

    _run(scenario())


def test_private_allowlist_never_allows_cloud_metadata_addresses() -> None:
    async def scenario() -> None:
        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("::ffff:169.254.169.254",)

        transport = PinnedHttpTransport(
            "https://mcp.internal/mcp",
            resolver=resolver,
            allowed_private_hosts=frozenset({"mcp.internal"}),
        )
        with pytest.raises(McpEndpointError, match="mcp_dns_address_rejected"):
            await transport._checked_addresses()
        await transport.aclose()

    _run(scenario())


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "169.254.20.10",
        "224.0.0.1",
        "255.255.255.255",
        "0.0.0.0",
        "240.0.0.1",
        "::",
        "::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "f000::1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",
        "2002:a9fe:a9fe::1",
    ],
)
def test_private_host_allowlist_does_not_allow_forbidden_ip_ranges(address: str) -> None:
    assert not _is_allowed_address(address, allow_private=True)


def test_ip_policy_allows_public_addresses_and_allowlisted_private_addresses() -> None:
    assert _is_allowed_address("8.8.8.8", allow_private=False)
    assert _is_allowed_address("2606:4700:4700::1111", allow_private=False)
    assert _is_allowed_address("10.20.30.40", allow_private=True)
    assert _is_allowed_address("fd12:3456:789a::1", allow_private=True)


def test_request_uses_checked_ip_and_original_host_and_sni_without_resolving_again() -> None:
    async def scenario() -> None:
        resolver_calls = 0
        inner = _RecordingTransport()

        async def resolver(_: str, __: int) -> Iterable[str]:
            nonlocal resolver_calls
            resolver_calls += 1
            return ("8.8.8.8",)

        client = httpx2.AsyncClient(
            transport=PinnedHttpTransport(
                "https://example.com/mcp",
                resolver=resolver,
                inner_transport=inner,
            )
        )
        async with client:
            await client.post("https://example.com/mcp", content=b"one")
            await client.post("https://example.com/mcp", content=b"two")
        request = inner.requests[0]
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "example.com"
        assert request.headers["accept-encoding"] == "identity"
        assert request.extensions["sni_hostname"] == "example.com"
        assert resolver_calls == 1

    _run(scenario())


@pytest.mark.parametrize("content_encoding", ["gzip", ""])
def test_http_rejects_non_identity_encoding_before_reading_body(content_encoding: str) -> None:
    async def scenario() -> None:
        compressed_expansion = gzip.compress(b"x" * (4 * 1024 * 1024))
        assert len(gzip.decompress(compressed_expansion)) > DEFAULT_MAX_RESPONSE_BYTES
        response_stream = _UnconsumedStream(compressed_expansion)

        class EncodedResponseTransport(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
                return httpx2.Response(
                    200,
                    headers={"content-encoding": content_encoding},
                    stream=response_stream,
                    request=request,
                )

        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("8.8.8.8",)

        client = httpx2.AsyncClient(
            transport=PinnedHttpTransport(
                "https://example.com/mcp",
                resolver=resolver,
                inner_transport=EncodedResponseTransport(),
            )
        )
        async with client:
            with pytest.raises(McpEndpointError, match="mcp_content_encoding_rejected"):
                await client.get("https://example.com/mcp")
        assert response_stream.closed
        assert not response_stream.iterated

    _run(scenario())


def test_local_tls_uses_custom_ca_and_validates_original_hostname(tmp_path: Any) -> None:
    async def scenario() -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp.test")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("mcp.test")]), critical=False)
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
        seen_sni: list[str | None] = []
        server_context.set_servername_callback(
            lambda _socket, server_name, _context: seen_sni.append(server_name)
        )
        seen_host: list[str] = []

        async def serve_connection(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            while (header_line := await reader.readline()) not in {b"\r\n", b"\n", b""}:
                name, _, value = header_line.decode().partition(":")
                if name.lower() == "host":
                    seen_host.append(value.strip())
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
            inner = httpx2.AsyncHTTPTransport(verify=client_context, trust_env=False)

            async def resolver(_: str, __: int) -> Iterable[str]:
                return ("127.0.0.1",)

            client = httpx2.AsyncClient(
                transport=PinnedHttpTransport(
                    f"https://mcp.test:{port}/mcp",
                    allowed_private_hosts=frozenset({"mcp.test"}),
                    resolver=resolver,
                    inner_transport=inner,
                )
            )
            async with client:
                response = await client.get(f"https://mcp.test:{port}/mcp")
                assert response.status_code == 200
            slow_client = httpx2.AsyncClient(
                transport=PinnedHttpTransport(
                    f"https://mcp.test:{port}/slow",
                    allowed_private_hosts=frozenset({"mcp.test"}),
                    resolver=resolver,
                    inner_transport=httpx2.AsyncHTTPTransport(
                        verify=client_context,
                        trust_env=False,
                    ),
                ),
                timeout=httpx2.Timeout(0.05),
            )
            async with slow_client:
                with pytest.raises(httpx2.ReadTimeout):
                    await slow_client.get(f"https://mcp.test:{port}/slow")
        assert seen_sni == ["mcp.test", "mcp.test"]
        assert seen_host == [f"mcp.test:{port}", f"mcp.test:{port}"]

    _run(scenario())


@pytest.mark.parametrize("status_code", [301, 302, 307, 308])
def test_redirects_fail_before_a_second_request(status_code: int) -> None:
    async def scenario() -> None:
        inner = _RecordingTransport(status_code=status_code)

        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("8.8.8.8",)

        client = httpx2.AsyncClient(
            transport=PinnedHttpTransport(
                "https://example.com/mcp",
                resolver=resolver,
                inner_transport=inner,
            )
        )
        async with client:
            with pytest.raises(McpRedirectRejectedError):
                await client.get("https://example.com/mcp")
        assert len(inner.requests) == 1

    _run(scenario())


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_errors_are_not_mcp_errors_and_do_not_retry(status_code: int) -> None:
    async def scenario() -> None:
        inner = _RecordingTransport(status_code=status_code)

        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("8.8.8.8",)

        client = build_pinned_http_client(
            "https://example.com/mcp",
            resolver=resolver,
            inner_transport=inner,
        )
        transport = streamable_http_client(
            "https://example.com/mcp",
            http_client=client,
        )
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

        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("8.8.8.8",)

        client = build_pinned_http_client(
            "https://example.com/mcp",
            resolver=resolver,
            inner_transport=peer,
        )
        async with client:
            transport = streamable_http_client(
                "https://example.com/mcp",
                http_client=client,
            )
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


def test_http_response_stream_limit_is_enforced() -> None:
    async def scenario() -> None:
        inner = _RecordingTransport(body=b"x" * 20)

        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("8.8.8.8",)

        client = httpx2.AsyncClient(
            transport=PinnedHttpTransport(
                "https://example.com/mcp",
                resolver=resolver,
                maximum_response_bytes=10,
                inner_transport=inner,
            )
        )
        async with client:
            with pytest.raises(McpResponseTooLargeError):
                await client.get("https://example.com/mcp")

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

        async def resolver(_: str, __: int) -> Iterable[str]:
            return ("8.8.8.8",)

        client = httpx2.AsyncClient(
            transport=PinnedHttpTransport(
                "https://example.com/mcp",
                resolver=resolver,
                inner_transport=StreamTransport(),
            )
        )
        async with client:
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


def test_stdio_uses_only_explicit_environment_and_argv_without_shell() -> None:
    async def scenario() -> None:
        command = [
            sys.executable,
            "-c",
            "import json, os, sys; result = {'jsonrpc': '2.0', 'id': 1, 'result': {"
            "'env': os.getenv('ALLOWED', '') + '|' + os.getenv('LEAK', ''), "
            "'arg': sys.argv[1]}}; print(json.dumps(result))",
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
            assert message.message.result["env"] == "yes|"
            assert message.message.result["arg"] == "$(touch /tmp/athena-mcp-shell-marker)"
        finally:
            await transport.__aexit__(None, None, None)

    _run(scenario())


def test_stdio_reports_nonzero_process_exit() -> None:
    async def scenario() -> None:
        transport = McpStdioTransport(
            sys.executable,
            ("-c", "raise SystemExit(7)"),
            base_environment={"PATH": os.defpath},
            grace_seconds=0.05,
        )
        await transport.__aenter__()
        await asyncio.sleep(0.05)
        with pytest.raises(ExceptionGroup) as error:
            await transport.__aexit__(None, None, None)
        assert isinstance(_exception_leaf(error.value), McpStdioProcessError)

    _run(scenario())


def test_stdio_reports_unconfirmed_process_cleanup() -> None:
    class _HangingProcess:
        pid = 123456789
        returncode = None
        stdin = None

        async def wait(self) -> int:
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        transport = McpStdioTransport(
            sys.executable,
            ("-c", "pass"),
            base_environment={"PATH": os.defpath},
            grace_seconds=0.01,
            term_seconds=0.01,
            kill_seconds=0.01,
        )
        with pytest.raises(McpStdioCleanupError, match="mcp_stdio_cleanup_failed"):
            await transport._shutdown(cast(anyio.abc.Process, _HangingProcess()))

    _run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group signaling required")
@pytest.mark.parametrize("ignore_term", [False, True])
def test_stdio_closes_stdin_and_reaps_terminated_process_group(
    tmp_path: Any,
    ignore_term: bool,
) -> None:
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
            grace_seconds=0.05,
            term_seconds=0.2,
            kill_seconds=0.2,
        )
        await transport.__aenter__()
        for _ in range(50):
            if pid_path.exists():
                break
            await asyncio.sleep(0.01)
        process_id = int(pid_path.read_text())
        await transport.__aexit__(None, None, None)
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, signal.SIGCONT)

    _run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal exit codes required")
@pytest.mark.parametrize("external_signal", [signal.SIGTERM, signal.SIGKILL])
def test_stdio_external_signal_exit_during_discovery_is_an_error(
    tmp_path: Any,
    external_signal: signal.Signals,
) -> None:
    async def scenario() -> None:
        pid_path = tmp_path / "externally-signalled-child.pid"
        descendant_pid_path = tmp_path / "externally-signalled-descendant.pid"
        script = (
            "import os,signal,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c',"
            f"{
                (
                    'import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                    'open(' + repr(str(descendant_pid_path)) + ",'w').write(str(os.getpid())); "
                    'time.sleep(20)'
                )!r
            }], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            f"open({str(pid_path)!r},'w').write(str(os.getpid())); time.sleep(20)"
        )
        transport = McpStdioTransport(
            sys.executable,
            ("-c", script),
            base_environment={"PATH": os.defpath},
            grace_seconds=0.2,
            term_seconds=0.2,
            kill_seconds=0.2,
        )
        await transport.__aenter__()
        for _ in range(50):
            if pid_path.exists() and descendant_pid_path.exists():
                break
            await asyncio.sleep(0.01)
        process_id = int(pid_path.read_text())
        descendant_id = int(descendant_pid_path.read_text())
        os.kill(process_id, external_signal)
        with pytest.raises(ExceptionGroup) as error:
            await transport.__aexit__(None, None, None)
        assert isinstance(_exception_leaf(error.value), McpStdioProcessError)
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_id, signal.SIGCONT)

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
            grace_seconds=0.05,
            term_seconds=0.2,
            kill_seconds=0.2,
        )
        with anyio.move_on_after(0.1) as cancellation_scope:
            async with transport:
                for _ in range(50):
                    if pid_path.exists():
                        break
                    await asyncio.sleep(0.01)
                await anyio.sleep_forever()
        assert cancellation_scope.cancelled_caught
        process_id = int(pid_path.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, signal.SIGCONT)

    _run(scenario())
