"""JSON-safe state contracts used by the LangGraph adapters."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
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
    plan_results: dict[str, Any]
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
    waiting: bool
    error: dict[str, Any]


def json_value(value: Any) -> Any:
    """Convert common domain values to checkpoint-safe JSON values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
        return value.value
    if hasattr(value, "__dict__"):
        return json_value(vars(value))
    return str(value)


__all__ = ["RootGraphState", "WorkerGraphState", "json_value"]
