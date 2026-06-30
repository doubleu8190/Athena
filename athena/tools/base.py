"""Tool definition helpers for built-in MCP tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class ToolHandler:
    """Definition of a built-in tool handler."""
    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    risk_level: str = "medium"
    idempotent: bool = True
    capability_tags: list[str] = field(default_factory=list)
