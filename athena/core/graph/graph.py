"""Graph builder — constructs the LangGraph StateGraph.

This is the central orchestration point.  Call ``build_graph()`` to get
a compiled graph, then stream it with ``graph.astream(initial_state, config)``.

The graph shape::

    START
      │
      ▼
    initialize  —  seed state from configurable
      │
      ▼
    plan  —  LLM generates TaskPlan
      │
      ├── (via Send) ──→ execute (parallel ready subtasks)
      │                      │
      │                      ├── success/skip ──→ collect
      │                      ├── retry ──→ execute (loop, max 5)
      │                      └── exhausted ──→ handle_failure
      │                                            │
      │                                            ├── abort/skip ──→ collect
      │                                            └── fallback ──→ execute
      │
      └── (plan failed) ──→ END

    collect  —  sink: merges parallel branch results
      │
      ├── (via Send) ──→ execute (next batch of ready subtasks)
      ├── (all done) ──→ finalize ──→ END
      └── (failures) ──→ plan (dynamic re-planning)

Fan-out / fan-in:
    ``after_plan`` and ``after_collect`` both return ``list[Send]`` to
    dispatch parallel ``execute`` branches.  Each branch follows its normal
    edges to ``collect``, which serves as the sink.  LangGraph automatically
    waits for all parallel branches to reach the sink before calling
    ``after_collect``.
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.base import BaseCheckpointSaver

from athena.core.graph.state import ExecutionState
from athena.core.graph.routing import (
    after_plan,
    after_execute,
    after_failure,
    after_collect,
)
from athena.core.graph.nodes.initialize import initialize_node
from athena.core.graph.nodes.plan import plan_node
from athena.core.graph.nodes.execute import execute_node
from athena.core.graph.nodes.handle_failure import handle_failure_node
from athena.core.graph.nodes.collect import collect_node
from athena.core.graph.nodes.finalize import finalize_node


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> StateGraph:
    """Build and return the compiled LangGraph StateGraph.

    The returned graph is *compiled* (a ``CompiledStateGraph``).

    Args:
        checkpointer: Optional checkpointer for checkpointing.  Required for
            ``interrupt()`` / resume (human-in-the-loop) and state persistence.
            If omitted, the graph runs stateless — pauses (``interrupt``) will
            raise an error.

    Returns:
        A compiled StateGraph ready for ``astream()`` / ``ainvoke()``.
    """
    builder = StateGraph(ExecutionState)

    # ── Add nodes ────────────────────────────────────────────────────
    builder.add_node("initialize", initialize_node)
    builder.add_node("plan", plan_node)
    builder.add_node("execute", execute_node)
    builder.add_node("handle_failure", handle_failure_node)
    builder.add_node("collect", collect_node)
    builder.add_node("finalize", finalize_node)

    # ── Edges ─────────────────────────────────────────────────────────
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "plan")

    # plan → Send("execute", …) or END
    # When after_plan returns list[Send], those Sends fan-out to "execute".
    # When it returns "__end__", the graph terminates.
    builder.add_conditional_edges(
        "plan",
        after_plan,
        {"__end__": END},
    )

    # execute → collect / retry(execute) / handle_failure
    builder.add_conditional_edges(
        "execute",
        after_execute,
        {
            "collect": "collect",
            "retry": "execute",
            "handle_failure": "handle_failure",
        },
    )

    # handle_failure → collect or execute (fallback)
    builder.add_conditional_edges(
        "handle_failure",
        after_failure,
        {
            "collect": "collect",
            "execute": "execute",
        },
    )

    # collect → Send("execute", …) for next batch, or plan(replan), or finalize
    # The path_map covers the *string* return values; list[Send] returns
    # are dispatched automatically.
    builder.add_conditional_edges(
        "collect",
        after_collect,
        {
            "plan": "plan",
            "finalize": "finalize",
            "__end__": END,
        },
    )

    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)


# ── Checkpointer factory ──────────────────────────────────────────────────

async def create_checkpointer(db_path: str | Path = "checkpoints.db"):
    """Create an async SQLite-backed checkpointer for the graph.

    .. warning::

       ``AsyncSqliteSaver.from_conn_string()`` is an ``@asynccontextmanager`` —
       calling it returns the *async-generator* (context-manager), not the
       ``AsyncSqliteSaver`` instance.  We create the ``aiosqlite`` connection
       manually so callers get a ready-to-use instance.

    Args:
        db_path: Path to the SQLite database file.  Defaults to
            ``checkpoints.db`` in the current working directory.

    Returns:
        An ``AsyncSqliteSaver`` instance suitable for LangGraph checkpoints.
        The caller is responsible for closing the underlying connection
        (``await checkpointer.conn.close()``) when done.
    """
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    conn = await aiosqlite.connect(str(db_path))
    return AsyncSqliteSaver(conn)
