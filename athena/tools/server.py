"""Built-in MCP stdio server entry point.

Run as: python -m athena.tools.server

Provides Athena's built-in tools (filesystem, web search) as MCP tools.
Uses JSON-RPC over stdin/stdout for communication with the MCP client.

IMPORTANT: stdout is the JSON-RPC channel — nothing else may write to it.
All logging is redirected to stderr to avoid corrupting the line protocol.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

# ── CRITICAL: Redirect all logging to stderr BEFORE any other imports ──
logging.basicConfig(
    format="%(message)s",
    stream=sys.stderr,
    level=logging.INFO,
)

import structlog  # noqa: E402

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

from athena.tools.filesystem import file_read, file_write, file_delete, file_search  # noqa: E402
from athena.tools.web_search import web_search  # noqa: E402
from athena.logging_config import get_logger  # noqa: E402

logger = get_logger(__name__)

# ── Tool registry ─────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "file_read",
        "description": "Read the contents of a file within /workspace/",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to /workspace/"},
            },
            "required": ["path"],
        },
        "supports_preview": False,
        "idempotent": True,
        "risk_level": "low",
        "capability_tags": ["filesystem", "read"],
    },
    {
        "name": "file_write",
        "description": "Write content to a file within /workspace/",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to /workspace/"},
                "content": {"type": "string", "description": "File content"},
            },
            "required": ["path", "content"],
        },
        "supports_preview": True,
        "idempotent": True,
        "risk_level": "medium",
        "capability_tags": ["filesystem", "write"],
    },
    {
        "name": "file_delete",
        "description": "Delete a file or directory within /workspace/",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to delete"},
            },
            "required": ["path"],
        },
        "supports_preview": True,
        "idempotent": True,
        "risk_level": "high",
        "capability_tags": ["filesystem", "delete"],
    },
    {
        "name": "file_search",
        "description": "Search for files within /workspace/ by name pattern or content",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern for name search (e.g. *.py), or search string for content search"},
                "path": {"type": "string", "default": ".", "description": "Base directory to search from"},
                "recursive": {"type": "boolean", "default": True, "description": "Search subdirectories recursively"},
                "match_type": {"type": "string", "enum": ["name", "content"], "default": "name", "description": "Match by filename glob or search inside file contents"},
                "max_results": {"type": "integer", "default": 50, "maximum": 200, "description": "Maximum number of results"},
            },
            "required": ["pattern"],
        },
        "supports_preview": True,
        "idempotent": True,
        "risk_level": "low",
        "capability_tags": ["filesystem", "read", "search"],
    },
    {
        "name": "web_search",
        "description": "Search the web for information",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 5, "maximum": 10},
            },
            "required": ["query"],
        },
        "supports_preview": False,
        "idempotent": True,
        "risk_level": "low",
        "capability_tags": ["web_search", "text_retrieval"],
    },
]

HANDLERS = {
    "file_read": file_read,
    "file_write": file_write,
    "file_delete": file_delete,
    "file_search": file_search,
    "web_search": web_search,
}


# ── JSON-RPC server ───────────────────────────────────────────────────

async def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Process a JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "athena-builtin-tools", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        }

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        preview = arguments.pop("_preview", False)

        handler = HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
            }

        try:
            result = await handler(preview=preview, **arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]},
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": f"Tool execution error: {e}"},
            }

    elif method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {},
        }

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def handle_notification(notification: dict[str, Any]) -> None:
    """Process a JSON-RPC notification (no response expected)."""
    method = notification.get("method", "")

    if method == "notifications/initialized":
        pass  # Client is ready
    elif method == "notifications/cancelled":
        pass  # Request cancelled


async def run_server():
    """Main stdio JSON-RPC loop."""
    import asyncio

    # Flush any import-time garbage that may have landed on stdout
    sys.stdout.flush()

    loop = asyncio.get_event_loop()

    # Read from stdin
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    # Write to stdout
    writer_transport, writer_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)

    async for line in reader:
        line = line.decode("utf-8").strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
            if "id" in msg:
                # Request
                response = await handle_request(msg)
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
            else:
                # Notification
                handle_notification(msg)
        except json.JSONDecodeError:
            pass


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_server())
