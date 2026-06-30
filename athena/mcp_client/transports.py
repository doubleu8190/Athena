"""MCP Transport layer.

Supports three transport types:
- stdio: Subprocess-based, used for built-in tools and skill containers
- http: Direct HTTP calls to remote MCP servers
- sse: Server-Sent Events transport for streaming MCP servers
"""

from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from typing import Any

from athena.logging_config import get_logger

logger = get_logger(__name__)


class MCPTransport(ABC):
    """Abstract transport for MCP JSON-RPC communication."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the transport connection."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the transport connection."""
        ...

    @abstractmethod
    async def send_request(self, request: dict) -> dict:
        """Send a JSON-RPC request and return the response."""
        ...

    @abstractmethod
    async def send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        ...


class StdioTransport(MCPTransport):
    """Transport over a subprocess's stdin/stdout.

    Used for built-in tools (python -m athena.tools.server) and
    skill containers (docker exec / docker attach).
    """

    def __init__(self, command: str | list[str], env: dict[str, str] | None = None):
        self.command = command if isinstance(command, list) else command.split()
        self._extra_env = env or {}
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Start the subprocess and begin logging its stderr."""
        env = os.environ.copy()
        if self._extra_env:
            env.update(self._extra_env)
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        # Read stderr line-by-line in the background so subprocess errors
        # (crashes, npx download failures, stack traces) are visible in logs.
        if self._process.stderr:
            self._stderr_task = asyncio.create_task(
                self._log_stderr(self._process.stderr)
            )

    async def _log_stderr(self, stream: asyncio.StreamReader) -> None:
        """Read stderr lines and log them at WARNING level."""
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded:
                    logger.warning(
                        "mcp_subprocess_stderr",
                        command=self.command[0],
                        stderr_line=decoded,
                    )
        except Exception:
            pass

    async def disconnect(self) -> None:
        """Terminate the subprocess."""
        # Cancel the stderr reader task first
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None

        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
            self._process = None

    async def send_request(self, request: dict) -> dict:
        """Send a JSON-RPC request over stdin and read response from stdout.

        Skips non-JSON and non-JSON-RPC lines that may appear on stdout due to
        library initialization noise (torch, chromadb, sentence-transformers, etc.).
        Gives up after MAX_SKIP_LINES consecutive unparseable lines.
        """
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError("StdioTransport not connected")

        MAX_SKIP_LINES = 10

        async with self._lock:
            payload = json.dumps(request) + "\n"
            self._process.stdin.write(payload.encode("utf-8"))
            await self._process.stdin.drain()

            skipped = 0
            while True:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(), timeout=120.0
                )
                if not line:
                    raise ConnectionError("StdioTransport: no response from subprocess")

                decoded = line.decode("utf-8").strip()
                if not decoded:
                    skipped += 1
                    if skipped > MAX_SKIP_LINES:
                        raise ConnectionError(
                            "StdioTransport: too many empty lines from subprocess"
                        )
                    continue

                try:
                    parsed = json.loads(decoded)
                except json.JSONDecodeError:
                    skipped += 1
                    if skipped > MAX_SKIP_LINES:
                        raise ConnectionError(
                            f"StdioTransport: too many unparseable lines "
                            f"(last: {decoded[:200]!r})"
                        )
                    continue

                # Accept any JSON-RPC message (response or notification)
                if isinstance(parsed, dict) and "jsonrpc" in parsed:
                    return parsed

                # Not a JSON-RPC message — probably library noise on stdout
                skipped += 1
                if skipped > MAX_SKIP_LINES:
                    raise ConnectionError(
                        f"StdioTransport: too many non-JSON-RPC lines "
                        f"(last: {decoded[:200]!r})"
                    )
                continue

    async def send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification over stdin."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("StdioTransport not connected")

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        async with self._lock:
            payload = json.dumps(notification) + "\n"
            self._process.stdin.write(payload.encode("utf-8"))
            await self._process.stdin.drain()


class HttpTransport(MCPTransport):
    """Transport over HTTP to a remote MCP server.

    Uses httpx for async HTTP calls. Suitable for external MCP servers.
    """

    def __init__(self, url: str, auth_config: dict | None = None):
        self.url = url.rstrip("/")
        self.auth_config = auth_config or {}
        self._client = None

    async def connect(self) -> None:
        """Initialize the HTTP client."""
        import httpx
        headers = {}
        auth_type = self.auth_config.get("auth_type", "none")

        if auth_type == "static":
            token_env = self.auth_config.get("auth_token_env", "")
            token = os.environ.get(token_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "oauth2_client":
            encrypted_token = self.auth_config.get("encrypted_token", "")
            if encrypted_token:
                # Decrypt token (delegated to a helper in production)
                from athena.mcp_client.auth import decrypt_token
                token = decrypt_token(encrypted_token)
                headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.AsyncClient(
            base_url=self.url,
            headers=headers,
            timeout=httpx.Timeout(120.0),
        )

    async def disconnect(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send_request(self, request: dict) -> dict:
        """Send a JSON-RPC request via HTTP POST."""
        if not self._client:
            raise RuntimeError("HttpTransport not connected")
        response = await self._client.post("/mcp", json=request)
        response.raise_for_status()
        return response.json()

    async def send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification via HTTP POST."""
        if not self._client:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._client.post("/mcp", json=notification)


class SSETransport(MCPTransport):
    """Transport over Server-Sent Events.

    Uses httpx-sse or similar for streaming connections.
    """

    def __init__(self, url: str, auth_config: dict | None = None):
        self.url = url.rstrip("/")
        self.auth_config = auth_config or {}
        self._client = None

    async def connect(self) -> None:
        """Initialize the SSE client."""
        import httpx
        headers = {"Accept": "text/event-stream"}
        auth_type = self.auth_config.get("auth_type", "none")

        if auth_type == "static":
            token_env = self.auth_config.get("auth_token_env", "")
            token = os.environ.get(token_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.AsyncClient(
            base_url=self.url,
            headers=headers,
            timeout=httpx.Timeout(300.0),
        )

    async def disconnect(self) -> None:
        """Close the SSE client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send_request(self, request: dict) -> dict:
        """Send a JSON-RPC request via HTTP POST (SSE endpoints use POST for requests)."""
        if not self._client:
            raise RuntimeError("SSETransport not connected")
        response = await self._client.post("/mcp", json=request)
        response.raise_for_status()
        return response.json()

    async def send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification via HTTP POST."""
        if not self._client:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._client.post("/mcp", json=notification)


class TransportFactory:
    """Factory for creating transport instances based on type."""

    @staticmethod
    def create(transport_type: str, connection_config: dict) -> MCPTransport:
        """Create a transport instance.

        Args:
            transport_type: 'stdio', 'http', or 'sse'.
            connection_config: Transport-specific configuration.

        Returns:
            An MCPTransport instance.
        """
        if transport_type == "stdio":
            command = connection_config.get("command", "")
            args = connection_config.get("args", [])
            env = connection_config.get("env", None)

            if args:
                # Claude Desktop format: separate command + args list
                cmd_list = [command] + args
            elif isinstance(command, list):
                cmd_list = command
            else:
                # Legacy format: single space-separated command string
                cmd_list = command.split() if command else []

            return StdioTransport(cmd_list, env)
        elif transport_type == "http":
            url = connection_config.get("url", "")
            auth = {k: v for k, v in connection_config.items() if k != "url"}
            return HttpTransport(url, auth)
        elif transport_type == "sse":
            url = connection_config.get("url", "")
            auth = {k: v for k, v in connection_config.items() if k != "url"}
            return SSETransport(url, auth)
        else:
            raise ValueError(f"Unknown transport type: {transport_type}")
