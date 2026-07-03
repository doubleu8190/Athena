"""Handle-failure node — applies the subtask's on_failure strategy.

Called after all retries for a subtask have been exhausted.  Routes:
- **abort** — increment circuit breaker, mark step as failed
- **skip** — mark step as skipped (upgrades to abort for critical steps)
- **fallback** — try static fallback_tool, then dynamic resolution via ToolRegistry
"""

from __future__ import annotations

from typing import Any

from langgraph.types import RunnableConfig

from athena.core.graph.state import ExecutionState
from athena.logging_config import bind_context, get_logger

logger = get_logger(__name__)

MAX_FALLBACK_DEPTH = 1


async def handle_failure_node(
    state: ExecutionState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Apply the subtask's on_failure strategy.

    Reads ``subtask_result`` from state (set by ``execute_node``) and
    returns an updated ``subtask_result`` with the final disposition.
    """
    result = state.get("subtask_result", {})
    if not result:
        return {}

    step = result.get("step", 0)
    strategy = result.get("on_failure", "abort")
    critical = result.get("critical", True)
    rendered_args = result.get("rendered_args", {})

    log = bind_context(step=step, strategy=strategy)

    # ── abort ─────────────────────────────────────────────────────────
    if strategy == "abort":
        log.error("subtask_aborted", error=result.get("error", ""))
        return {
            "subtask_result": {**result, "status": "abort"},
            "circuit_breaker_count": state.get("circuit_breaker_count", 0) + 1,
        }

    # ── skip ──────────────────────────────────────────────────────────
    elif strategy == "skip":
        if critical:
            log.error("critical_subtask_cannot_skip")
            return {
                "subtask_result": {**result, "status": "abort"},
                "circuit_breaker_count": state.get("circuit_breaker_count", 0) + 1,
            }
        log.info("subtask_skipped")
        return {
            "subtask_result": {**result, "status": "skipped"},
        }

    # ── fallback ──────────────────────────────────────────────────────
    elif strategy == "fallback":
        cfg = config["configurable"]
        tool_registry = cfg["tool_registry"]
        mcp_client = cfg["mcp_client"]
        original_tool = result.get("tool_name", "")

        # 1. Try static fallback_tool from plan
        fallback_tool = result.get("fallback_tool")
        if fallback_tool and isinstance(fallback_tool, dict):
            fb_name = fallback_tool.get("tool_name")
            fb_server_id = fallback_tool.get("mcp_server_id")
            args_mapping = fallback_tool.get("args_mapping", "passthrough")

            fb_args = rendered_args
            if args_mapping == "remap":
                overrides = fallback_tool.get("args_overrides", {})
                fb_args = {**rendered_args, **overrides}

            log.info("fallback_static_attempt", to_tool=fb_name)

            try:
                server_id = fb_server_id or _resolve_server(
                    tool_registry, fb_name
                )
                fb_result = await mcp_client.call_tool(
                    server_id=server_id,
                    tool_name=fb_name,
                    arguments=fb_args,
                )
                if fb_result.success:
                    log.info("fallback_static_success", to_tool=fb_name)
                    from athena.core.sandbox import normalize_output
                    return {
                        "subtask_result": {
                            **result,
                            "status": "success",  # route to collect as success
                            "output": fb_result.content,
                            "normalized_output": normalize_output(fb_result.content),
                            "tool_name": fb_name,
                            "fallback_from": original_tool,
                        },
                    }
            except Exception as e:
                log.warning("fallback_static_failed", error=str(e))

            # Static fallback failed → return as fallback for dynamic retry
            return {
                "subtask_result": {
                    **result,
                    "status": "fallback",
                    "fallback_from": original_tool,
                },
            }

        # 2. Try dynamic fallback via ToolRegistry
        fallback_tool_registry = tool_registry.resolve_fallback(original_tool)
        if fallback_tool_registry:
            log.info("fallback_dynamic_attempt", to_tool=fallback_tool_registry.name)
            try:
                fb_result = await mcp_client.call_tool(
                    server_id=fallback_tool_registry.source_server_id,
                    tool_name=fallback_tool_registry.name,
                    arguments=rendered_args,
                )
                if fb_result.success:
                    log.info("fallback_dynamic_success")
                    from athena.core.sandbox import normalize_output
                    return {
                        "subtask_result": {
                            **result,
                            "status": "success",
                            "output": fb_result.content,
                            "normalized_output": normalize_output(fb_result.content),
                            "tool_name": fallback_tool_registry.name,
                            "fallback_from": original_tool,
                        },
                    }
            except Exception as e:
                log.warning("fallback_dynamic_failed", error=str(e))

        # 3. Both fallbacks failed → abort
        log.error("fallback_chain_exhausted")
        return {
            "subtask_result": {**result, "status": "abort"},
            "circuit_breaker_count": state.get("circuit_breaker_count", 0) + 1,
        }

    # ── Unknown strategy ──────────────────────────────────────────────
    log.error("unknown_failure_strategy", strategy=strategy)
    return {
        "subtask_result": {**result, "status": "abort"},
        "circuit_breaker_count": state.get("circuit_breaker_count", 0) + 1,
    }


def _resolve_server(tool_registry, tool_name: str) -> str:
    """Resolve the MCP server ID for a tool name."""
    tool = tool_registry.get_tool_by_name(tool_name)
    return tool.source_server_id if tool else "builtin-core"
