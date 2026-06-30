"""RAG MCP stdio server entry point — powered by FastMCP.

Run as: python -m athena.tools.rag_server

Provides semantic search, vector upsert, and vector delete tools for
user memory management. Wraps athena.core.rag.RAGManager as MCP tools.

FastMCP automatically handles:
- JSON-RPC 2.0 framing over stdin/stdout
- tools/list with auto-generated JSON Schema from type hints
- tools/call with Pydantic parameter validation
- Logging to stderr (stdout is the clean JSON-RPC channel)
"""

from __future__ import annotations

# ── CRITICAL: Redirect all logging to stderr BEFORE other imports ────────
# The stdio JSON-RPC protocol uses stdout for messages.  Any log output on
# stdout corrupts the line protocol.  FastMCP logs to stderr internally,
# but application-level structlog/logging needs to be pointed at stderr too.
# RAGManager initialization logs config warnings and collection status.
import logging
import sys

logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)

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

from typing import Any  # noqa: E402

from fastmcp import FastMCP  # noqa: E402

from athena.core.rag import get_rag_manager  # noqa: E402

mcp = FastMCP("Athena RAG", version="0.1.0")


# ── Tool wrappers ────────────────────────────────────────────────────────


@mcp.tool(
    name="semantic_search",
    description="Search user memories by semantic similarity using vector embeddings",
)
async def semantic_search(
    user_id: str,
    query: str,
    top_k: int = 5,
) -> dict[str, Any]:
    rag = get_rag_manager()
    results = await rag.semantic_search(user_id=user_id, query=query, top_k=top_k)
    return {"results": results, "count": len(results)}


@mcp.tool(
    name="upsert_vector",
    description="Create or update a memory's vector embedding in the vector store",
)
async def upsert_vector(
    memory_id: str,
    user_id: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rag = get_rag_manager()
    vector_id = await rag.upsert_vector(
        memory_id=memory_id,
        user_id=user_id,
        text=text,
        metadata=metadata or {},
    )
    return {"vector_id": vector_id, "status": "upserted"}


@mcp.tool(
    name="delete_vector",
    description="Delete a memory's vector embedding from the vector store",
)
async def delete_vector(
    vector_id: str,
) -> dict[str, Any]:
    rag = get_rag_manager()
    rag.delete_vector(vector_id)
    return {"vector_id": vector_id, "status": "deleted"}


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
