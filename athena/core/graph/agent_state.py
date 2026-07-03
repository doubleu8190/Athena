"""AgentState — simplified state schema for the tool-calling agent graph.

Compared to the old ExecutionState, this drops all plan-time concepts
(task_plan, step_outputs, completed_steps, failed_steps, subtask_result,
circuit_breaker_count, retry_counts) in favour of a standard LLM agent loop
where the LLM decides which tools to call and synthesises the final answer.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from langmem.short_term import RunningSummary


class AgentState(TypedDict):
    """State for the tool-calling agent graph.

    All values MUST be pickle-serializable for LangGraph checkpointing.
    Non-serializable dependencies travel via ``config["configurable"]``.
    """

    # ── Conversation ──
    messages: Annotated[list[BaseMessage], add_messages]
    """Full message history managed by LangGraph's add_messages reducer
    and automatically summarised by the ``SummarizationNode``."""

    context: RunningSummary | None
    """Running summary state maintained by ``SummarizationNode``.
    Tracks which messages have been summarised so the node only
    processes new messages on each invocation.  Set to ``None`` on
    first run; the summarization node populates it."""

    system_context: str | None
    """Dynamic system context rebuilt each invocation (RAG-injected memories
    and other ephemeral metadata).  *Not* used for conversation summary
    (that is handled by ``context`` + ``SummarizationNode``)."""

    # ── Tool execution ──
    pending_tool_calls: list[dict[str, Any]] | None
    """Tool calls returned by the LLM that need to be executed.
    Format: ``[{"id": "...", "name": "...", "arguments": {...}}, ...]``."""

    agent_iteration: int
    """How many times the agent node has been invoked in the current loop.
    Guards against infinite tool-calling loops."""

    # ── Control ──
    status: str
    """High-level status: ``thinking`` | ``executing`` | ``confirming`` |
    ``completed`` | ``failed``."""

    # ── Session identifiers ──
    session_id: str
    user_id: str
    channel: str
