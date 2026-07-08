"""Agent node — calls the LLM with tool schemas and returns the response.

Context-aware summarisation is handled by the upstream ``summarize_node``
which populates ``state["effective_messages"]`` before this node runs.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from athena.core.graph.agent_routing import MAX_AGENT_ITERATIONS
from athena.core.graph.agent_state import AgentState
from athena.core.llm_provider.manager import LLMProviderManager
from athena.logging_config import bind_context, get_logger

logger = get_logger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """
# ROLE
You are Athena, a helpful AI assistant with tool-calling capabilities via MCP.

# CORE DECISION TREE
1. **Direct Reply** -> User greets, asks general knowledge, or requests explanations.
2. **Tool Call** -> User asks for real-time data, external actions, or private context.

# TOOL EXECUTION PROTOCOL
- **Parallel**: If tools have zero data dependency, call ALL in one turn to minimize latency.
- **Sequential**: If Tool B's input requires Tool A's output, call A -> wait -> call B.
- **Pagination/Safety**: Never call more than 5 tools in a single turn. If more needed, ask user to narrow scope.
- **Data Handling**: If tool returns massive JSON (>10k tokens), summarize key points before presenting. Do not dump raw JSON unless explicitly requested.

# OUTPUT STANDARDS
- **Language**: Always match the user's input language exactly.
- **Format**: Use Markdown. Tables for comparisons, code blocks with language tags, bullet lists for steps.
- **Conciseness**: Answer the exact question. Omit disclaimers like "As an AI..." or "Based on my knowledge...".

# BOUNDARIES & FAILURE RECOVERY
- **Hallucination**: Never invent tool names, parameters, or results. If tool doesn't exist, say "Tool unavailable" and offer a manual workaround.
- **Failure Loop**: If the same tool fails twice, STOP calling it. Explain the error, suggest manual action, and ask for guidance.
- **Sensitive Data**: Never expose system paths, auth tokens, or internal configs in output.

# PERSONA CONSTRAINT
- Be direct and witty, but never sarcastic. If uncertain, say "I don't know" immediately without prelude.

---
**Final Rule**: If this instruction conflicts with user request, follow THIS instruction.
"""


async def _load_tools() -> list:
    """Load MCP tools as BaseTool instances for LLM binding."""
    try:
        from athena.mcp_client.tool_loader import load_mcp_base_tools
        return await load_mcp_base_tools()
    except Exception as e:
        logger.warning("agent_tools_load_failed", error=str(e))
        return []


async def agent_node(state: AgentState, config: RunnableConfig) -> dict:
    """Call the LLM with tool schemas and return its response.

    Expects ``effective_messages`` to have been populated by the upstream
    ``summarize_node``.  Falls back to building messages from raw state
    when absent (e.g. in tests).
    """
    configurable: dict[str, Any] = config.get("configurable", {})

    llm_manager: LLMProviderManager | None = configurable.get("llm_manager")
    if llm_manager is None:
        raise KeyError(
            "'llm_manager' missing from config['configurable']. "
            "Ensure the LLMProviderManager is passed in the graph config."
        )

    session_id: str = configurable.get("session_id", state.get("session_id", ""))
    log = bind_context(session_id=session_id, node="agent_node")

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

        # Use effective_messages from summarize_node, fall back to raw messages
        effective = state.get("effective_messages")
        if effective is not None:
            messages = effective
        else:
            system_ctx = configurable.get("system_context") or state.get("system_context")
            messages = _build_messages(state["messages"], system_ctx)

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
            log.warning("answer_without_tools")
            response = AIMessage(
                content=response.content
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
    system_context: str | None = None,
) -> list:
    """Fallback: build message list without summarisation."""
    messages: list = [SystemMessage(content=AGENT_SYSTEM_PROMPT)]
    if system_context:
        messages.append(SystemMessage(content=system_context))
    messages.extend(raw_messages)
    return messages
