"""应用级显式依赖容器，附加到 ``FastAPI.app.state``。

将各子系统的运行时实例聚合到一个数据类中，供路由层通过
``get_runtime_container()`` 透明获取。隔离了运行时执行逻辑与依赖组装。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.requests import HTTPConnection

from athena.core.sandbox.ports import SandboxRunner
from athena.core.sandbox.workspace import WorkspaceManager
from athena.runtime.transport import SessionEventBus, RuntimeEventPublisher
from athena.infrastructure.postgre.repositories.agent_store import AgentStore

if TYPE_CHECKING:
    from athena.core.files.runtime import FileIntelligenceRuntime
    from athena.core.llm.provider import LLMProvider
    from athena.core.memory.long_term_memory import LongTermMemoryService
    from athena.core.tools.catalog import ToolCatalogService
    from athena.core.tools.manager import UnifiedToolManager
    from athena.core.tools.mcp.manager import MCPManager
    from athena.gateway.approval import ApprovalManager
    from athena.infrastructure.postgre.database import Database
    from athena.core.retrieval.ports import RetrievalTraceReader


@dataclass
class RuntimeContainer:
    """应用运行期间共享的显式依赖集合。"""

    db: Database
    retrieval_trace_reader: RetrievalTraceReader
    event_publisher: RuntimeEventPublisher
    approval_manager: ApprovalManager
    tool_manager: UnifiedToolManager
    tool_catalog: ToolCatalogService
    mcp_manager: MCPManager
    llm: LLMProvider
    file_runtime: FileIntelligenceRuntime
    memory_service: LongTermMemoryService
    agent_store: AgentStore
    realtime_transport: SessionEventBus
    sandbox_runner: SandboxRunner
    workspace_manager: WorkspaceManager


def get_runtime_container(connection: HTTPConnection) -> RuntimeContainer:
    """从 HTTP 连接的应用状态读取已初始化的运行时容器。

    参数:
        connection (HTTPConnection): 当前请求或 WebSocket 连接，必须关联 FastAPI 应用。
    返回值:
        RuntimeContainer: 应用启动阶段注入的运行时依赖集合。
    异常:
        RuntimeError: 应用尚未完成初始化，或状态中的对象不是 ``RuntimeContainer``。
    """
    app = getattr(connection, "app", None)
    state = getattr(app, "state", None)
    runtime = getattr(state, "runtime", None)
    if not isinstance(runtime, RuntimeContainer):
        raise RuntimeError("Application runtime is not initialized")
    return runtime
