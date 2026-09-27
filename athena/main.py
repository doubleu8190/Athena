"""Athena 应用入口 — FastAPI 应用创建、依赖注入、路由注册.

启动流程：
1. 配置 structlog 日志
2. 初始化 PostgreSQL 数据库（建表）
3. 初始化 LLM Provider / 工具管理器 / 审批管理器 / 记忆系统 / 上下文压缩
4. 构建 LangGraph 运行时并编译唯一 Agent 入口
5. 注册 REST API 与 SSE 路由

业务逻辑已剥离至：
- athena.runtime — 命令消费、LangGraph 与恢复协调
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from athena.config.settings import get_settings
from athena.container import RuntimeContainer
from athena.core.llm.provider import LLMProvider
from athena.gateway.auth.middleware import AuthenticationMiddleware
from athena.gateway.approval import ApprovalManager
from athena.gateway.routes import api_router
from athena.infrastructure.postgre.database import Database
from athena.infrastructure.postgre.repositories.agent_store import AgentStore
from athena.infrastructure.sandbox.docker_runner import DockerRunner
from athena.core.sandbox.workspace import WorkspaceManager
from athena.core.tools.spec import ToolRuntime
from athena.runtime import (
    CancellationRegistry,
    CommandConsumer,
    LangGraphRuntime,
    RecoveryReconciler,
    build_graph,
)
from athena.runtime.command_notifications import CommandNotifier
from athena.runtime.transport import RuntimeEventPublisher, SessionEventBus
from athena.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 — 启动初始化与关闭清理。

    启动阶段按依赖顺序初始化所有子系统，关闭阶段按逆序释放资源。
    所有子系统通过 ``app.state.runtime`` 注入到路由层。

    生成值：
        None。yield 前完成初始化，yield 后执行清理。

    异常：
        异常: 任何子系统初始化失败时向上抛出，阻止应用启动。
    """
    settings = get_settings()
    loopback = settings.host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and not settings.auth_enabled:
        raise RuntimeError("AUTH_ENABLED must be true when binding beyond loopback")
    if settings.auth_enabled and (
        not settings.auth_username
        or not settings.auth_password
        or len(settings.auth_session_secret) < 32
    ):
        raise RuntimeError(
            "Authentication requires username, password and a 32+ byte session secret"
        )
    configure_logging(debug=settings.debug)
    logger.info("athena_starting", host=settings.host, port=settings.port)

    # ── 1. 数据库 ──
    db = Database(settings.postgres_url)
    await db.connect(
        pool_size=settings.postgres_pool_size,
        max_overflow=settings.postgres_max_overflow,
    )
    command_notifier = CommandNotifier()
    agent_store = AgentStore(command_notifier=command_notifier)

    # ── 2. Application Event publisher ──
    event_publisher = RuntimeEventPublisher(agent_store)

    # ── 3. 审批管理器（绑定事件发布器与数据库） ──
    approval_manager = ApprovalManager(
        approval_timeout=settings.approval_timeout,
        event_publisher=event_publisher,
        db=db,
        agent_store=agent_store,
    )

    # ── 4. 工具管理器（注册内置工具 + MCP 工具） ──
    from athena.core.tools.catalog import ToolCatalogService
    from athena.core.tools.manager import UnifiedToolManager
    from athena.core.tools.builtin.registry import register_builtin_tools

    sandbox_runner = DockerRunner(settings)
    workspace_manager = WorkspaceManager(settings.sandbox_workspace_root)
    if settings.sandbox_required and not await sandbox_runner.health():
        raise RuntimeError("Docker sandbox is required but unavailable")

    tool_runtime = ToolRuntime(
        workspace_manager=workspace_manager,
        sandbox_runner=sandbox_runner,
        settings=settings,
    )
    tool_manager = UnifiedToolManager(
        approval_manager=approval_manager,
        tool_runtime=tool_runtime,
    )
    tool_catalog = ToolCatalogService(db.tools)
    builtin_tool_names = register_builtin_tools(tool_manager)
    await tool_catalog.reconcile(tool_manager, names=builtin_tool_names)

    # MCP 服务器管理器（恢复已持久化的 MCP 服务端 配置）
    from athena.core.tools.mcp.adapter import MCPToolAdapter
    from athena.core.tools.mcp.manager import MCPManager

    mcp_adapter = MCPToolAdapter(
        tool_manager,
        tool_catalog,
        sandbox_runner=sandbox_runner,
        sandbox_workspace_root=settings.sandbox_workspace_root,
        sandbox_image=settings.sandbox_shell_image,
    )
    mcp_manager = MCPManager(tool_manager=tool_manager, db=db, adapter=mcp_adapter)
    await mcp_manager.load_persisted()

    # ── 5. LLM / 记忆 / 压缩 ──
    llm_primary = LLMProvider.from_primary_settings(settings=settings)
    # 副 Provider：用于检索、摘要、事实提取、压缩等轻量任务
    llm_secondary = (
        LLMProvider.from_secondary_settings(settings=settings) or llm_primary
    )

    # ── 5.1 文件智能 ──
    from athena.core.files.runtime import FileIntelligenceRuntime
    from athena.core.files.reranking import CrossEncoderFileReranker
    from athena.infrastructure.chroma.file_vector_store import ChromaFileVectorStore
    from athena.infrastructure.chroma.embedding import embedding_config_from_settings
    from athena.core.tools.catalog import ToolRegistry
    from athena.core.tools.providers.files import build_file_tool_specs

    file_vector_store = ChromaFileVectorStore(
        path=str(settings.chroma_path),
        embedding_config=embedding_config_from_settings(settings),
    )
    file_reranker = None
    if settings.file_rerank_enabled:
        file_reranker = CrossEncoderFileReranker(
            settings.file_rerank_model,
            batch_size=settings.file_rerank_batch_size,
            max_chars=settings.file_rerank_max_chars,
            device=settings.file_rerank_device,
        )
        try:
            await file_reranker.initialize()
        except Exception:
            if settings.file_rerank_required:
                raise
            logger.exception("file_reranker_initialization_failed_fallback_enabled")
            file_reranker = None
    file_runtime = FileIntelligenceRuntime(
        db.files,
        llm_primary,
        llm_secondary,
        settings=settings,
        event_publisher=event_publisher,
        trace_writer=db.retrieval,
        vector_store=file_vector_store,
        file_token_counter=file_vector_store.token_counter,
        reranker=file_reranker,
    )
    await file_runtime.initialize()

    from athena.core.files.knowledge_document_worker import KnowledgeDocumentWorker
    from athena.infrastructure.postgre.repositories.knowledge_document_job_repository import (
        KnowledgeDocumentJobRepository,
    )

    knowledge_document_worker = KnowledgeDocumentWorker(
        KnowledgeDocumentJobRepository(), file_runtime
    )

    # 注册文件能力工具到工具管理器
    tool_registry = ToolRegistry(tool_manager, tool_catalog)
    tool_specs = build_file_tool_specs(file_runtime)
    await tool_registry.install(tool_specs)

    # ── 5.2 记忆系统 ──
    from athena.core.memory.long_term_memory import LongTermMemoryService

    from athena.infrastructure.chroma.memory_vector_store import ChromaMemoryVectorStore
    from athena.infrastructure.postgre.repositories.memory_repository import (
        PostgresMemoryRepository,
    )
    from athena.infrastructure.postgre.repositories.memory_job_repository import (
        MemoryJobRepository,
    )
    from athena.core.memory.write_job_worker import MemoryWriteJobWorker

    memory_service = LongTermMemoryService(
        settings=settings,
        repository=PostgresMemoryRepository(),
        vector_store=ChromaMemoryVectorStore(
            path=str(settings.chroma_path),
            embedding_config=embedding_config_from_settings(settings),
        ),
    )
    try:
        await memory_service.initialize()
    except Exception as e:
        logger.warning("memory_init_skipped", error=str(e))
        raise e

    # 后台看门狗：周期 flush 访问统计 + 清理过期记忆
    memory_flush_task = asyncio.create_task(memory_service.run_periodic_flush())

    from athena.core.memory.retrieval import (
        HybridMemoryRetriever,
        MemoryRetrievalService,
    )

    retrieval_manager = HybridMemoryRetriever(
        memory_service,
        settings=settings,
        trace_writer=db.retrieval,
    )
    memory_retrieval = MemoryRetrievalService(retrieval_manager, llm_primary, settings)

    from athena.core.memory.distillation import LongTermMemorySummarizer

    long_term_memory_summarizer = LongTermMemorySummarizer(
        llm_secondary, memory_service, settings=settings
    )

    from athena.core.memory.distillation import FactExtractor

    fact_extractor = FactExtractor(llm_secondary)
    memory_job_repository = MemoryJobRepository()
    memory_job_worker = MemoryWriteJobWorker(
        memory_job_repository,
        # Workflow is created by LangGraphRuntime; worker is attached below.
        None,
    )

    # ── 5.3 上下文压缩 ──
    from athena.core.compression.compressor import ContextCompressor
    from athena.core.llm.tokens import ModelTokenCounter

    compressor = ContextCompressor(
        llm=llm_secondary,
        token_counter=ModelTokenCounter(llm_secondary.model),
        db=db,
        settings=settings,
    )

    # ── 6. LangGraph 运行时（唯一 Agent 编排入口） ──
    graph_runtime = LangGraphRuntime(
        llm=llm_primary,
        tool_manager=tool_manager,
        db=db,
        event_publisher=event_publisher,
        compressor=compressor,
        memory_retrieval=memory_retrieval,
        long_term_memory_summarizer=long_term_memory_summarizer,
        fact_extractor=fact_extractor,
        memory_service=memory_service,
        settings=settings,
        file_runtime=file_runtime,
        memory_job_repository=memory_job_repository,
        agent_store=agent_store,
    )
    # Access the runtime property so the workflow is constructed before the
    # worker starts claiming queued turns.  The backing attribute is lazily
    # initialized and is None during runtime construction.
    memory_job_worker.configure_workflow(graph_runtime.fact_memory_write_workflow)
    await memory_job_worker.start()
    # 注册子 Agent 派生工具到工具管理器
    await tool_registry.install(graph_runtime.build_delegation_tool_specs())

    # ── 6.1 RuntimeContainer（路由层依赖注入容器） ──
    realtime_transport = SessionEventBus()
    agent_store.transport = realtime_transport
    app.state.runtime = RuntimeContainer(
        db=db,
        retrieval_trace_reader=db.retrieval,
        event_publisher=event_publisher,
        approval_manager=approval_manager,
        tool_manager=tool_manager,
        tool_catalog=tool_catalog,
        mcp_manager=mcp_manager,
        llm=llm_primary,
        file_runtime=file_runtime,
        memory_service=memory_service,
        agent_store=agent_store,
        realtime_transport=realtime_transport,
        sandbox_runner=sandbox_runner,
        workspace_manager=workspace_manager,
    )

    # 目的是将没来得及取消的run，在重启的时候取消掉
    await RecoveryReconciler(
        app.state.runtime.agent_store,
        orchestration=db.orchestration,
    ).reconcile()

    # 初始化 PostgreSQL checkpointer，确保 LangGraph 的状态可以在中断后恢复。
    checkpointer_context = AsyncPostgresSaver.from_conn_string(
        settings.postgres_conn_string
    )
    checkpointer = await checkpointer_context.__aenter__()
    await checkpointer.setup()

    graph = build_graph(graph_runtime, checkpointer)
    command_consumer = CommandConsumer(
        app.state.runtime.agent_store,
        notifier=command_notifier,
        graph=graph,
        cancellation_registry=CancellationRegistry(),
        memory_service=memory_service,
        approval_manager=approval_manager,
        sandbox_runner=sandbox_runner,
    )
    await command_consumer.start()
    # 所有运行时依赖和路由容器均就绪后，再开始领取持久化知识库任务。
    await knowledge_document_worker.start()

    logger.info("athena_started")
    yield

    # ── 关闭清理（按初始化逆序释放资源） ──
    logger.info("athena_shutting_down")
    await knowledge_document_worker.stop()
    try:
        await command_consumer.stop()
    except Exception as e:
        logger.warning("command_consumer_shutdown_failed", error=str(e))
    await memory_job_worker.stop()
    await checkpointer_context.__aexit__(None, None, None)
    # 关闭不会取消持久化审批。审批记录和未完成工具尝试需要跨进程保留，
    # 下次启动后由用户手动恢复任务，再按 DB 状态决定等待或执行。
    # 停掉后台看门狗，并落盘内存中未同步的访问统计
    memory_flush_task.cancel()
    try:
        await memory_flush_task
    except asyncio.CancelledError:
        pass
    try:
        await memory_service.flush_access_stats()
    except Exception as e:
        logger.warning("memory_final_flush_failed", error=str(e))
    # 断开所有 MCP 服务端 连接，防子进程泄漏
    try:
        await mcp_manager.shutdown()
    except Exception as e:
        logger.warning("mcp_shutdown_failed", error=str(e))
    await sandbox_runner.close_all()
    await db.close()
    logger.info("athena_stopped")


# ---------------------------------------------------------------------------
# FastAPI 应用（仅创建、中间件、路由注册）
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Athena",
    description="自主 AI Agent 桌面应用",
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(AuthenticationMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 REST API 路由（/api/*）；事件通过 SSE 提供。
app.include_router(api_router)


def run() -> None:
    """启动 uvicorn 服务器（命令行入口）。

    debug 模式下启用热重载，但只监听 athena 源码目录，
    避免内置工具写入项目文件或 pip install 触发误重启。
    """
    import uvicorn

    settings = get_settings()
    if (
        settings.host not in {"127.0.0.1", "localhost", "::1"}
        and settings.auth_enabled is False
    ):
        raise RuntimeError("AUTH_ENABLED must be true when binding beyond loopback")
    if settings.host not in {"127.0.0.1", "localhost", "::1"} and (
        len(settings.auth_session_secret) < 32
        or not settings.auth_username
        or not settings.auth_password
    ):
        raise RuntimeError(
            "LAN binding requires AUTH_USERNAME, AUTH_PASSWORD and a 32+ byte AUTH_SESSION_SECRET"
        )
    # reload 模式只监听源码目录，避免误重启：
    # 内置工具(write_file / exec_shell)会向项目根目录写 .py 产物
    # （如 create_paper.py），pip install 也会向 .venv 写 .py；
    # 若按 uvicorn 默认监视整个 CWD，这些写入都会触发热重启。
    reload_dirs = [str(Path(__file__).resolve().parent)] if settings.debug else None
    uvicorn.run(
        "athena.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        reload_dirs=reload_dirs,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    run()
