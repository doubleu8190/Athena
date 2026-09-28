"""Reusable explicit test doubles for required runtime dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from athena.core.tools.manager import UnifiedToolManager
from athena.container import RuntimeContainer


@dataclass
class ApprovalResponse:
    future: asyncio.Future[bool]
    id: str = "auto-approved"
    timeout: float = 0


class AutoApprove:
    """Approval port used by tests that are not exercising approval behavior."""

    async def request_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        plan_id: str | None = None,
        task_id: str | None = None,
        worker_run_id: str | None = None,
    ) -> ApprovalResponse:
        future = asyncio.get_running_loop().create_future()
        future.set_result(True)
        return ApprovalResponse(future=future)

    async def wait_for_decision(self, approval_id: str, timeout: float) -> bool:
        return True


def make_tool_manager() -> UnifiedToolManager:
    return UnifiedToolManager(approval_manager=AutoApprove())


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
        "agent_store": MagicMock(),
        "realtime_transport": MagicMock(),
        "neo4j_graph_store": None,
    }
    dependencies.update(overrides)
    runtime = RuntimeContainer(**dependencies)
    app.state.runtime = runtime
    return runtime
