"""AgentState — simplified state schema for the tool-calling agent graph.

Compared to the old ExecutionState, this drops all plan-time concepts
(task_plan, step_outputs, completed_steps, failed_steps, subtask_result,
circuit_breaker_count, retry_counts) in favour of a standard LLM agent loop
where the LLM decides which tools to call and synthesises the final answer.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """State for the tool-calling agent graph.

    All values MUST be pickle-serializable for LangGraph checkpointing.
    Non-serializable dependencies travel via ``config["configurable"]``.
    """

    # ── Conversation ──
    messages: Annotated[list[BaseMessage], add_messages]
    """Full message history managed by LangGraph's add_messages reducer.
    Context-aware summarization (in agent_node) reads from the session
    table, not from graph state."""

    system_context: str | None
    """Dynamic system context rebuilt each invocation (RAG-injected memories
    and other ephemeral metadata).  Injected into the agent's system prompt
    by ``summarize_node`` alongside the conversation summary."""

    # ── Tool execution ──
    pending_tool_calls: list[dict[str, Any]] | None
    """Tool calls returned by the LLM that need to be executed.
    Format: ``[{"id": "...", "name": "...", "arguments": {...}}, ...]``."""

    confirmed_tool_calls: list[dict[str, Any]] | None
    """Tool calls that passed confirmation and are ready to execute.
    Set by the confirm node; consumed by tools node via Send() fan-out."""

    agent_iteration: int
    """How many times the agent node has been invoked in the current loop.
    Guards against infinite tool-calling loops."""

    # ── Summarisation ──
    effective_messages: list | None
    """Compressed message list populated by ``summarize_node``.
    Contains ``[system_prompt, summary, messages[offset:]]`` when a
    summary exists, otherwise ``None`` (agent falls back to raw messages)."""

    summary_offset: int
    """Index into ``messages`` up to which summarisation has been applied.
    Managed by ``summarize_node`` and persisted atomically with the
    LangGraph checkpoint — no separate DB transaction required."""

    # ── Tool availability ──
    tools_available: bool
    """Whether MCP tools were loaded successfully.  Set by ``agent_node``;
    when ``False``, the agent warns the user that tools are unavailable."""

    # ── Control ──
    status: str
    """High-level status: ``thinking`` | ``executing`` | ``confirming`` |
    ``completed`` | ``failed``."""

    # ── Session identifiers ──
    session_id: str
    user_id: str
    channel: str
