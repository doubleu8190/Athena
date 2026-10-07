"""Reusable explicit test doubles for required runtime dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from athena.core.tools.manager import UnifiedToolManager
from athena.container import RuntimeContainer


def make_tool_manager() -> UnifiedToolManager:
    return UnifiedToolManager()


class FakeMemoryGraphStore:
    """测试用图存储替身，显式满足记忆服务的必需依赖。"""

    def __init__(self) -> None:
        self.deleted_memory_ids: list[str] = []
        self.nodes: dict[str, dict[str, Any]] = {}

    async def upsert_memory_node(self, record: dict[str, Any]) -> None:
        self.nodes[str(record["id"])] = dict(record)

    async def delete_memory_node(self, memory_id: str) -> None:
        self.deleted_memory_ids.append(memory_id)
        self.nodes.pop(memory_id, None)

    async def add_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None

    async def remove_relation(
        self, source_id: str, target_id: str, relation_type: str
    ) -> None:
        return None

    async def list_revisions(self, memory_id: str) -> list[dict[str, Any]]:
        return []


def install_runtime(app: Any, **overrides: Any) -> RuntimeContainer:
    """Attach a complete runtime container to a focused FastAPI test app."""
    dependencies = {
        "db": MagicMock(),
        "retrieval_trace_reader": MagicMock(),
        "event_publisher": MagicMock(),
        "approval_manager": MagicMock(),
        "tool_manager": MagicMock(),
        "tool_catalog": MagicMock(),
        "mcp_manager": MagicMock(),
        "llm": MagicMock(),
        "file_runtime": MagicMock(),
        "memory_service": MagicMock(),
        "command_store": MagicMock(),
        "run_store": MagicMock(),
        "approval_store": MagicMock(),
        "event_store": MagicMock(),
        "realtime_transport": MagicMock(),
        "neo4j_graph_store": MagicMock(),
    }
    dependencies.update(overrides)
    runtime = RuntimeContainer(**dependencies)
    app.state.runtime = runtime
    return runtime
