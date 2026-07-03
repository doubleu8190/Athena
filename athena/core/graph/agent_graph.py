"""Agent graph builder — constructs the tool-calling agent StateGraph.

This is the simplified replacement for the old plan→execute→collect→finalize
graph.  It implements a standard LLM agent loop with optional message
summarisation::

    START
      │
      ▼
    summarize  (SummarizationNode — trims long history, optional)
      │
      ▼
    agent ── (no tool_calls) ──→ END
      │
      └── (has tool_calls) ──→ tools
                                  │
                                  ▼
                                agent  (loop until answer or max iterations)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from athena.core.graph.agent_state import AgentState
from athena.core.graph.agent_routing import after_agent, after_tools
from athena.core.graph.nodes.agent import agent_node
from athena.core.graph.nodes.tools import tools_node



def build_agent_graph(
    checkpointer: BaseCheckpointSaver | None = None,
    summarization_model: Any = None  # noqa: ANN401,
) -> StateGraph:
    """Build and return the compiled tool-calling agent StateGraph.

    Args:
        checkpointer: Optional ``SqliteSaver`` for checkpointing.  Required
            for ``interrupt()`` / resume (human-in-the-loop confirmation).
        summarization_model: Optional LangChain chat model (e.g.
            ``ChatOpenAI``) used by ``SummarizationNode`` to compress long
            message histories.  If ``None``, messages grow unbounded — only
            suitable for short-lived sessions.

    Returns:
        A compiled StateGraph ready for ``astream()`` / ``ainvoke()``.
    """
    # ── Import SummarizationNode lazily (langmem is optional) ─────────
    try:
        from langmem.short_term import SummarizationNode as _SummarizationNode
        _HAS_LANGMEM = True
    except ImportError:
        _HAS_LANGMEM = False

    builder = StateGraph(AgentState)

    # ── Summarization node (optional, before agent) ────────────────────
    if _HAS_LANGMEM and summarization_model is not None:
        summarize_node = _SummarizationNode(
            model=summarization_model,
            max_tokens=4096,
            max_tokens_before_summary=8192,
            max_summary_tokens=512,
            input_messages_key="messages",
            output_messages_key="messages",  # in-place: auto RemoveMessage
            name="summarize",
        )
        builder.add_node("summarize", summarize_node)
        builder.add_edge(START, "summarize")
        builder.add_edge("summarize", "agent")
    else:
        builder.add_edge(START, "agent")

    # ── Nodes ───────────────────────────────────────────────────────────
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)

    # ── Edges ────────────────────────────────────────────────────────────
    # agent → END or tools
    builder.add_conditional_edges(
        "agent",
        after_agent,
        {
            "tools": "tools",
            "__end__": END,
        },
    )

    # tools → agent (loop back for synthesis or additional tool calls)
    builder.add_conditional_edges(
        "tools",
        after_tools,
        {
            "agent": "agent",
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
