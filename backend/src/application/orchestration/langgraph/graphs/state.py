"""JSON-safe state contracts used by the LangGraph adapters."""

from __future__ import annotations

from typing import Any, TypedDict

from application.orchestration.graph_state import (
    RootAnswerInputState,
    RootPlanIdentity,
    RootResumeIdentity,
    RootWaitIdentity,
)
from application.runs.request_contracts import RootRequestState


class RootGraphState(TypedDict, total=False):
    """Recoverable Root state.

    Structured values are stored as JSON dictionaries and parsed into their
    concrete models when workflow nodes consume them.

    Runtime objects such as an LLM, database session, event bus or stop signal
    are deliberately kept out of this mapping.  They are supplied through
    graph dependencies and invocation config instead.
    """

    request: dict[str, Any]
    root_decision: dict[str, Any]
    root_turn_count: int
    planning_feedback: dict[str, Any] | None
    planning_context_updated: bool
    plan_draft: dict[str, Any] | None
    answer_input: dict[str, Any] | None
    worker_tools: list[dict[str, Any]]
    knowledge_bases: list[dict[str, Any]]
    available_context_providers: list[str]
    context_queries: list[dict[str, Any]]
    context_bundle: dict[str, Any]
    plan: dict[str, Any]
    deferred_plan_versions: dict[str, list[dict[str, Any]]]
    plan_results: dict[str, Any]
    root_wait: dict[str, Any]
    response: dict[str, Any]
    result: dict[str, Any]
    resume_value: dict[str, Any]
    execution_lease: dict[str, Any]
    status: str
    waiting: bool
    error: dict[str, Any]


class WorkerGraphState(TypedDict, total=False):
    """Recoverable state for exactly one Task execution."""

    execution: dict[str, Any]
    input: dict[str, Any]
    messages: list[dict[str, Any]]
    response: dict[str, Any]
    result: dict[str, Any]
    status: str
    waiting: bool
    error: dict[str, Any]
    tool_round: int
    pending_approval: dict[str, Any] | None
    pending_tool_calls: list[dict[str, Any]]
    pending_tool_index: int


__all__ = [
    "RootAnswerInputState",
    "RootPlanIdentity",
    "RootGraphState",
    "RootRequestState",
    "RootResumeIdentity",
    "RootWaitIdentity",
    "WorkerGraphState",
]
