"""Tools node — executes tool calls requested by the LLM.

Each invocation handles one *batch* of tool calls (the LLM may request
multiple tools in a single response, and they can run in parallel).
The pipeline for each tool call:

1. Harness pre-check — security rules evaluation
2. Risk-based confirmation via ``interrupt()`` (human-in-the-loop)
3. MCP tool call via ``MCPClient.call_tool()``
4. Return results as ``ToolMessage`` objects back to the agent
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langgraph.types import RunnableConfig, interrupt
from langchain_core.messages import ToolMessage

from athena.core.graph.agent_state import AgentState
from athena.logging_config import bind_context, get_logger

if TYPE_CHECKING:
    from athena.core.harness import HarnessEngine
from athena.mcp_client.client import MCPClient
from athena.mcp_client.registry import ToolRegistry

logger = get_logger(__name__)


async def tools_node(
    state: AgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Execute pending tool calls from the LLM.

    Reads ``state["pending_tool_calls"]``, executes each one through the
    harness → confirm → MCP pipeline, and returns ``ToolMessage`` results.

    Multiple tool calls in a single batch execute sequentially within this
    node (harness checks are order-sensitive).  True cross-tool parallelism
    could be added later with LangGraph's ``Send()`` fan-out.
    """
    pending = state.get("pending_tool_calls")
    if not pending:
        logger.warning("tools_node_called_without_pending_calls")
        return {"pending_tool_calls": None}

    cfg = config["configurable"]
    harness : HarnessEngine = cfg["harness_engine"]
    mcp_client : MCPClient = cfg["mcp_client"]
    tool_registry : ToolRegistry = cfg.get("tool_registry")
    
    log = bind_context(session_id=state.get("session_id", ""))

    tool_messages: list[ToolMessage] = []

    for tc in pending:
        tool_name = tc.get("name", "")
        tool_args = tc.get("arguments", {})
        tool_call_id = tc.get("id", "")

        # Ensure arguments is a dict (may come as string from some providers)
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except json.JSONDecodeError:
                logger.error("tools_node_invalid_args", tool=tool_name, args=tool_args)
                tool_args = {}

        log.info("executing_tool", tool=tool_name, tool_call_id=tool_call_id)

        # ── 1. Resolve server_id ─────────────────────────────────────────
        tool = tool_registry.get_tool_by_name(tool_name) if tool_registry else None
        server_id = tool.source_server_id if tool else "builtin-core"

        # ── 2. Harness pre-check ─────────────────────────────────────────
        try:
            
            harness_result = await harness.pre_check(state,tool_name, tool_args)
        except Exception as e:
            log.warning("harness_check_exception", error=str(e))
            tool_messages.append(ToolMessage(
                content=json.dumps({"error": f"Security check failed: {e}"}),
                tool_call_id=tool_call_id,
                name=tool_name,
            ))
            continue

        if not harness_result.allowed:
            log.warning("harness_blocked", tool=tool_name, reason=harness_result.reason)
            tool_messages.append(ToolMessage(
                content=json.dumps({"error": f"Operation blocked: {harness_result.reason}"}),
                tool_call_id=tool_call_id,
                name=tool_name,
            ))
            continue

        # ── 3. Confirmation (human-in-the-loop) ───────────────────────────
        if harness_result.requires_confirmation:
            decision = interrupt({
                "type": "confirmation_required",
                "tool_name": tool_name,
                "args": tool_args,
                "risk_level": harness_result.risk_level.value,
                "cooling_off_seconds": harness_result.cooling_off_seconds,
                "reason": getattr(harness_result, "reason", ""),
            })

            if decision != "approved":
                log.info("user_rejected", tool=tool_name)
                tool_messages.append(ToolMessage(
                    content=json.dumps({"error": "User rejected the operation"}),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                ))
                continue

        # ── 4. MCP tool call ──────────────────────────────────────────────
        try:
            result = await mcp_client.call_tool(
                server_id=server_id,
                tool_name=tool_name,
                arguments=tool_args,
            )

            if result.success:
                # Serialize the content for the LLM
                content_str = _serialize_result(result.content)
                log.info("tool_success", tool=tool_name)
                tool_messages.append(ToolMessage(
                    content=content_str,
                    tool_call_id=tool_call_id,
                    name=tool_name,
                ))
            else:
                error_msg = result.error or "Tool returned failure"
                log.warning("tool_failed", tool=tool_name, error=error_msg)
                tool_messages.append(ToolMessage(
                    content=json.dumps({"error": error_msg}),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                ))

        except Exception as e:
            log.error("tool_exception", tool=tool_name, error=str(e))
            tool_messages.append(ToolMessage(
                content=json.dumps({"error": str(e)}),
                tool_call_id=tool_call_id,
                name=tool_name,
            ))

    return {
        "messages": tool_messages,
        "pending_tool_calls": None,
    }


def _serialize_result(content: Any) -> str:
    """Serialize a tool result for the LLM to read.

    The LLM needs a string representation of the result.  We JSON-encode
    structured data and pass plain text through unchanged.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False, indent=2)
    return str(content)
