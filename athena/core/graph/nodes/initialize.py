"""Initialize node — seeds the graph state from configurable context.

This is the first node after START.  It reads session identifiers from
``config["configurable"]`` and resets all execution-tracking fields.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import RunnableConfig

from athena.core.graph.state import ExecutionState
from athena.logging_config import get_logger

logger = get_logger(__name__)


async def initialize_node(
    state: ExecutionState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Seed the initial execution state from configurable.

    The ``config["configurable"]`` dict carries the live objects
    (SessionContext, ContextManager, LLMProviderManager, MCPClient,
    HarnessEngine, ToolRegistry) — they are NOT put into state because
    LangGraph checkpointing requires JSON-serializable values.

    Returns a partial state update with zeroed tracking fields.
    """
    cfg = config.get("configurable", {})
    session_ctx = cfg.get("session_context")
    user_id = cfg.get("user_id", "")
    channel = cfg.get("channel", "web")

    if session_ctx:
        user_id = session_ctx.user_id or user_id
        channel = session_ctx.channel or channel

    logger.info(
        "graph_initialized",
        session_id=cfg.get("session_id", ""),
        user_id=user_id,
        channel=channel,
    )

    return {
        "task_plan": None,
        "plan_error": None,
        "step_outputs": {},
        "completed_steps": [],
        "failed_steps": [],
        "subtask_result": None,
        "circuit_breaker_count": 0,
        "retry_counts": {},
        "status": "planning",
        "session_id": cfg.get("session_id", ""),
        "user_id": user_id,
        "channel": channel,
    }
