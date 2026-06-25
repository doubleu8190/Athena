"""RAG MCP stdio server entry point.

Run as: python -m athena.tools.rag_server

Provides semantic search, vector upsert, and vector delete tools for
user memory management. Wraps athena.core.rag.RAGManager as MCP tools.

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
# The stdio JSON-RPC protocol uses stdout for messages. Any log output on
# stdout corrupts the line protocol and causes "Extra data" parse errors.
#
# Both stdlib logging AND structlog must be pointed at stderr:
# - stdlib logging: logging.basicConfig(stream=sys.stderr)
# - structlog: structlog.configure() with a stderr-bound logger factory
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

from athena.logging_config import get_logger  # noqa: E402

logger = get_logger(__name__)

# ── Tool registry ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "semantic_search",
        "description": "Search user memories by semantic similarity using vector embeddings",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "The user ID to search memories for",
                },
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Number of results to return",
                },
            },
            "required": ["user_id", "query"],
        },
        "supports_preview": False,
        "idempotent": True,
        "risk_level": "low",
        "capability_tags": ["memory", "semantic_search", "text_retrieval"],
    },
    {
        "name": "upsert_vector",
        "description": "Create or update a memory's vector embedding in the vector store",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Unique memory identifier",
                },
                "user_id": {
                    "type": "string",
                    "description": "The user ID who owns this memory",
                },
                "text": {
                    "type": "string",
                    "description": "The memory text to embed and store",
                },
                "metadata": {
                    "type": "object",
                    "description": "Additional metadata (key, tags, etc.)",
                },
            },
            "required": ["memory_id", "user_id", "text"],
        },
        "supports_preview": False,
        "idempotent": True,
        "risk_level": "low",
        "capability_tags": ["memory", "vector_write"],
    },
    {
        "name": "delete_vector",
        "description": "Delete a memory's vector embedding from the vector store",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vector_id": {
                    "type": "string",
                    "description": "The vector/document ID to delete (same as memory_id)",
                },
            },
            "required": ["vector_id"],
        },
        "supports_preview": False,
        "idempotent": True,
        "risk_level": "low",
        "capability_tags": ["memory", "vector_delete"],
    },
]


from athena.core.rag import get_rag_manager  # noqa: E402


# ── Tool handlers ─────────────────────────────────────────────────────────

async def handle_semantic_search(**kwargs) -> dict:
    """Handler for semantic_search tool."""
    user_id = kwargs["user_id"]
    query = kwargs["query"]
    top_k = kwargs.get("top_k", 5)

    rag = get_rag_manager()
    results = await rag.semantic_search(user_id=user_id, query=query, top_k=top_k)
    return {"results": results, "count": len(results)}


async def handle_upsert_vector(**kwargs) -> dict:
    """Handler for upsert_vector tool."""
    memory_id = kwargs["memory_id"]
    user_id = kwargs["user_id"]
    text = kwargs["text"]
    metadata = kwargs.get("metadata") or {}

    rag = get_rag_manager()
    vector_id = await rag.upsert_vector(
        memory_id=memory_id,
        user_id=user_id,
        text=text,
        metadata=metadata,
    )
    return {"vector_id": vector_id, "status": "upserted"}


async def handle_delete_vector(**kwargs) -> dict:
    """Handler for delete_vector tool."""
    vector_id = kwargs["vector_id"]

    rag = get_rag_manager()
    rag.delete_vector(vector_id)
    return {"vector_id": vector_id, "status": "deleted"}


HANDLERS = {
    "semantic_search": handle_semantic_search,
    "upsert_vector": handle_upsert_vector,
    "delete_vector": handle_delete_vector,
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
                "serverInfo": {"name": "athena-rag-skill", "version": "0.1.0"},
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
        arguments.pop("_preview", False)  # Not used by RAG tools

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
            logger.error("rag_tool_error", tool=tool_name, error=str(e))
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
        pass
    elif method == "notifications/cancelled":
        pass


async def run_server():
    """Main stdio JSON-RPC loop."""
    import asyncio

    # Flush any import-time garbage that may have landed on stdout.
    # Libraries like torch, chromadb, or sentence-transformers can emit
    # initialization messages / progress bars to stdout on first import.
    sys.stdout.flush()

    loop = asyncio.get_event_loop()

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

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
                response = await handle_request(msg)
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
            else:
                handle_notification(msg)
        except json.JSONDecodeError:
            pass


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_server())
