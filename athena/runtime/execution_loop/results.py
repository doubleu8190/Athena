"""Execution-loop result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentExecutionResult:
    """LangGraph execution loop 的最终结果。"""

    content: str
    run_id: str
    turn_count: int
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    error_detail: dict[str, Any] | None = None
    interrupted: bool = False
