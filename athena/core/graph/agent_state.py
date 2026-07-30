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

    # ── Tool execution ──
    pending_tool_calls: list[dict[str, Any]] | None
    """Tool calls returned by the LLM that need to be executed.
    Format: ``[{"id": "...", "name": "...", "arguments": {...}}, ...]``."""

    confirmed_tool_calls: list[dict[str, Any]] | None
    """Tool calls that passed confirmation and are ready to execute.
    Set by the confirm node; consumed by tools node via Send() fan-out."""

    allowed_tool_calls: list[dict[str, Any]] | None
    """Tool calls that passed pre-check without needing confirmation.
    Set by ``precheck_node``; merged into Send() fan-out by ``after_confirm``."""

    blocked_tool_calls: list[dict[str, Any]] | None
    """Tool calls blocked by harness pre-check.  Set by ``precheck_node``."""

    needs_confirmation_tool_calls: list[dict[str, Any]] | None
    """Tool calls requiring user confirmation.  Set by ``precheck_node``;
    consumed sequentially by ``confirm_node`` via state-based flow."""

    _awaiting_confirmation: list[dict[str, Any]] | None
    """Set by ``confirm_node`` when a tool lacks a confirmation decision.
    The ``after_confirm`` router sees this and routes to END so the
    external handler can request user input.  After the decision is
    provided, the graph is re-invoked targeting ``confirm``."""

    _confirmation_results: dict[str, str] | None
    """Mapping of tool_call_id → "approved"|"rejected".  Populated by the
    external handler after receiving user confirmation.  The ``confirm_node``
    reads this to process pending tools."""

    agent_iteration: int
    """How many times the agent node has been invoked in the current loop.
    Guards against infinite tool-calling loops."""

    # ── Summarisation ──
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
