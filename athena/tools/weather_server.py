"""Weather MCP stdio server entry point — powered by FastMCP.

Run as: python -m athena.tools.weather_server

Provides weather query tools for mainland China, Hong Kong, Macau, and Taiwan.
Uses wttr.in as the free weather data source.

FastMCP automatically handles:
- JSON-RPC 2.0 framing over stdin/stdout
- tools/list with auto-generated JSON Schema from type hints
- tools/call with Pydantic parameter validation
- Logging to stderr (stdout is the clean JSON-RPC channel)
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from athena.tools.weather import query_weather as _query_weather

mcp = FastMCP("Athena Weather", version="0.1.0")


@mcp.tool(
    name="query_weather",
    description=(
        "查询指定城市的天气信息，支持中国大陆及港澳台城市。"
        "可查询今日实时天气和未来3天预报（不支持历史天气）。"
    ),
)
async def query_weather(
    city: str,
    date: str | None = None,
) -> dict[str, Any]:
    """Query weather for a city, optionally filtered by date."""
    return await _query_weather(city=city, date=date)


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
