"""FastAPI 应用工厂。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from application.sessions import SessionService
from application.auth import AuthenticationConfig, AuthenticationService
from application.runs import RunQueryService
from application.runs import RunService
from application.sessions import SessionMessageQueryService
from interfaces.http.controllers.sessions_controller import (
    build_sessions_router,
)
from interfaces.http.controllers.query_controller import build_query_router
from application.configuration import ProviderQueryService, SettingsQueryService
from application.knowledge import KnowledgeBaseQueryService
from application.tools import ToolQueryService
from application.tools import ToolService, MCPService
from application.approval import ApprovalService
from application.orchestration import OrchestrationQueryService
from application.memory import (
    MemoryEpisodeManagementService,
    MemoryJobManagementService,
    MemoryQueryService,
    MemoryService,
    SessionMemorySettingsService,
)
from application.projects import ProjectService
from application.retrieval import RetrievalQueryService
from application.files import AttachmentService
from interfaces.http.controllers.files_controller import build_files_router
from application.knowledge import KnowledgeBaseService
from interfaces.http.controllers.knowledge_controller import build_knowledge_router
from interfaces.http.controllers.runs_controller import build_run_query_router, build_runs_router
from interfaces.http.controllers.orchestration_controller import build_orchestration_router
from application.events import EventStreamService
from interfaces.http.sse.events import build_events_router
from interfaces.http.controllers.approval_controller import build_approval_router
from interfaces.http.controllers.tools_controller import build_tools_router
from interfaces.http.controllers.mcp_controller import build_mcp_router
from interfaces.http.controllers.evaluation_controller import build_evaluation_router
from interfaces.http.auth import AuthOptions, AuthenticationMiddleware, build_auth_router
from interfaces.http.auth import middleware as auth_middleware
from interfaces.http.worker_protocol import install_worker_protocol_handlers
from .dependencies import ApplicationDependencies, build_default_dependencies
from .lifespan import lifespan


def create_app(
    *,
    dependencies: ApplicationDependencies | None = None,
    session_service: SessionService | None = None,
    session_memory_settings: SessionMemorySettingsService | None = None,
    project_service: ProjectService | None = None,
    message_query_service: SessionMessageQueryService | None = None,
    run_query_service: RunQueryService | None = None,
    run_service: RunService | None = None,
    orchestration_query_service: OrchestrationQueryService | None = None,
    event_stream_service: EventStreamService | None = None,
    tool_query_service: ToolQueryService | None = None,
    tool_service: ToolService | None = None,
    approval_service: ApprovalService | None = None,
    mcp_service: MCPService | None = None,
    provider_query_service: ProviderQueryService | None = None,
    settings_query_service: SettingsQueryService | None = None,
    memory_query_service: MemoryQueryService | None = None,
    memory_service: MemoryService | None = None,
    memory_episode_service: MemoryEpisodeManagementService | None = None,
    memory_job_service: MemoryJobManagementService | None = None,
    retrieval_query_service: RetrievalQueryService | None = None,
    knowledge_query_service: KnowledgeBaseQueryService | None = None,
    attachment_service: AttachmentService | None = None,
    knowledge_service: KnowledgeBaseService | None = None,
    evaluation_repository: Any | None = None,
    evaluation_target_factory: Callable[[Any], Any] | None = None,
    evaluation_dataset_loader: Callable[[str], Any] | None = None,
    evaluation_event_sink: Any | None = None,
    evaluation_progress_hub: Any | None = None,
    auth_options: AuthOptions | None = None,
    auth_service: AuthenticationService | None = None,
) -> FastAPI:
    """创建无外部副作用的 Athena 应用。

    参数：
        session_service: 可选的会话服务实例。提供后才注册会话路由；
            未显式传入时使用依赖图中的实例。
    返回值：
        FastAPI: 已注册健康检查和可选会话 Controller 的应用实例。
    异常：
        本函数不主动抛出业务异常；路由和生命周期资源的异常按原样传播。
    """
    graph = dependencies or build_default_dependencies()
    resolved_auth_options = auth_options or AuthOptions.from_settings(auth_middleware.get_settings())
    resolved_auth_service = auth_service or AuthenticationService(AuthenticationConfig(
        enabled=resolved_auth_options.enabled,
        username=resolved_auth_options.username,
        password=resolved_auth_options.password,
        session_secret=resolved_auth_options.session_secret,
        session_ttl_seconds=resolved_auth_options.session_ttl_seconds,
    ))
    # Explicit test/deployment services take precedence over the paired
    # mutation service for the same read endpoint.
    explicit_tool_query = tool_query_service is not None
    explicit_tool_service = tool_service is not None
    explicit_memory_query = memory_query_service is not None
    app = FastAPI(
        title="Athena",
        version="0.1.0.dev0",
        lifespan=lambda application: lifespan(application, graph.resources),
    )
    install_worker_protocol_handlers(app)
    app.add_middleware(AuthenticationMiddleware, options=resolved_auth_options, auth_service=resolved_auth_service)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.dependencies = graph

    def registered(name: str, explicit: Any | None) -> Any | None:
        return explicit if explicit is not None else graph.service(name)

    session_service = registered("session_service", session_service)
    session_memory_settings = registered(
        "session_memory_settings", session_memory_settings
    )
    project_service = registered("project_service", project_service)
    message_query_service = registered("message_query_service", message_query_service)
    run_query_service = registered("run_query_service", run_query_service)
    run_service = registered("run_service", run_service)
    orchestration_query_service = registered(
        "orchestration_query_service", orchestration_query_service
    )
    event_stream_service = registered("event_stream_service", event_stream_service)
    tool_query_service = registered("tool_query_service", tool_query_service)
    tool_service = registered("tool_service", tool_service)
    approval_service = registered("approval_service", approval_service)
    mcp_service = registered("mcp_service", mcp_service)
    provider_query_service = registered("provider_query_service", provider_query_service)
    settings_query_service = registered("settings_query_service", settings_query_service)
    memory_query_service = registered("memory_query_service", memory_query_service)
    memory_service = registered("memory_service", memory_service)
    memory_episode_service = registered(
        "memory_episode_service", memory_episode_service
    )
    memory_job_service = registered(
        "memory_job_service", memory_job_service
    )
    retrieval_query_service = registered("retrieval_query_service", retrieval_query_service)
    knowledge_query_service = registered("knowledge_query_service", knowledge_query_service)
    attachment_service = registered("attachment_service", attachment_service)
    knowledge_service = registered("knowledge_service", knowledge_service)
    evaluation_repository = registered("evaluation_repository", evaluation_repository)
    evaluation_target_factory = registered("evaluation_target_factory", evaluation_target_factory)
    evaluation_dataset_loader = registered("evaluation_dataset_loader", evaluation_dataset_loader)
    evaluation_event_sink = registered("evaluation_event_sink", evaluation_event_sink)
    evaluation_progress_hub = registered("evaluation_progress_hub", evaluation_progress_hub)
    if explicit_tool_query:
        tool_service = None
    if explicit_tool_service:
        tool_query_service = None
    if explicit_memory_query:
        memory_service = None

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, Any]:
        """返回应用的进程健康状态。"""
        return {"status": "ok", "architecture": "restructured"}

    @app.get("/api/health", tags=["health"])
    async def api_health() -> dict[str, Any]:
        return {"status": "ok", "architecture": "restructured"}

    app.include_router(build_auth_router(options=resolved_auth_options, auth_service=resolved_auth_service), prefix="/api")

    if session_service is not None:
        app.include_router(
            build_sessions_router(
                session_service,
                memory_settings_service=session_memory_settings,
                message_query_service=message_query_service,
                run_query_service=run_query_service,
            ),
            prefix="/api",
        )
    if any(
        service is not None
        for service in (
            tool_query_service,
            provider_query_service,
            settings_query_service,
            memory_query_service,
            memory_service,
            memory_episode_service,
            memory_job_service,
            retrieval_query_service,
            knowledge_query_service,
            project_service,
        )
    ):
        app.include_router(
            build_query_router(
                tool_query_service=tool_query_service,
                provider_query_service=provider_query_service,
                settings_query_service=settings_query_service,
                memory_query_service=memory_query_service,
                memory_service=memory_service,
                memory_episode_service=memory_episode_service,
                memory_job_service=memory_job_service,
                project_service=project_service,
                retrieval_query_service=retrieval_query_service,
                knowledge_query_service=knowledge_query_service,
                include_tools=tool_service is None,
            ),
            prefix="/api",
        )
    if attachment_service is not None:
        app.include_router(build_files_router(attachment_service), prefix="/api")
    if knowledge_service is not None:
        app.include_router(build_knowledge_router(knowledge_service), prefix="/api")
    if evaluation_repository is not None:
        app.include_router(build_evaluation_router(
            repository=evaluation_repository,
            target_factory=evaluation_target_factory,
            dataset_loader=evaluation_dataset_loader,
            event_sink=evaluation_event_sink,
            progress_hub=evaluation_progress_hub,
        ), prefix="/api")
    if tool_service is not None:
        app.include_router(build_tools_router(tool_service), prefix="/api")
    if approval_service is not None:
        app.include_router(build_approval_router(approval_service), prefix="/api")
    if mcp_service is not None:
        app.include_router(build_mcp_router(mcp_service), prefix="/api")
    if run_service is not None:
        app.include_router(build_runs_router(run_service), prefix="/api")
    if run_query_service is not None:
        app.include_router(build_run_query_router(run_query_service), prefix="/api")
    if orchestration_query_service is not None:
        app.include_router(
            build_orchestration_router(orchestration_query_service), prefix="/api"
        )
    if event_stream_service is not None:
        app.include_router(build_events_router(event_stream_service), prefix="/api")
    for router in graph.routers:
        app.include_router(router)
    return app
