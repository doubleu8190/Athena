"""Tool Registry — global tool registration and state management.

Each tool has an independent state machine (active/stale/disabled) separate
from its parent MCP server's connection status.

State transitions:
    active --(server disconnect)--> stale --(reconnect + refresh)--> active
    active --(admin disable)--> disabled --(admin enable)--> active
    stale --(admin disable)--> disabled

Admin disables survive reconnects — reconnected tools that were disabled
remain disabled.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from athena.logging_config import get_logger
from athena.models import get_session, get_session_maker

logger = get_logger(__name__)


@dataclass
class RegisteredTool:
    """In-memory representation of a registered tool."""
    id: str
    name: str
    version: str
    description: str
    parameters_schema: dict[str, Any]
    source: str          # 'builtin', 'skill', 'external'
    source_server_id: str
    handler_info: str
    status: str = "active"          # 'active', 'stale', 'disabled'
    risk_level: str = "medium"
    supports_preview: bool = False
    idempotent: bool = True
    capability_tags: list[str] = field(default_factory=list)


class ToolRegistry:
    """Global registry of all tools from all MCP servers.

    Maintains the tool state machine and provides deterministic fallback
    resolution using a 6-level tiebreaker.
    """

    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}  # keyed by tool.id
        self._tools_by_name_server: dict[tuple[str, str], str] = {}  # (name, server_id) -> tool.id

    # ── Registration ──────────────────────────────────────────────────

    async def register(
        self,
        server_id: str,
        tools: list[Any],
        source: str,
    ) -> list[str]:
        """Register tools from a server. Returns list of tool IDs.

        Uses DB as the source of truth — inserts or updates tool records,
        then syncs to in-memory cache.
        """
        from athena.mcp_client.client import ToolDef

        registered_ids = []
        db_path = ""  # Will be set from config — here we use direct DB access
        # For now, use in-memory only; DB sync happens via seed loader and reconnects

        for tool in tools:
            if isinstance(tool, ToolDef):
                td = tool
            else:
                td = ToolDef(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    parameters_schema=tool.get("parameters_schema", tool.get("inputSchema", {})),
                    version=tool.get("version", "1.0.0"),
                    supports_preview=tool.get("supports_preview", False),
                    idempotent=tool.get("idempotent", True),
                    capability_tags=tool.get("capability_tags", []),
                    risk_level=tool.get("risk_level", "medium"),
                )

            tool_id = f"{server_id}::{td.name}"
            key = (td.name, server_id)

            if key in self._tools_by_name_server:
                # Update existing
                existing_id = self._tools_by_name_server[key]
                existing = self._tools[existing_id]
                existing.description = td.description
                existing.parameters_schema = td.parameters_schema
                existing.version = td.version
                existing.supports_preview = td.supports_preview
                existing.idempotent = td.idempotent
                existing.capability_tags = td.capability_tags
                existing.risk_level = td.risk_level
                # Keep disabled status if admin-set
                if existing.status != "disabled":
                    existing.status = "active"
                registered_ids.append(existing_id)
            else:
                rt = RegisteredTool(
                    id=tool_id,
                    name=td.name,
                    version=td.version,
                    description=td.description,
                    parameters_schema=td.parameters_schema,
                    source=source,
                    source_server_id=server_id,
                    handler_info=td.name,
                    status="active",
                    risk_level=td.risk_level,
                    supports_preview=td.supports_preview,
                    idempotent=td.idempotent,
                    capability_tags=td.capability_tags,
                )
                self._tools[tool_id] = rt
                self._tools_by_name_server[key] = tool_id
                registered_ids.append(tool_id)

        logger.info(
            "tools_registered",
            server_id=server_id,
            count=len(registered_ids),
        )
        return registered_ids

    # ── State machine operations ──────────────────────────────────────

    async def mark_stale(self, server_id: str) -> None:
        """Mark all tools from a server as stale (server disconnected)."""
        count = 0
        for tool in self._tools.values():
            if tool.source_server_id == server_id and tool.status == "active":
                tool.status = "stale"
                count += 1
        if count:
            logger.info("tools_marked_stale", server_id=server_id, count=count)

    async def refresh(self, server_id: str, fresh_tools: list[Any]) -> None:
        """Handle tools/list refresh after reconnect.

        Compares fresh vs existing tools:
        - New tools → register as active
        - Deleted tools (not in fresh list) → mark disabled
        - Existing tools → stale→active (unless admin-disabled)
        """
        from athena.mcp_client.client import ToolDef

        fresh_names = set()
        for tool in fresh_tools:
            name = tool.name if isinstance(tool, ToolDef) else tool.get("name", "")
            fresh_names.add(name)

        # Mark deleted tools as disabled
        for tool in list(self._tools.values()):
            if tool.source_server_id == server_id:
                if tool.name not in fresh_names:
                    tool.status = "disabled"
                    logger.info("tool_marked_deleted", tool_id=tool.id, name=tool.name)

        # Re-register fresh tools (register() handles update/create)
        await self.register(server_id, fresh_tools, self._get_source_for_server(server_id))

        # Restore stale to active (except disabled)
        for tool in self._tools.values():
            if tool.source_server_id == server_id:
                if tool.status == "stale":
                    tool.status = "active"

    async def disable_tool(self, tool_id: str) -> bool:
        """Admin action: disable a tool. Returns True if successful."""
        tool = self._tools.get(tool_id)
        if not tool:
            return False
        tool.status = "disabled"
        logger.info("tool_disabled", tool_id=tool_id, name=tool.name)
        return True

    async def enable_tool(self, tool_id: str) -> bool:
        """Admin action: re-enable a disabled tool. Returns True if successful."""
        tool = self._tools.get(tool_id)
        if not tool:
            return False
        tool.status = "active"
        logger.info("tool_enabled", tool_id=tool_id, name=tool.name)
        return True

    # ── Queries ───────────────────────────────────────────────────────

    def get_active_tools(self) -> list[RegisteredTool]:
        """Return all active tools (for Planner injection)."""
        return [t for t in self._tools.values() if t.status == "active"]

    def get_tool_by_name(
        self, name: str, server_id: str | None = None
    ) -> RegisteredTool | None:
        """Find a tool by name, optionally scoped to a server."""
        if server_id:
            key = (name, server_id)
            tool_id = self._tools_by_name_server.get(key)
            return self._tools.get(tool_id) if tool_id else None
        # Search all servers for first match
        for key, tool_id in self._tools_by_name_server.items():
            if key[0] == name:
                return self._tools.get(tool_id)
        return None

    def get_tool_by_id(self, tool_id: str) -> RegisteredTool | None:
        return self._tools.get(tool_id)

    def get_server_tools(self, server_id: str) -> list[RegisteredTool]:
        """Return all tools from a specific server."""
        return [t for t in self._tools.values() if t.source_server_id == server_id]

    # ── Fallback resolution ───────────────────────────────────────────

    def resolve_fallback(
        self,
        failed_tool_name: str,
        capability_tag: str | None = None,
    ) -> RegisteredTool | None:
        """Deterministic fallback tool selection using a 6-level tiebreaker.

        Rules:
        1. capability_tag exact match (with the failed tool)
        2. risk_level lowest first
        3. supports_preview preferred
        4. Same source_server_id as failed
        5. Same source type (builtin/skill/external)
        6. Dictionary order by tool_name (stability guarantee)

        Excludes: the failed tool itself, status != 'active', server not connected.
        """
        failed_tool = self.get_tool_by_name(failed_tool_name)
        if not failed_tool:
            return None

        failed_tags = set(failed_tool.capability_tags)
        if capability_tag:
            failed_tags.add(capability_tag)

        # Collect candidates
        candidates = []
        for tool in self._tools.values():
            if tool.id == failed_tool.id:
                continue
            if tool.status != "active":
                continue
            candidates.append(tool)

        if not candidates:
            return None

        # Score each candidate (lower score = better)
        def score(tool: RegisteredTool) -> tuple:
            # 1. capability_tag match (0 = matches, 1 = no match)
            tool_tags = set(tool.capability_tags)
            tag_match = 0 if (failed_tags & tool_tags) else 1

            # 2. risk_level (lower is safer)
            risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
            risk_score = risk_order.get(tool.risk_level, 1)

            # 3. supports_preview (0 = yes = better, 1 = no)
            preview_score = 0 if tool.supports_preview else 1

            # 4. Same server (0 = same, 1 = different)
            server_score = 0 if tool.source_server_id == failed_tool.source_server_id else 1

            # 5. Same source type (0 = same, 1 = different)
            source_score = 0 if tool.source == failed_tool.source else 1

            # 6. Dictionary order (stable sort key)
            name_key = tool.name

            return (tag_match, risk_score, preview_score, server_score, source_score, name_key)

        candidates.sort(key=score)
        return candidates[0]

    # ── Helpers ───────────────────────────────────────────────────────

    def _get_source_for_server(self, server_id: str) -> str:
        """Determine the source type for a server."""
        for tool in self._tools.values():
            if tool.source_server_id == server_id:
                return tool.source
        return "external"

    def export_for_planner(self) -> list[dict[str, Any]]:
        """Export active tools in a format suitable for LLM system prompts."""
        tools = self.get_active_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters_schema": t.parameters_schema,
                "source_server_id": t.source_server_id,
                "risk_level": t.risk_level,
                "supports_preview": t.supports_preview,
                "capability_tags": t.capability_tags,
            }
            for t in tools
        ]


# ── Singleton ───────────────────────────────────────────────────────────────

_tool_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """Return the process-wide singleton ToolRegistry (lazy-init).

    There is exactly one tool registry per process. It is a pure in-memory
    data container with no lifecycle methods, making it an ideal singleton.
    Shared by MCPClient, Planner, Executor, and API handlers.
    """
    global _tool_registry
    if _tool_registry is not None:
        return _tool_registry
    _tool_registry = ToolRegistry()
    logger.info("tool_registry_singleton_initialized")
    return _tool_registry
