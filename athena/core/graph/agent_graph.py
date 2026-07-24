"""Agent graph builder — constructs the tool-calling agent StateGraph.

Standard LLM agent loop::

    START
      │
      ▼
    summarize ──→ agent ── (no tool_calls) ──→ END
                     │
                     └── (has tool_calls) ──→ confirm ──→ Send(tools, call_1) ─┐
                                                   Send(tools, call_2) ─┼→ summarize (loop)
                                                   Send(tools, call_3) ─┘

``interrupt()`` (human-in-the-loop) only happens inside ``confirm_node``
— never inside ``Send()`` branches — which prevents the duplicate
confirmation bug caused by LangGraph re-evaluating conditional edges on
resume.

Context-aware summarisation is handled by ``summarize_node`` which runs
**before** every ``agent_node`` call.  It estimates token usage and, if
needed, summarises older messages via the fast model and persists the
summary in the session table.
"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from athena.core.graph.agent_routing import after_agent, after_confirm, after_tools
from athena.core.graph.agent_state import AgentState
from athena.core.graph.nodes.agent import agent_node
from athena.core.graph.nodes.confirm import confirm_node
from athena.core.graph.nodes.summarize import summarize_node
from athena.core.graph.nodes.tools import tools_node


def build_agent_graph(
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Build and return the compiled tool-calling agent StateGraph.

    Args:
        checkpointer: Optional ``SqliteSaver`` for checkpointing.  Required
            for ``interrupt()`` / resume (human-in-the-loop confirmation).

    Returns:
        A compiled StateGraph ready for ``astream()`` / ``ainvoke()``.
    """
    builder = StateGraph(AgentState)

    # ── Nodes ───────────────────────────────────────────────────────────
    builder.add_node("summarize", summarize_node)
    builder.add_node("agent", agent_node)
    builder.add_node("confirm", confirm_node)
    builder.add_node("tools", tools_node)

    # ── Edges ────────────────────────────────────────────────────────────
    builder.add_edge(START, "summarize")
    builder.add_edge("summarize", "agent")

    # agent → confirm (when tool_calls exist) or END
    builder.add_conditional_edges(
        "agent",
        after_agent,
        {
            "confirm": "confirm",
            "__end__": END,
        },
    )

    # confirm → fan-out to tools via Send(), or END (all blocked/rejected)
    builder.add_conditional_edges("confirm", after_confirm)

    # tools → summarize → agent (loop back for synthesis or additional tool calls)
    builder.add_conditional_edges(
        "tools",
        after_tools,
        {
            "summarize": "summarize",
            "__end__": END,
        },
    )

    return builder.compile(checkpointer=checkpointer)


# ── Checkpointer factory (shared with agent graph) ──────────────────────────

async def create_checkpointer(db_path: str | Path = "checkpoints.db") -> AsyncSqliteSaver:
    """Create an async SQLite-backed checkpointer for the agent graph.

    .. warning::

       ``AsyncSqliteSaver.from_conn_string()`` is an ``@asynccontextmanager`` —
       calling it returns the *async-generator* (context-manager), not the
       ``AsyncSqliteSaver`` instance.  We create the ``aiosqlite`` connection
       manually so callers get a ready-to-use instance.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        An ``AsyncSqliteSaver`` instance suitable for LangGraph checkpoints.
        The caller is responsible for closing the underlying connection
        (``await checkpointer.conn.close()``) when done.
    """
    import aiosqlite

    conn = await aiosqlite.connect(str(db_path))
    return AsyncSqliteSaver(conn)
