"""Built-in MCP stdio server — powered by FastMCP.

Run as: python -m athena.tools.server

Provides Athena's built-in tools (filesystem, web search) as MCP tools.
Uses FastMCP for protocol handling, schema generation, and transport.

FastMCP automatically handles:
- JSON-RPC 2.0 framing over stdin/stdout
- tools/list with auto-generated JSON Schema from type hints
- tools/call with Pydantic parameter validation
- Logging to stderr (stdout is the clean JSON-RPC channel)
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from athena.tools.filesystem import (
    file_read as _file_read,
    file_write as _file_write,
    file_delete as _file_delete,
    file_search as _file_search,
)

mcp = FastMCP("Athena Built-in Tools", version="0.2.0")


# ── Tool wrappers ────────────────────────────────────────────────────────
#
# Thin wrappers around the existing tool functions.
# ``exclude_args`` hides internal parameters from the public tool schema
# that the LLM sees.  ``idempotency_key`` is likewise hidden — it is
# injected by the Executor, never by the LLM.


@mcp.tool(
    name="file_read",
    description="Read the contents of a file. Path must be absolute.",
)
async def file_read(path: str) -> dict[str, Any]:
    return await _file_read(path=path)


@mcp.tool(
    name="file_write",
    description="Write content to a file. Path must be absolute. Creates parent directories as needed.",
    exclude_args=["idempotency_key"],
)
async def file_write(
    path: str,
    content: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return await _file_write(
        path=path, content=content,
        idempotency_key=idempotency_key,
    )


@mcp.tool(
    name="file_delete",
    description="Delete a file or directory. Path must be absolute.",
    exclude_args=["idempotency_key"],
)
async def file_delete(
    path: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return await _file_delete(
        path=path,
        idempotency_key=idempotency_key,
    )


@mcp.tool(
    name="file_search",
    description="Search for files by name pattern (glob) or content. Path must be absolute.",
)
async def file_search(
    pattern: str,
    path: str = "/",
    recursive: bool = True,
    match_type: str = "name",
    max_results: int = 50,
) -> dict[str, Any]:
    return await _file_search(
        pattern=pattern,
        path=path,
        recursive=recursive,
        match_type=match_type,
        max_results=max_results,
    )

# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
