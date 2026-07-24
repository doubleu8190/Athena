"""Tools node — executes a single confirmed tool call from the LLM.

Each invocation handles **one** tool call.  The agent graph uses
``Send()`` to fan out multiple tool calls as independent branches.
Confirmation is handled **before** this node by ``confirm_node``,
so this node only executes already-confirmed tools.

All tool calls flow through ``MCPClient.call_tool()`` — the single
connection pool with heartbeat, reconnect, and state management.
LLM tool-binding (``model.bind_tools()``) uses the same path via
``_MCPClientToolAdapter`` in ``tool_loader.py``.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig

from athena.core.graph.agent_state import AgentState
from athena.core.resilience.llm_decision import LLMDecision
from athena.core.resilience.manager import ResilienceManager
from athena.logging_config import bind_context, get_logger
from athena.mcp_client.client import MCPClient

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

    tc = confirmed[0]
    tool_name = tc.get("name", "")
    tool_args = tc.get("arguments", {})
    tool_call_id = tc.get("id", "")

    cfg = config.get("configurable", {})
    mcp_client: MCPClient | None = cfg.get("mcp_client")
    if mcp_client is None:
        # Fallback to singleton for production; tests should inject via config
        from athena.mcp_client.client import get_mcp_client
        mcp_client = get_mcp_client()

    log = bind_context(session_id=cfg["session_id"], node="tools_node")

    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except json.JSONDecodeError:
            logger.error("tools_node_invalid_args", tool=tool_name, args=tool_args)
            tool_args = {}

    log.info("executing_tool", tool=tool_name, args=tool_args, tool_call_id=tool_call_id)

    # Resolve server_id from MCPClient registry
    tool = mcp_client.get_tool_by_name(tool_name)
    server_id = tool.source_server_id if tool else "builtin-core"

    # ── Single execution path via MCPClient ────────────────────────
    async def tool_func(args: dict) -> Any:
        """Execute tool via MCPClient — the only path."""
        result = await mcp_client.call_tool(
            server_id=server_id,
            tool_name=tool_name,
            arguments=args,
        )
        if result.success:
            return result.content
        raise ToolExecutionError(result.error or "Tool returned failure")

    # ── Resilience-aware path ──────────────────────────────────────
    resilience_manager: ResilienceManager | None = cfg.get("resilience_manager")

    if resilience_manager is not None:
        return await _execute_with_resilience(
            resilience_manager=resilience_manager,
            mcp_client=mcp_client,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=tool_call_id,
            server_id=server_id,
            tool_func=tool_func,
            log=log,
        )

    # ── Simple path (no resilience manager) ────────────────────────
    return await _execute_simple(
        tool_name=tool_name,
        tool_args=tool_args,
        tool_call_id=tool_call_id,
        tool_func=tool_func,
        log=log,
    )


# ── Resilience-aware execution ─────────────────────────────────────


async def _execute_with_resilience(
    resilience_manager: ResilienceManager,
    mcp_client: MCPClient,
    tool_name: str,
    tool_args: dict,
    tool_call_id: str,
    server_id: str,
    tool_func,
    log,
) -> dict[str, Any]:
    """Execute tool using ResilienceManager (retry + circuit breaker + LLM decision)."""

    exec_result = await resilience_manager.execute_tool(
        tool_name=tool_name,
        tool_args=tool_args,
        tool_call_id=tool_call_id,
        tool_func=tool_func,
        server_id=server_id,
    )

    # ── Handle LLM fallback decision ──────────────────────────────
    if not exec_result.success and exec_result.fallback_tool_name and exec_result.decision_used:
        return await _execute_fallback(
            resilience_manager=resilience_manager,
            mcp_client=mcp_client,
            fallback_name=exec_result.fallback_tool_name,
            fallback_decision=exec_result.decision_used,
            original_args=tool_args,
            original_call_id=tool_call_id,
            depth=0,
            log=log,
        )

    if exec_result.success:
        log.info("tool_success", tool=tool_name, via="resilience_manager")
    else:
        log.warning(
            "tool_failed_after_resilience",
            tool=tool_name,
            error=exec_result.error_message,
        )

    return {
        "messages": [
            ToolMessage(
                content=exec_result.to_message_content(),
                tool_call_id=tool_call_id,
                name=tool_name,
            )
        ]
    }


MAX_FALLBACK_DEPTH = 3


async def _execute_fallback(
    resilience_manager: ResilienceManager,
    mcp_client: MCPClient,
    fallback_name: str,
    fallback_decision: LLMDecision,
    original_args: dict,
    original_call_id: str,
    depth: int,
    log,
) -> dict[str, Any]:
    """Execute a fallback tool identified by the LLM decision.

    Supports nested fallbacks up to MAX_FALLBACK_DEPTH. Uses the
    injected tool_registry (not the process-wide singleton) for
    server_id resolution. Prefers decision.adjusted_args when present,
    falls back to original_args when the decision has no adjusted_args.
    """
    if depth >= MAX_FALLBACK_DEPTH:
        log.warning("max_fallback_depth_reached", depth=depth, tool=fallback_name)
        return {
            "messages": [
                ToolMessage(
                    content=json.dumps(
                        {"error": f"Fallback depth limit reached ({MAX_FALLBACK_DEPTH})."}
                    ),
                    tool_call_id=original_call_id,
                    name=fallback_name,
                )
            ]
        }

    log.info("executing_fallback_tool", to_tool=fallback_name, depth=depth)

    fb_tool = mcp_client.get_tool_by_name(fallback_name)
    fb_server_id = fb_tool.source_server_id if fb_tool else "builtin-core"

    # Prefer adjusted_args from the decision; fall back to original_args
    fallback_args = fallback_decision.adjusted_args or original_args

    async def fallback_func(args: dict) -> Any:
        result = await mcp_client.call_tool(
            server_id=fb_server_id,
            tool_name=fallback_name,
            arguments=args,
        )
        if result.success:
            return result.content
        raise ToolExecutionError(result.error or "Fallback tool returned failure")

    fb_result = await resilience_manager.execute_tool(
        tool_name=fallback_name,
        tool_args=fallback_args,
        tool_call_id=original_call_id,
        tool_func=fallback_func,
        server_id=fb_server_id,
    )

    # ── Handle nested fallback (Fix #4) ──────────────────────────
    if not fb_result.success and fb_result.fallback_tool_name and fb_result.decision_used:
        log.info(
            "nested_fallback",
            from_tool=fallback_name,
            to_tool=fb_result.fallback_tool_name,
            depth=depth,
        )
        return await _execute_fallback(
            resilience_manager=resilience_manager,
            mcp_client=mcp_client,
            fallback_name=fb_result.fallback_tool_name,
            fallback_decision=fb_result.decision_used,
            original_args=original_args,
            original_call_id=original_call_id,
            depth=depth + 1,
            log=log,
        )

    if fb_result.success:
        log.info("fallback_tool_success", tool=fallback_name, depth=depth)
    else:
        log.warning(
            "fallback_tool_failed", tool=fallback_name, error=fb_result.error_message, depth=depth
        )

    return {
        "messages": [
            ToolMessage(
                content=fb_result.to_message_content(),
                tool_call_id=original_call_id,
                name=fallback_name,
            )
        ]
    }


# ── Simple execution (no resilience) ───────────────────────────────


async def _execute_simple(
    tool_name: str,
    tool_args: dict,
    tool_call_id: str,
    tool_func,
    log,
) -> dict[str, Any]:
    """Simple tool execution without retry/circuit breaker."""
    try:
        result_content = await tool_func(tool_args)
        content_str = _serialize_result(result_content)
        log.info("tool_success", tool=tool_name, via="mcp_client")
        return {
            "messages": [
                ToolMessage(
                    content=content_str,
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )
            ]
        }
    except Exception as e:
        log.error("tool_exception", tool=tool_name, error=str(e))
        return {
            "messages": [
                ToolMessage(
                    content=json.dumps({"error": str(e)}),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )
            ]
        }


# ── Helpers ────────────────────────────────────────────────────────


class ToolExecutionError(Exception):
    """Raised when a tool returns a non-success result."""

    pass


def _serialize_result(content: Any) -> str:
    """Serialize a tool result for the LLM to read."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False, indent=2)
    return str(content)
