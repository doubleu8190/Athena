"""MCP Client — connection lifecycle management.

Handles connecting to all registered MCP servers, heartbeats,
tool discovery (tools/list), and tool invocation (tools/call).

Supports stdio (built-in tools, skill containers), HTTP, and SSE transports.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from athena.config import Config
from athena.logging_config import get_logger
from athena.mcp_client.transports import TransportFactory
from athena.models import get_session, get_session_maker

logger = get_logger(__name__)


@dataclass
class ToolDef:
    """Tool definition returned by tools/list."""
    name: str
    description: str = ""
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    version: str = "1.0.0"
    idempotent: bool = True
    capability_tags: list[str] = field(default_factory=list)
    risk_level: str = "medium"


@dataclass
class ToolResult:
    """Result from a tools/call invocation."""
    server_id: str
    tool_name: str
    success: bool
    content: Any = None
    error: str | None = None


class ServerConnection:
    """Represents a live connection to a single MCP server."""

    def __init__(self, server_id: str, transport_type: str, connection_config: dict):
        self.server_id = server_id
        self.transport_type = transport_type
        self.connection_config = connection_config
        self._transport = TransportFactory.create(transport_type, connection_config)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """Establish the transport connection and perform MCP handshake."""
        try:
            await self._transport.connect()
            # Perform MCP initialize handshake
            result = await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "athena", "version": "0.1.0"},
            })
            if result:
                # Send initialized notification
                await self._transport.send_notification("notifications/initialized", {})
                self._connected = True
                logger.info("mcp_server_connected", server_id=self.server_id)
            return self._connected
        except Exception as e:
            logger.error("mcp_server_connect_failed", server_id=self.server_id, error=str(e))
            return False

    async def disconnect(self) -> None:
        """Close the transport connection."""
        self._connected = False
        try:
            await self._transport.disconnect()
        except Exception:
            pass

    async def list_tools(self) -> list[ToolDef]:
        """Call tools/list and return parsed tool definitions."""
        result = await self._send_request("tools/list", {})
        if not result:
            return []
        tools_data = result.get("tools", [])
        parsed = []
        for t in tools_data:
            schema = t.get("inputSchema", t.get("parameters_schema", {}))
            parsed.append(ToolDef(
                name=t["name"],
                description=t.get("description", ""),
                parameters_schema=schema,
                version=t.get("version", "1.0.0"),
                idempotent=t.get("idempotent", True),
                capability_tags=t.get("capability_tags", []),
                risk_level=t.get("risk_level", "medium"),
            ))
        return parsed

    async def call_tool(self, tool_name: str, arguments: dict) -> ToolResult:
        """Call tools/call and return a standard ToolResult."""
        try:
            result = await self._send_request("tools/call", {
                "name": tool_name,
                "arguments": dict(arguments),
            })
            return ToolResult(
                server_id=self.server_id,
                tool_name=tool_name,
                success=True,
                content=result.get("content", result),
            )
        except Exception as e:
            return ToolResult(
                server_id=self.server_id,
                tool_name=tool_name,
                success=False,
                error=str(e),
            )

    async def ping(self) -> bool:
        """Send a ping to check connectivity."""
        try:
            result = await self._send_request("ping", {})
            return result is not None
        except Exception:
            return False

    async def _send_request(self, method: str, params: dict) -> Any | None:
        """Send a JSON-RPC request and return the result."""
        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        try:
            response = await self._transport.send_request(request)
            if "error" in response:
                logger.warning(
                    "mcp_rpc_error",
                    server_id=self.server_id,
                    method=method,
                    error=response["error"],
                )
                return None
            return response.get("result")
        except Exception as e:
            logger.error(
                "mcp_rpc_exception",
                server_id=self.server_id,
                method=method,
                error=str(e),
            )
            raise


# ── Singleton ───────────────────────────────────────────────────────────────

_mcp_client: MCPClient | None = None


def set_mcp_client(client: MCPClient) -> None:
    """Set the process-wide singleton MCPClient instance.

    Called once during application startup (lifespan). Must be called
    before any call to get_mcp_client().
    """
    global _mcp_client
    _mcp_client = client
    logger.info("mcp_client_singleton_set")


def get_mcp_client() -> MCPClient:
    """Return the process-wide singleton MCPClient instance.

    Raises RuntimeError if not yet initialized. The lifespan must call
    set_mcp_client() during startup before any route handler accesses this.
    """
    if _mcp_client is None:
        raise RuntimeError("MCPClient not initialized — call set_mcp_client() during lifespan startup")
    return _mcp_client


class MCPClient:
    """Manages MCP server connections and tool operations.

    Responsibilities:
    - Connection lifecycle (connect, disconnect, heartbeat, reconnect)
    - Tool discovery: tools/list on connect, periodic refresh
    - Tool invocation: tools/call
    - Stale propagation: immediately marks server tools as stale on disconnect
    """

    def __init__(self, config: Config):
        self.config = config
        from athena.mcp_client.registry import get_tool_registry
        self.registry = get_tool_registry()
        self._connections: dict[str, ServerConnection] = {}
        self._connecting: set[str] = set()  # server_ids currently attempting connection
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the MCP client: connect all registered servers, begin heartbeat."""
        self._running = True
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import select
            from athena.models.mcp_server import MCPServer
            result = await session.execute(
                select(MCPServer).where(MCPServer.enabled == True)
            )
            servers = result.scalars().all()
        logger.info("mcp_client_starting", found_servers=servers)
        for server in servers:
            await self.connect_server(server.server_id)

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("mcp_client_started", server_count=len(self._connections))

    async def stop(self) -> None:
        """Gracefully disconnect all servers."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        for server_id in list(self._connections.keys()):
            await self.disconnect_server(server_id)

        logger.info("mcp_client_stopped")

    async def connect_server(self, server_id: str) -> bool:
        """Connect to a specific MCP server and register its tools.

        Returns True if connection succeeded.
        """
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import select
            from athena.models.mcp_server import MCPServer
            result = await session.execute(
                select(MCPServer).where(MCPServer.server_id == server_id)
            )
            server_record = result.scalar_one_or_none()

        if not server_record:
            logger.error("mcp_server_not_found", server_id=server_id)
            return False

        config_json = json.loads(server_record.connection_config)

        conn = ServerConnection(
            server_id=server_id,
            transport_type=server_record.transport,
            connection_config=config_json,
        )

        self._connecting.add(server_id)
        try:
            if await conn.connect():
                self._connections[server_id] = conn
                self._connecting.discard(server_id)

                # Discover and register tools
                tools = await conn.list_tools()
                source = server_record.source
                await self.registry.register(server_id, tools, source)

                # Update Prometheus metrics
                self._update_metrics()
                return True

            self._connecting.discard(server_id)
            return False
        except Exception:
            self._connecting.discard(server_id)
            raise

    async def disconnect_server(self, server_id: str) -> None:
        """Disconnect a server and mark its tools as stale."""
        self._connecting.discard(server_id)
        conn = self._connections.pop(server_id, None)
        if conn:
            await conn.disconnect()

        # Mark tools as stale
        await self.registry.mark_stale(server_id)

        # Update Prometheus metrics
        self._update_metrics()

    def get_connection_status(self, server_id: str) -> str:
        """Return connection status: 'connected', 'disconnected', or 'connecting'.

        This is a computed (in-memory) status — it is NOT persisted to the database.
        The database only stores whether the server is enabled (admin intent).
        """
        if server_id in self._connecting:
            return "connecting"
        conn = self._connections.get(server_id)
        if conn and conn.connected:
            return "connected"
        return "disconnected"

    def get_all_connection_statuses(self) -> dict[str, str]:
        """Return connection status for all known server IDs."""
        all_ids: set[str] = set(self._connections.keys()) | self._connecting
        # Also include servers the registry knows about
        for tool in self.registry._tools.values():
            all_ids.add(tool.source_server_id)
        return {sid: self.get_connection_status(sid) for sid in all_ids}

    def _update_metrics(self) -> None:
        """Push current connection states to the Prometheus gauge."""
        try:
            from athena.api.metrics import athena_mcp_server_status
            for server_id in self._connections:
                athena_mcp_server_status.labels(server_id=server_id).set(1)
            for server_id in self._connecting:
                athena_mcp_server_status.labels(server_id=server_id).set(0)
        except ImportError:
            pass

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict,
    ) -> ToolResult:
        """Call a tool on a specific server.

        Args:
            server_id: Target MCP server.
            tool_name: Tool to invoke.
            arguments: Tool arguments.
        """
        conn = self._connections.get(server_id)
        if not conn:
            # Attempt reconnect
            if await self.connect_server(server_id):
                conn = self._connections.get(server_id)
            if not conn:
                return ToolResult(
                    server_id=server_id,
                    tool_name=tool_name,
                    success=False,
                    error=f"Server {server_id} not connected",
                )
        logger.info("mcp_call_tool", server_id=server_id, tool_name=tool_name, arguments=arguments)
        return await conn.call_tool(tool_name, arguments)

    async def list_tools(self, server_id: str) -> list[ToolDef]:
        """Get the current tool list from a connected server."""
        conn = self._connections.get(server_id)
        if not conn:
            return []
        return await conn.list_tools()

    async def _heartbeat_loop(self) -> None:
        """Periodic health check and reconnect loop."""
        interval = self.config.system.mcp_heartbeat_interval_seconds
        while self._running:
            await asyncio.sleep(interval)
            for server_id, conn in list(self._connections.items()):
                if not await conn.ping():
                    logger.warning("mcp_heartbeat_failed", server_id=server_id)
                    await self.disconnect_server(server_id)
                    # Attempt reconnect
                    backoff = min(
                        interval, self.config.system.mcp_reconnect_backoff_max_seconds
                    )
                    await asyncio.sleep(backoff)
                    if self._running:
                        if await self.connect_server(server_id):
                            # Refresh tools on reconnect
                            if self.config.system.mcp_tools_list_refresh_on_reconnect:
                                fresh_tools = await self.list_tools(server_id)
                                await self.registry.refresh(server_id, fresh_tools)
