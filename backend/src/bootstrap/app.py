"""迁移期 FastAPI 应用工厂。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from application.sessions import SessionService
from application.runs import RunQueryService
from application.runs import RunCommandService
from application.sessions import SessionMessageQueryService
from interfaces.http.controllers.sessions_controller import (
    build_sessions_router,
)
from interfaces.http.controllers.query_controller import build_query_router
from application.common.query_services import (
    MemoryQueryService,
    ProviderQueryService,
    RetrievalQueryService,
    SettingsQueryService,
)
from application.knowledge import KnowledgeBaseQueryService
from application.tools import ToolQueryService
from application.tools import ToolService, MCPService
from application.approval import ApprovalService
from application.memory import MemoryService
from application.files import AttachmentService
from interfaces.http.controllers.files_controller import build_files_router
from application.knowledge import KnowledgeBaseService
from interfaces.http.controllers.knowledge_controller import build_knowledge_router
from interfaces.http.controllers.runs_controller import build_runs_router
from application.events import EventStreamService
from interfaces.http.sse.events import build_events_router
from interfaces.http.controllers.approval_controller import build_approval_router
from interfaces.http.controllers.tools_controller import build_tools_router
from interfaces.http.controllers.mcp_controller import build_mcp_router
from interfaces.http.auth import AuthenticationMiddleware, build_auth_router
from .dependencies import ApplicationDependencies, Provider, build_default_dependencies
from .lifespan import lifespan


def create_app(
    *,
    dependencies: ApplicationDependencies | None = None,
    session_service_factory: Callable[[], SessionService] | None = None,
    message_query_factory: Callable[[], SessionMessageQueryService] | None = None,
    run_query_factory: Callable[[], RunQueryService] | None = None,
    run_command_factory: Callable[[], RunCommandService] | None = None,
    event_stream_factory: Callable[[], EventStreamService] | None = None,
    tool_query_factory: Callable[[], ToolQueryService] | None = None,
    tool_service_factory: Callable[[], ToolService] | None = None,
    approval_service_factory: Callable[[], ApprovalService] | None = None,
    mcp_service_factory: Callable[[], MCPService] | None = None,
    provider_query_factory: Callable[[], ProviderQueryService] | None = None,
    settings_query_factory: Callable[[], SettingsQueryService] | None = None,
    memory_query_factory: Callable[[], MemoryQueryService] | None = None,
    memory_service_factory: Callable[[], MemoryService] | None = None,
    retrieval_query_factory: Callable[[], RetrievalQueryService] | None = None,
    knowledge_query_factory: Callable[[], KnowledgeBaseQueryService] | None = None,
    attachment_service_factory: Callable[[], AttachmentService] | None = None,
    knowledge_service_factory: Callable[[], KnowledgeBaseService] | None = None,
) -> FastAPI:
    """创建无外部副作用的迁移期应用。

    参数：
        session_service_factory: 可选的会话服务工厂。提供后才注册会话路由，
            由调用方负责组装 repository 和其他运行时依赖。
    返回值：
        FastAPI: 已注册健康检查和可选会话 Controller 的应用实例。
    异常：
        本函数不主动抛出业务异常；依赖工厂异常会在请求处理时传播。
    """
    graph = dependencies or build_default_dependencies()
    # Explicit test/deployment providers take precedence over the paired
    # mutation service for the same read endpoint.
    explicit_tool_query = tool_query_factory is not None
    explicit_tool_service = tool_service_factory is not None
    explicit_memory_query = memory_query_factory is not None
    app = FastAPI(
        title="Athena",
        version="0.1.0.dev0",
        lifespan=lambda application: lifespan(application, graph.resources),
    )
    app.add_middleware(AuthenticationMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.dependencies = graph

    def registered(name: str, explicit: Provider | None) -> Provider | None:
        return explicit if explicit is not None else graph.provider(name)

    session_service_factory = registered("session_service_factory", session_service_factory)
    message_query_factory = registered("message_query_factory", message_query_factory)
    run_query_factory = registered("run_query_factory", run_query_factory)
    run_command_factory = registered("run_command_factory", run_command_factory)
    event_stream_factory = registered("event_stream_factory", event_stream_factory)
    tool_query_factory = registered("tool_query_factory", tool_query_factory)
    tool_service_factory = registered("tool_service_factory", tool_service_factory)
    approval_service_factory = registered("approval_service_factory", approval_service_factory)
    mcp_service_factory = registered("mcp_service_factory", mcp_service_factory)
    provider_query_factory = registered("provider_query_factory", provider_query_factory)
    settings_query_factory = registered("settings_query_factory", settings_query_factory)
    memory_query_factory = registered("memory_query_factory", memory_query_factory)
    memory_service_factory = registered("memory_service_factory", memory_service_factory)
    retrieval_query_factory = registered("retrieval_query_factory", retrieval_query_factory)
    knowledge_query_factory = registered("knowledge_query_factory", knowledge_query_factory)
    attachment_service_factory = registered("attachment_service_factory", attachment_service_factory)
    knowledge_service_factory = registered("knowledge_service_factory", knowledge_service_factory)
    if explicit_tool_query:
        tool_service_factory = None
    if explicit_tool_service:
        tool_query_factory = None
    if explicit_memory_query:
        memory_service_factory = None

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, Any]:
        """返回迁移期应用的进程健康状态。"""
        return {"status": "ok", "architecture": "restructured"}

    @app.get("/api/health", tags=["health"])
    async def api_health() -> dict[str, Any]:
        return {"status": "ok", "architecture": "restructured"}

    app.include_router(build_auth_router(), prefix="/api")

    if session_service_factory is not None:
        app.include_router(
            build_sessions_router(
                session_service_factory,
                message_query_factory=message_query_factory,
                run_query_factory=run_query_factory,
            ),
            prefix="/api",
        )
    if any(
        factory is not None
        for factory in (
            tool_query_factory,
            provider_query_factory,
            settings_query_factory,
            memory_query_factory,
            memory_service_factory,
            retrieval_query_factory,
            knowledge_query_factory,
        )
    ):
        app.include_router(
            build_query_router(
                tool_factory=tool_query_factory,
                provider_factory=provider_query_factory,
                settings_factory=settings_query_factory,
                memory_factory=memory_query_factory,
                memory_service_factory=memory_service_factory,
                retrieval_factory=retrieval_query_factory,
                knowledge_factory=knowledge_query_factory,
                include_tools=tool_service_factory is None,
            ),
            prefix="/api",
        )
    if attachment_service_factory is not None:
        app.include_router(build_files_router(attachment_service_factory), prefix="/api")
    if knowledge_service_factory is not None:
        app.include_router(build_knowledge_router(knowledge_service_factory), prefix="/api")
    if tool_service_factory is not None:
        app.include_router(build_tools_router(tool_service_factory), prefix="/api")
    if approval_service_factory is not None:
        app.include_router(build_approval_router(approval_service_factory), prefix="/api")
    if mcp_service_factory is not None:
        app.include_router(build_mcp_router(mcp_service_factory), prefix="/api")
    if run_command_factory is not None:
        app.include_router(build_runs_router(run_command_factory), prefix="/api")
    if event_stream_factory is not None:
        app.include_router(build_events_router(event_stream_factory), prefix="/api")
    for router in graph.routers:
        app.include_router(router)
    return app
