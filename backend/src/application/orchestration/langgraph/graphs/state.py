"""JSON-safe state contracts used by the LangGraph adapters."""

from __future__ import annotations

from typing import Any, TypedDict


class RootGraphState(TypedDict, total=False):
    """Recoverable Root state.

    Runtime objects such as an LLM, database session, event bus or stop signal
    are deliberately kept out of this mapping.  They are supplied through
    graph dependencies and invocation config instead.
    """

    request: dict[str, Any]
    understanding: dict[str, Any]
    context_plan: dict[str, Any]
    context_bundle: dict[str, Any]
    harness_input: dict[str, Any]
    execution: dict[str, Any]
    plan: dict[str, Any]
    deferred_plan_versions: dict[str, list[dict[str, Any]]]
    plan_results: dict[str, Any]
    root_wait: dict[str, Any]
    response: dict[str, Any]
    result: dict[str, Any]
    resume_value: dict[str, Any]
    next_route: str
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


__all__ = ["RootGraphState", "WorkerGraphState"]
