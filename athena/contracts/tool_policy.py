"""Tool authorization contracts shared by the core tool layer and runtime."""

from __future__ import annotations

from dataclasses import dataclass

from .orchestration import AgentRole

DELEGATION_TOOL_NAMES = frozenset({"spawn_sub_agent", "spawn_parallel_agents"})


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    role: AgentRole
    allowed_tools: frozenset[str]

    @classmethod
    def for_worker(
        cls, available_tools: list[str], requested_tools: list[str] | None = None
    ) -> ToolPolicy:
        available = set(available_tools) - DELEGATION_TOOL_NAMES
        if requested_tools is None:
            allowed = available
        else:
            requested = set(requested_tools)
            unknown = requested - set(available_tools)
            if unknown:
                raise ValueError(
                    f"worker requested unknown tools: {', '.join(sorted(unknown))}"
                )
            allowed = requested & available
        return cls(role=AgentRole.WORKER, allowed_tools=frozenset(allowed))

    def permits(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools

