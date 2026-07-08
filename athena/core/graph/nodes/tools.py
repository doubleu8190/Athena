"""Tools node — executes a single confirmed tool call from the LLM.

Each invocation handles **one** tool call.  The agent graph uses
``Send()`` to fan out multiple tool calls as independent branches.
Confirmation is handled **before** this node by ``confirm_node``,
so this node only executes already-confirmed tools.

Pipeline for each tool call:
1. Tool execution via ``BaseTool.ainvoke()`` or ``MCPClient.call_tool()``
2. Return ``ToolMessage`` result back to the agent
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.types import RunnableConfig
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from athena.core.graph.agent_state import AgentState
from athena.logging_config import bind_context, get_logger
from athena.mcp_client.client import MCPClient
from athena.mcp_client.registry import ToolRegistry

logger = get_logger(__name__)


async def tools_node(
    state: AgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Execute a single confirmed tool call.

    Called via ``Send("tools", {"confirmed_tool_calls": [tc]})`` — each
    invocation processes exactly one tool call.  Confirmation is handled
    by ``confirm_node`` before this node runs.
    """
    confirmed = state.get("confirmed_tool_calls")
    if not confirmed:
        logger.warning("tools_node_called_without_confirmed_calls")
        return {}

    # Process the first (and only) tool call in this branch
    tc = confirmed[0]
    tool_name = tc.get("name", "")
    tool_args = tc.get("arguments", {})
    tool_call_id = tc.get("id", "")

    cfg = config["configurable"]
    mcp_client: MCPClient = cfg["mcp_client"]
    tool_registry: ToolRegistry = cfg.get("tool_registry")

    log = bind_context(session_id=cfg["session_id"], node="tools_node")

    # Ensure arguments is a dict (may come as string from some providers)
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except json.JSONDecodeError:
            logger.error("tools_node_invalid_args", tool=tool_name, args=tool_args)
            tool_args = {}

    log.info("executing_tool", tool=tool_name, args=tool_args, tool_call_id=tool_call_id)

    # ── Execute tool ──────────────────────────────────────────────────
    # Try BaseTool.ainvoke() first (from MultiServerMCPClient),
    # fall back to MCPClient.call_tool() for backward compatibility.
    mcp_tools = await _load_mcp_tools_map()

    if tool_name in mcp_tools:
        try:
            result_content = await mcp_tools[tool_name].ainvoke(tool_args)
            content_str = _serialize_result(result_content)
            log.info("tool_success", tool=tool_name, via="base_tool")
            return {"messages": [ToolMessage(
                content=content_str,
                tool_call_id=tool_call_id,
                name=tool_name,
            )]}
        except Exception as e:
            log.error("tool_exception", tool=tool_name, error=str(e))
            return {"messages": [ToolMessage(
                content=json.dumps({"error": str(e)}),
                tool_call_id=tool_call_id,
                name=tool_name,
            )]}

    # Fallback: use MCPClient directly
    tool = tool_registry.get_tool_by_name(tool_name) if tool_registry else None
    server_id = tool.source_server_id if tool else "builtin-core"

    try:
        result = await mcp_client.call_tool(
            server_id=server_id,
            tool_name=tool_name,
            arguments=tool_args,
        )

        if result.success:
            content_str = _serialize_result(result.content)
            log.info("tool_success", tool=tool_name, via="mcp_client")
            return {"messages": [ToolMessage(
                content=content_str,
                tool_call_id=tool_call_id,
                name=tool_name,
            )]}
        else:
            error_msg = result.error or "Tool returned failure"
            log.warning("tool_failed", tool=tool_name, error=error_msg)
            return {"messages": [ToolMessage(
                content=json.dumps({"error": error_msg}),
                tool_call_id=tool_call_id,
                name=tool_name,
            )]}

    except Exception as e:
        log.error("tool_exception", tool=tool_name, error=str(e))
        return {"messages": [ToolMessage(
            content=json.dumps({"error": str(e)}),
            tool_call_id=tool_call_id,
            name=tool_name,
        )]}


async def _load_mcp_tools_map() -> dict[str, BaseTool]:
    """Load MCP tools and return as a name-keyed dict.

    Uses ``load_mcp_base_tools()`` from the tool loader bridge.
    Returns empty dict on failure (tools_node will fall back to MCPClient).
    """
    try:
        from athena.mcp_client.tool_loader import load_mcp_base_tools
        tools = await load_mcp_base_tools()
        return {t.name: t for t in tools}
    except Exception as e:
        logger.warning("mcp_tools_load_failed_for_execution", error=str(e))
        return {}


def _serialize_result(content: Any) -> str:
    """Serialize a tool result for the LLM to read."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False, indent=2)
    return str(content)
