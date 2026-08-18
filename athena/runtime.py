"""Explicit application runtime dependencies attached to ``FastAPI.app.state``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.requests import HTTPConnection

if TYPE_CHECKING:
    from athena.core.agent.workflow import AgentWorkflow
    from athena.core.files.runtime import FileIntelligenceRuntime
    from athena.core.files.tasks import FileTaskWorker
    from athena.core.llm.provider import LLMProvider
    from athena.core.memory.memory import MemoryManager
    from athena.core.tools.catalog import ToolCatalogService
    from athena.core.tools.manager import UnifiedToolManager
    from athena.core.tools.mcp.manager import MCPManager
    from athena.gateway.approval import ApprovalManager
    from athena.gateway.ws.manager import WebSocketManager
    from athena.infrastructure.sqlite.database import Database


@dataclass
class RuntimeContainer:
    db: Database
    websocket_manager: WebSocketManager
    approval_manager: ApprovalManager
    tool_manager: UnifiedToolManager
    tool_catalog: ToolCatalogService
    mcp_manager: MCPManager
    llm: LLMProvider
    file_runtime: FileIntelligenceRuntime
    file_worker: FileTaskWorker
    memory_manager: MemoryManager
    workflow: AgentWorkflow


def runtime_from(connection: HTTPConnection) -> RuntimeContainer:
    """Read the required runtime container from a Request or WebSocket."""
    app = getattr(connection, "app", None)
    state = getattr(app, "state", None)
    runtime = getattr(state, "runtime", None)
    if not isinstance(runtime, RuntimeContainer):
        raise RuntimeError("Application runtime is not initialized")
    return runtime
