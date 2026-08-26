"""Reusable explicit test doubles for required runtime dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from athena.core.tools.manager import UnifiedToolManager
from athena.runtime import RuntimeContainer
from athena.evaluation.portal import EvaluationPortal


@dataclass
class ApprovalResponse:
    future: asyncio.Future[bool]


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
    ) -> ApprovalResponse:
        future = asyncio.get_running_loop().create_future()
        future.set_result(True)
        return ApprovalResponse(future=future)


def make_tool_manager() -> UnifiedToolManager:
    return UnifiedToolManager(approval_manager=AutoApprove())


def install_runtime(app: Any, **overrides: Any) -> RuntimeContainer:
    """Attach a complete runtime container to a focused FastAPI test app."""
    dependencies = {
        "db": MagicMock(),
        "websocket_manager": MagicMock(),
        "approval_manager": MagicMock(),
        "tool_manager": MagicMock(),
        "tool_catalog": MagicMock(),
        "mcp_manager": MagicMock(),
        "llm": MagicMock(),
        "file_runtime": MagicMock(),
        "file_worker": MagicMock(),
        "memory_manager": MagicMock(),
        "workflow": MagicMock(),
        "evaluation_portal": EvaluationPortal("/tmp/athena_test_evaluation"),
    }
    dependencies.update(overrides)
    runtime = RuntimeContainer(**dependencies)
    app.state.runtime = runtime
    return runtime
