"""附加到 ``FastAPI.app.state`` 的显式应用运行时依赖。"""

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
    from athena.evaluation.shadow import ShadowRetrievalRunner
    from athena.evaluation.portal import EvaluationPortal


@dataclass
class RuntimeContainer:
    """表示 RuntimeContainer 组件，封装相关状态和行为。
    """
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
    evaluation_portal: EvaluationPortal


def runtime_from(connection: HTTPConnection) -> RuntimeContainer:
    """从 Request 或 WebSocket 中读取所需的运行时容器。"""
    app = getattr(connection, "app", None)
    state = getattr(app, "state", None)
    runtime = getattr(state, "runtime", None)
    if not isinstance(runtime, RuntimeContainer):
        raise RuntimeError("Application runtime is not initialized")
    return runtime
