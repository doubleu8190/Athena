"""Agent node — calls the LLM with tool schemas and returns the response.

Context-aware summarisation is handled by the upstream ``summarize_node``
which manages ``summary_offset`` before this node runs.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from athena.core.graph.agent_routing import MAX_AGENT_ITERATIONS
from athena.core.graph.agent_state import AgentState
from athena.core.llm_provider.manager import LLMProviderManager
from athena.core.prompt_loader import load_prompt
from athena.logging_config import bind_context, get_logger

logger = get_logger(__name__)

AGENT_SYSTEM_PROMPT = load_prompt("agent_system.md")


async def _load_tools() -> list:
    """Load MCP tools as BaseTool instances for LLM binding."""
    try:
        from athena.mcp_client.tool_loader import load_mcp_base_tools
        return await load_mcp_base_tools()
    except Exception as e:
        logger.warning("agent_tools_load_failed", error=str(e))
        return []


async def _load_session_summary(session_id: str) -> str | None:
    """Load the cumulative conversation summary from the session table.

    Returns ``None`` when no summary exists.
    """
    from sqlalchemy import select

    from athena.models.base import get_session
    from athena.models.session import Session

    db = await get_session()
    try:
        row = (
            await db.execute(
                select(Session.summary).where(Session.session_id == session_id)
            )
        ).first()
        return row[0] if row and row[0] else None
    finally:
        await db.close()


async def agent_node(state: AgentState, config: RunnableConfig) -> dict:
    """Call the LLM with tool schemas and return its response.

    System prompts are assembled immediately before each LLM invocation
    and are never stored in graph state.  Conversation summaries are loaded
    from the session table on demand.
    """
    configurable: dict[str, Any] = config.get("configurable", {})

    llm_manager: LLMProviderManager | None = configurable.get("llm_manager")
    if llm_manager is None:
        from athena.core.llm_provider.manager import get_llm_manager
        llm_manager = get_llm_manager()

    thread_id: str = configurable.get("thread_id", state.get("session_id", ""))
    log = bind_context(session_id=thread_id, node="agent_node")

    iteration = state.get("agent_iteration", 0) + 1

    # ── Max-iteration guard ─────────────────────────────────────────────
    if iteration > MAX_AGENT_ITERATIONS:
        log.warning("max_iterations_reached", iteration=iteration)
        fallback = AIMessage(
            content=(
                f"I've reached the maximum number of iterations ({MAX_AGENT_ITERATIONS}). "
                "Stopping to avoid an infinite loop. Please try rephrasing your request."
            )
        )
        return {
            "messages": [fallback],
            "status": "completed",
            "pending_tool_calls": None,
            "agent_iteration": iteration,
        }

    # Load tools and bind to LLM
    tools_available = True
    try:
        all_tools = await _load_tools()
        tools_available = bool(all_tools)
        llm = llm_manager.base_model
        if all_tools:
            llm = llm.bind_tools(all_tools)

        summary_offset = state.get("summary_offset", 0)
        all_messages = state["messages"]
        effective = all_messages[summary_offset:]
        session_id = configurable.get("thread_id", state.get("session_id", ""))
        summary = await _load_session_summary(session_id)
        messages = _build_messages(effective, summary)

        response: AIMessage = await llm.ainvoke(messages)
    except Exception as e:
        log.error("agent_node_error", error=str(e))
        error_msg = AIMessage(content=f"Error: {e}")
        return {
            "messages": [error_msg],
            "status": "failed",
            "pending_tool_calls": None,
            "agent_iteration": iteration,
            "tools_available": tools_available,
        }

    # ── Empty-response guard ────────────────────────────────────────────
    if not response.content and not response.tool_calls:
        log.warning("empty_llm_response", tools_available=tools_available)
        msg = "I'm sorry, I couldn't generate a response. Please try again."
        if not tools_available:
            msg += " (Note: external tools are temporarily unavailable.)"
        fallback = AIMessage(content=msg)
        return {
            "messages": [fallback],
            "status": "completed",
            "pending_tool_calls": None,
            "agent_iteration": iteration,
            "tools_available": tools_available,
        }

    # Determine status based on tool calls
    tool_calls = response.tool_calls or []
    if tool_calls:
        status = "executing"
        pending = [
            {
                "id": tc.get("id", ""),
                "name": tc.get("name", ""),
                "arguments": tc.get("args", {}),
            }
            for tc in tool_calls
        ]
    else:
        status = "completed"
        pending = None
        # Warn the user when tools were unavailable and the agent
        # produced a text-only answer (may be hallucinated).
        if not tools_available and response.content:
            log.info("answer_without_tools")
            content = ""
            if isinstance(response.content, list):
                content = "\n".join(str(item) for item in response.content)
            else:
                content = response.content
            response = AIMessage(
                content=content
                + "\n\n⚠️ External tools are temporarily unavailable — "
                "this answer may be incomplete."
            )

    return {
        "messages": [response],
        "status": status,
        "pending_tool_calls": pending,
        "agent_iteration": iteration,
        "tools_available": tools_available,
    }


def _build_messages(
    raw_messages: list,
    summary: str | None = None,
) -> list:
    """Assemble the message list with system prompt for LLM invocation."""
    messages: list = [SystemMessage(content=AGENT_SYSTEM_PROMPT)]
    if summary:
        messages.append(SystemMessage(content=f"Conversation summary:\n{summary}"))
    messages.extend(raw_messages)
    return messages
