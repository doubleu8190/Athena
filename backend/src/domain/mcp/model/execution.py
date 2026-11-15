"""Single-call MCP execution contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from domain.mcp.model.entity import McpConnectionInput


class McpCallStatus(StrEnum):
    """Business result of one MCP tools/call."""

    OK = "ok"
    ERROR = "error"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class McpRuntimeToolTarget:
    """Current DB-versioned routing target with in-memory connection secrets."""

    tool_id: str
    server_id: str
    remote_name: str
    config_version: int
    connection: McpConnectionInput = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpRuntimeToolDefinition:
    """One catalog row pinned with its server's current configuration version."""

    tool_id: str
    server_id: str
    remote_name: str
    model_name: str
    description: str
    input_schema: dict[str, Any]
    config_version: int
    requires_approval: bool = False


@dataclass(frozen=True, slots=True)
class McpCallResult:
    """Bounded, transport-independent projection of a tools/call result."""

    status: McpCallStatus
    code: str
    message: str = ""
    text: str = ""
    structured: Any = None
    unsupported_content: tuple[str, ...] = ()
    truncated: bool = False
    cleanup_error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.code or len(self.code) > 128:
            raise ValueError("mcp_call_result_invalid")
        if not isinstance(self.structured, (dict, list, str, int, float, bool, type(None))):
            raise ValueError("mcp_call_result_invalid")
        if not isinstance(self.unsupported_content, tuple):
            object.__setattr__(self, "unsupported_content", tuple(self.unsupported_content))


def mcp_message_from_result(result: object) -> str:
    """Extract only text content for a safe diagnostic message."""
    content = getattr(result, "content", ())
    if not isinstance(content, (list, tuple)):
        return ""
    parts = [
        item.text
        for item in content
        if getattr(item, "type", None) == "text" and isinstance(getattr(item, "text", None), str)
    ]
    return "\n".join(parts)[:2000]


def mcp_content_projection(result: object) -> tuple[str, Any, tuple[str, ...]]:
    """Project text/structured content; mark unsupported content without fetching it."""
    text_parts: list[str] = []
    unsupported: list[str] = []
    content = getattr(result, "content", ())
    if isinstance(content, (list, tuple)):
        for item in content:
            item_type = getattr(item, "type", None)
            if item_type == "text" and isinstance(getattr(item, "text", None), str):
                text_parts.append(item.text)
            elif isinstance(item_type, str):
                unsupported.append(item_type)
    structured = getattr(result, "structured_content", None)
    if structured is not None and isinstance(structured, (dict, list, str, int, float, bool)):
        return "\n".join(text_parts), structured, tuple(dict.fromkeys(unsupported))
    if structured is not None:
        unsupported.append("structured_invalid")
    return "\n".join(text_parts), None, tuple(dict.fromkeys(unsupported))


__all__ = [
    "McpCallResult",
    "McpCallStatus",
    "McpRuntimeToolTarget",
    "mcp_content_projection",
    "mcp_message_from_result",
]
