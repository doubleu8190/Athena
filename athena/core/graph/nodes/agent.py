"""Agent node — calls the LLM with tools, decides: answer or call tools.

This is the brain of the tool-calling agent.  It replaces the old ``plan_node``
by letting the LLM *directly* decide whether to answer the user or invoke tools
(via native function calling), instead of always generating a JSON plan.

Two outcomes:
1. LLM returns text (no tool_calls) → final answer, route to END
2. LLM returns tool_calls → route to tools_node for execution
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.types import RunnableConfig
from langchain_core.messages import AIMessage

from athena.core.graph.agent_state import AgentState
from athena.core.llm_provider.manager import LLMProviderManager
from athena.logging_config import bind_context, get_logger
from athena.mcp_client.registry import ToolRegistry

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

    Reads the full ``messages`` history (restored by checkpointer + new user
    message), injects the system prompt + system_context, and calls the LLM
    with native function-calling enabled.
    """
    llm_manager: LLMProviderManager = config["configurable"]["llm_manager"]
    tool_registry: ToolRegistry = config["configurable"]["tool_registry"]

    log = bind_context(session_id=state.get("session_id", ""))

    # Build tool definitions in OpenAI format (the providers handle conversion)
    tools: list[dict[str, Any]]  = tool_registry.export_for_llm() if tool_registry else []

    # Build messages: system prompt + system_context + conversation
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT}
    ]

    system_context = state.get("system_context")
    if system_context:
        messages.append({"role": "system", "content": system_context})

    # Convert LangChain messages to dicts for the LLM
    for msg in state.get("messages", []):
        if hasattr(msg, "type") and hasattr(msg, "content"):
            role = msg.type
            if role == "human":
                role = "user"
            elif role == "ai":
                role = "assistant"

            entry: dict[str, Any] = {"role": role, "content": msg.content}

            # If the AI message has tool_calls, convert to OpenAI format:
            #   {"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}
            # DeepSeek requires the "type" discriminator on each tool_call entry.
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.get("id", tc.get("tool_call_id", "")),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", tc.get("function", {}).get("name", "")),
                            "arguments": json.dumps(
                                tc.get("args", tc.get("arguments", tc.get("function", {}).get("arguments", {}))),
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for tc in msg.tool_calls
                ]

            # Tool messages must carry tool_call_id so the API can
            # associate the result with the preceding tool call.
            if role == "tool":
                entry["tool_call_id"] = getattr(msg, "tool_call_id", "")

            messages.append(entry)
        elif isinstance(msg, dict):
            # Dict messages may come from old-style history or
            # LangGraph checkpointing.  Normalize tool_calls to
            # OpenAI format when present.
            if msg.get("tool_calls"):
                msg = dict(msg)  # shallow copy to avoid mutating state
                msg["tool_calls"] = [
                    {
                        "id": tc.get("id", tc.get("tool_call_id", "")),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", tc.get("function", {}).get("name", "")),
                            "arguments": json.dumps(
                                tc.get("args", tc.get("arguments", tc.get("function", {}).get("arguments", {}))),
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for tc in msg["tool_calls"]
                ]
            messages.append(msg)

    # Track iterations
    iteration = state.get("agent_iteration", 0) + 1

    # ── Call LLM with tools ─────────────────────────────────────────────
    try:
        response = await llm_manager.generate(
            messages=messages,
            tools=tools if tools else None,
            temperature=0.7,
        )

        # Case 1: LLM answered directly (no tool calls)
        if response.text and not response.tool_calls:
            log.info("agent_direct_answer", iteration=iteration)
            return {
                "messages": [AIMessage(content=response.text)],
                "status": "completed",
                "agent_iteration": iteration,
                "pending_tool_calls": None,
            }

        # Case 2: LLM wants to call tools
        if response.tool_calls:
            log.info("agent_tool_calls", iteration=iteration, tool_calls=response.tool_calls)
            tool_names = [tc.get("name", "unknown") for tc in response.tool_calls]
            log.info(
                "agent_tool_calls",
                tools=tool_names,
                count=len(response.tool_calls),
                iteration=iteration,
            )

            ai_msg = AIMessage(content=response.text or "")
            ai_msg.tool_calls = [
                {
                    "id": tc.get("id", f"call_{i}"),
                    "name": tc.get("name", ""),
                    "args": tc.get("arguments", {}),
                }
                for i, tc in enumerate(response.tool_calls)
            ]

            return {
                "messages": [ai_msg],
                "pending_tool_calls": response.tool_calls,
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
