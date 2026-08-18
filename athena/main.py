"""Athena 应用入口 — FastAPI 应用创建、依赖注入、路由注册.

启动流程：
1. 配置 structlog 日志
2. 初始化 SQLite 数据库（建表）
3. 初始化 LLM Provider / 工具管理器 / 审批管理器 / 记忆系统 / 上下文压缩
4. 构建 AgentWorkflow 并注入路由运行时
5. 注册 REST API 路由与 WebSocket 端点
6. 启动时执行被动会话恢复（检测 interrupted 会话并通知用户）

业务逻辑已剥离至：
- gateway/ws/handler.py — WebSocket 端点与消息处理
- gateway/recovery.py — 会话恢复
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from athena.config.settings import get_settings
from athena.core.llm.provider import LLMProvider
from athena.infrastructure.sqlite.database import Database
from athena.runtime import RuntimeContainer

from athena.gateway.approval import ApprovalManager
from athena.gateway.routes import api_router

from athena.gateway.ws.handler import router as websocket_router
from athena.gateway.ws.manager import WebSocketManager
from athena.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 — 启动初始化与关闭清理。

    启动阶段按依赖顺序初始化所有子系统，关闭阶段按逆序释放资源。
    所有子系统通过 ``app.state.runtime`` 注入到路由层。

    Yields:
        None。yield 前完成初始化，yield 后执行清理。

    Raises:
        Exception: 任何子系统初始化失败时向上抛出，阻止应用启动。
    """
    settings = get_settings()
    configure_logging(debug=settings.debug)
    logger.info("athena_starting", host=settings.host, port=settings.port)

    # ── 1. 数据库 ──
    db = Database(settings.sqlite_db_path)
    await db.connect()

    # ── 2. WebSocket 管理器 ──
    ws_manager = WebSocketManager()

    # ── 3. 审批管理器（绑定 ws + db） ──
    approval_manager = ApprovalManager(
        approval_timeout=settings.approval_timeout,
        websocket_manager=ws_manager,
        db=db,
    )

    # ── 4. 工具管理器（注册内置工具 + MCP 工具） ──
    from athena.core.tools.catalog import ToolCatalogService
    from athena.core.tools.manager import UnifiedToolManager
    from athena.core.tools.builtin.registry import register_builtin_tools

    tool_manager = UnifiedToolManager(approval_manager=approval_manager)
    tool_catalog = ToolCatalogService(db.tools)
    builtin_tool_names = register_builtin_tools(tool_manager)
    await tool_catalog.reconcile(tool_manager, names=builtin_tool_names)

    # MCP 服务器管理器（恢复已持久化的 MCP Server 配置）
    from athena.core.tools.mcp.adapter import MCPToolAdapter
    from athena.core.tools.mcp.manager import MCPManager

    mcp_adapter = MCPToolAdapter(tool_manager, tool_catalog)
    mcp_manager = MCPManager(tool_manager=tool_manager, db=db, adapter=mcp_adapter)
    await mcp_manager.load_persisted()

    # ── 5. LLM / 记忆 / 压缩 ──
    llm_primary = LLMProvider.from_primary_settings(settings=settings)
    # 副 Provider：用于检索、摘要、事实提取、压缩等轻量任务
    llm_secondary = (
        LLMProvider.from_secondary_settings(settings=settings) or llm_primary
    )

    # ── 5.1 File Intelligence ──
    from athena.core.files.runtime import FileIntelligenceRuntime
    from athena.core.files.tasks import FileTaskWorker
    from athena.core.tools.catalog import ToolRegistry
    from athena.core.tools.providers.files import build_file_tool_specs

    file_runtime = FileIntelligenceRuntime(
        db.files, llm_primary, llm_secondary, settings=settings, ws_manager=ws_manager
    )
    await file_runtime.initialize()

    # 注册文件能力工具到工具管理器
    tool_registry = ToolRegistry(tool_manager, tool_catalog)
    toolSpecList: list = build_file_tool_specs(file_runtime)
    await tool_registry.install(toolSpecList)

    # 启动文件任务 Worker（后台消费解析/索引/摘要任务队列）
    file_worker = FileTaskWorker(file_runtime)
    file_runtime.set_task_enqueuer(file_worker.enqueue_task)

    # ── 5.2 记忆系统 ──
    from athena.core.memory.memory import MemoryManager

    from athena.infrastructure.chroma.memory_store import ChromaMemoryStore
    from athena.infrastructure.sqlite.memory_repository import SqliteMemoryRepository

    memory_manager = MemoryManager(
        settings=settings,
        repository=SqliteMemoryRepository(),
        vector_store=ChromaMemoryStore(path=str(settings.chroma_path)),
    )
    try:
        await memory_manager.initialize()
    except Exception as e:
        logger.warning("memory_init_skipped", error=str(e))
        raise e

    # 后台看门狗：周期 flush 访问统计 + 清理过期记忆
    memory_flush_task = asyncio.create_task(memory_manager.run_periodic_flush())

    from athena.core.memory.retrieval import (
        HybridRetrievalManager,
        MemoryRetrievalService,
    )

    retrieval_manager = HybridRetrievalManager(
        llm_secondary, memory_manager, settings=settings
    )
    memory_retrieval = MemoryRetrievalService(
        retrieval_manager, llm_primary, settings
    )

    from athena.core.memory.summarizer import ConversationSummarizer

    conversation_summarizer = ConversationSummarizer(
        llm_secondary, memory_manager, settings=settings
    )

    from athena.core.memory.summarizer import FactExtractor

    fact_extractor = FactExtractor(llm_secondary)

    # ── 5.3 上下文压缩 ──
    from athena.core.compression.compressor import ContextCompressor

    compressor = ContextCompressor(
        llm=llm_secondary,
        token_counter=llm_primary,
        db=db,
        settings=settings,
    )

    # ── 6. AgentWorkflow（核心编排入口） ──
    from athena.core.agent.workflow import AgentWorkflow

    workflow = AgentWorkflow(
        llm=llm_primary,
        tool_manager=tool_manager,
        db=db,
        ws_manager=ws_manager,
        compressor=compressor,
        memory_retrieval=memory_retrieval,
        conversation_summarizer=conversation_summarizer,
        fact_extractor=fact_extractor,
        memory_manager=memory_manager,
        settings=settings,
    )
    # 注册子 Agent 派生工具到工具管理器
    await tool_registry.install(workflow.delegation_tool_specs())
    file_worker.set_continuation_callback(workflow.resume_file_continuation)
    await file_worker.start()

    # ── 6.1 RuntimeContainer（路由层依赖注入容器） ──
    app.state.runtime = RuntimeContainer(
        db=db,
        websocket_manager=ws_manager,
        approval_manager=approval_manager,
        tool_manager=tool_manager,
        tool_catalog=tool_catalog,
        mcp_manager=mcp_manager,
        llm=llm_primary,
        file_runtime=file_runtime,
        file_worker=file_worker,
        memory_manager=memory_manager,
        workflow=workflow,
    )

    # ── 7. 被动会话恢复 ──
    from athena.gateway.recovery import recover_interrupted_sessions

    await recover_interrupted_sessions(db)

    logger.info("athena_started")
    yield

    # ── 关闭清理（按初始化逆序释放资源） ──
    logger.info("athena_shutting_down")
    try:
        await file_worker.stop()
    except Exception as e:
        logger.warning("file_worker_shutdown_failed", error=str(e))
    try:
        await approval_manager.cancel_all_pending("")
    except Exception as e:
        logger.warning("approval_cancel_pending_failed", error=str(e))
    # 停掉后台看门狗，并落盘内存中未同步的访问统计
    memory_flush_task.cancel()
    try:
        await memory_flush_task
    except asyncio.CancelledError:
        pass
    try:
        await memory_manager.flush_access_stats()
    except Exception as e:
        logger.warning("memory_final_flush_failed", error=str(e))
    # 断开所有 MCP Server 连接，防子进程泄漏
    try:
        await mcp_manager.shutdown()
    except Exception as e:
        logger.warning("mcp_shutdown_failed", error=str(e))
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

# 允许所有来源的 CORS 请求（开发环境）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 REST API 路由（/api/*）和 WebSocket 端点（/ws）
app.include_router(api_router)
app.include_router(websocket_router)


def run() -> None:
    """启动 uvicorn 服务器（命令行入口）。

    debug 模式下启用热重载，但只监听 athena 源码目录，
    避免内置工具写入项目文件或 pip install 触发误重启。
    """
    import uvicorn

    settings = get_settings()
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
