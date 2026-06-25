"""Weather MCP stdio server entry point.

Run as: python -m athena.tools.weather_server

Provides weather query tools for mainland China, Hong Kong, Macau, and Taiwan.
Uses wttr.in as the free weather data source.
Uses JSON-RPC over stdin/stdout for communication with the MCP client.
<<<<<<< HEAD

IMPORTANT: stdout is the JSON-RPC channel — nothing else may write to it.
All logging is redirected to stderr to avoid corrupting the line protocol.
=======
>>>>>>> 6b79d02412b262c340cef98b966f8457e324cd91
"""

from __future__ import annotations

import json
<<<<<<< HEAD
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

from athena.tools.weather import query_weather  # noqa: E402
from athena.logging_config import get_logger  # noqa: E402
=======
import sys
from typing import Any

from athena.tools.weather import query_weather
from athena.logging_config import get_logger
>>>>>>> 6b79d02412b262c340cef98b966f8457e324cd91

logger = get_logger(__name__)

# ── Tool registry ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "query_weather",
        "description": (
            "查询指定城市的天气信息，支持中国大陆及港澳台城市。"
            "可查询今日实时天气和未来3天预报（不支持历史天气）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": (
                        "城市名称，支持中文（如：北京、上海、香港、台北、深圳）"
                        "或英文/拼音（如：Beijing, Shanghai, Hong+Kong）"
                    ),
                },
                "date": {
                    "type": "string",
                    "description": (
                        "日期，格式 YYYY-MM-DD。不填默认查询今天。"
                        "仅支持今天及未来3天，历史日期暂不支持。"
                    ),
                },
            },
            "required": ["city"],
        },
        "supports_preview": False,
        "idempotent": True,
        "risk_level": "low",
        "capability_tags": ["weather", "information", "text_retrieval"],
    },
]

HANDLERS = {
    "query_weather": query_weather,
}


# ── JSON-RPC server ───────────────────────────────────────────────────────

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
                "serverInfo": {"name": "athena-weather", "version": "0.1.0"},
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
        arguments.pop("_preview", False)  # Not used by weather tool

        handler = HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
            }

        try:
            result = await handler(**arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ]
                },
            }
        except Exception as e:
            logger.error("weather_tool_error", tool=tool_name, error=str(e))
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
