"""Agent node — calls the LLM with tools, decides: answer or call tools.

This is the brain of the tool-calling agent.  It uses LangChain's native
message types (SystemMessage, HumanMessage, AIMessage, ToolMessage) and
``model.bind_tools()`` for function calling, letting LangChain handle all
API format conversion automatically.

Two outcomes:
1. LLM returns text (no tool_calls) → final answer, route to END
2. LLM returns tool_calls → route to tools_node for execution
"""

from __future__ import annotations

from typing import Any

from langgraph.types import RunnableConfig
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from athena.core.graph.agent_state import AgentState
from athena.logging_config import bind_context, get_logger

logger = get_logger(__name__)

# ── System prompt for the agent ─────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """You are Athena, a helpful AI assistant with access to tools.

## Your Capabilities
You can:
- Answer questions directly when you have sufficient knowledge
- Call tools to fetch real-time data, perform actions, or access external services
- Combine information from multiple tool calls to give comprehensive answers

## Guidelines
1. **Answer directly** for conversational messages or questions you can answer
   without tools (e.g., "hello", "what can you do?", general knowledge).
2. **Call tools** when you need real-time data, external services, or to perform
   actions.  Only call tools that actually exist — do not invent tool names.
3. **Be concise but complete** — give the user what they asked for, no more.
4. **When tools fail**, explain the failure to the user in plain language and
   suggest alternatives if possible.
5. **Use parallel tool calls** when you need data from multiple independent
   sources — the system will execute them concurrently.
6. **Format your answers with Markdown** for readability (lists, code blocks,
   tables where appropriate).

## Safety
- Never suggest or generate harmful commands
- Never attempt to access sensitive system paths
- If you're unsure, ask the user for clarification
"""

# ── Maximum agent iterations ────────────────────────────────────────────────

MAX_AGENT_ITERATIONS = 10
"""Hard limit on agent → tools → agent loops to prevent runaway chains."""


async def agent_node(
    state: AgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Call the LLM with tools and determine the next action.

    Builds a message list using native LangChain message types, loads fresh
    MCP tools via ``load_mcp_base_tools()`` for hot-plugging, binds them to
    the model with ``model.bind_tools()``, and invokes the model.

    LangChain handles ALL format conversion (OpenAI ↔ Anthropic) internally.
    """
    from athena.core.llm_provider.manager import LLMProviderManager
    from athena.mcp_client.tool_loader import load_mcp_base_tools

    llm_manager: LLMProviderManager = config["configurable"]["llm_manager"]
    session_id = config["configurable"]["session_id"]
    log = bind_context(session_id=session_id)

    # ── Build messages as native LangChain objects ─────────────────────
    messages: list[BaseMessage] = [SystemMessage(content=AGENT_SYSTEM_PROMPT)]

    system_context = state.get("system_context")
    if system_context:
        messages.append(SystemMessage(content=system_context))

    messages.extend(state.get("messages", []))

    # ── Load fresh MCP tools (supports hot-plugging) ──────────────────
    mcp_tools = await load_mcp_base_tools()

    # ── Bind tools to model ───────────────────────────────────────────
    model: BaseChatModel = llm_manager.base_model
    model_with_tools = model.bind_tools(mcp_tools) if mcp_tools else model

    # Track iterations
    iteration = state.get("agent_iteration", 0) + 1

    # ── Invoke model ──────────────────────────────────────────────────
    try:
        response: AIMessage = await model_with_tools.ainvoke(messages)

        # Case 1: LLM answered directly (no tool calls)
        if response.content and not response.tool_calls:
            log.info("agent_direct_answer", iteration=iteration)
            return {
                "messages": [response],
                "status": "completed",
                "agent_iteration": iteration,
                "pending_tool_calls": None,
            }

        # Case 2: LLM wants to call tools
        if response.tool_calls:
            tool_names = [tc.get("name", "unknown") for tc in response.tool_calls]
            log.info(
                "agent_tool_calls",
                tools=tool_names,
                count=len(response.tool_calls),
                iteration=iteration,
            )

            pending_tool_calls = [
                {
                    "id": tc.get("id", f"call_{i}"),
                    "name": tc.get("name", ""),
                    "arguments": tc.get("args", {}),
                }
                for i, tc in enumerate(response.tool_calls)
            ]

            return {
                "messages": [response],
                "pending_tool_calls": pending_tool_calls,
                "status": "executing",
                "agent_iteration": iteration,
            }

        # Case 3: LLM returned nothing useful
        log.warning("agent_empty_response")
        return {
            "messages": [AIMessage(content="I'm sorry, I couldn't generate a response. Could you try again?")],
            "status": "completed",
            "agent_iteration": iteration,
            "pending_tool_calls": None,
        }

    except Exception as e:
        log.error("agent_call_failed", error=str(e))
        return {
            "messages": [AIMessage(content=f"I encountered an error: {str(e)}. Please try again.")],
            "status": "failed",
            "agent_iteration": iteration,
            "pending_tool_calls": None,
        }
