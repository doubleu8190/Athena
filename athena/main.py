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
- services/recovery.py — 会话恢复
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from athena.config.settings import get_settings
from athena.db.database import close_database, get_database
from athena.gateway.approval import get_approval_manager, set_approval_manager
from athena.gateway.routes import api_router
from athena.gateway.routes._runtime import set_workflow
from athena.gateway.ws.handler import websocket_endpoint
from athena.gateway.ws.manager import get_websocket_manager, set_websocket_manager
from athena.gateway.recovery import recover_interrupted_sessions
from athena.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 - 启动初始化与关闭清理."""
    settings = get_settings()
    configure_logging(debug=settings.debug)
    logger.info("athena_starting", host=settings.host, port=settings.port)

    # 1. 数据库
    db = await get_database(settings.sqlite_db_path)

    # 2. WebSocket 管理器
    ws_manager = get_websocket_manager()
    set_websocket_manager(ws_manager)

    # 3. 审批管理器（绑定 ws + db）
    approval_manager = get_approval_manager(
        approval_timeout=settings.approval_timeout,
        websocket_manager=ws_manager,
        db=db,
    )
    approval_manager.set_websocket_manager(ws_manager)
    approval_manager.set_db(db)
    set_approval_manager(approval_manager)

    # 4. 工具管理器（注册内置工具）
    from athena.core.tools.manager import UnifiedToolManager, set_tool_manager
    from athena.core.tools.builtin.registry import register_builtin_tools
    tool_manager = UnifiedToolManager(approval_manager=approval_manager)
    register_builtin_tools(tool_manager)
    set_tool_manager(tool_manager)

    # 5. LLM / 记忆 / 压缩
    from athena.core.llm.provider import get_llm_provider
    llm = get_llm_provider(settings)

    from athena.core.memory.memory import MemoryManager
    from athena.core.memory.retrieval import HybridRetrievalManager, MemoryRetrievalService
    from athena.core.memory.summarizer import ConversationSummarizer
    memory_manager = MemoryManager(settings=settings)
    try:
        await memory_manager.initialize()
    except Exception as e:
        logger.warning("memory_init_skipped", error=str(e))
        memory_manager = None  # type: ignore

    retrieval_manager = (
        HybridRetrievalManager(llm, memory_manager, settings=settings)
        if memory_manager is not None
        else None
    )
    memory_retrieval = (
        MemoryRetrievalService(retrieval_manager) if retrieval_manager is not None else None
    )
    conversation_summarizer = (
        ConversationSummarizer(llm, memory_manager, settings=settings)
        if memory_manager is not None
        else None
    )

    from athena.core.compression.compressor import ContextCompressor
    compressor = ContextCompressor(llm=llm, settings=settings)

    # 6. AgentWorkflow
    from athena.core.agent.workflow import AgentWorkflow
    workflow = AgentWorkflow(
        llm=llm,
        tool_manager=tool_manager,
        db=db,
        ws_manager=ws_manager,
        compressor=compressor,
        memory_retrieval=memory_retrieval,
        conversation_summarizer=conversation_summarizer,
        settings=settings,
    )
    set_workflow(workflow)

    # 7. 被动会话恢复（project_memory 约束：通知用户 → 等待确认 → 执行恢复）
    await recover_interrupted_sessions(db)

    logger.info("athena_started")
    yield

    # 关闭清理
    logger.info("athena_shutting_down")
    try:
        await approval_manager.cancel_all_pending("")
    except Exception:
        pass
    await close_database()
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.websocket("/ws/{session_id}")(websocket_endpoint)


def run() -> None:
    """启动 uvicorn 服务器（命令行入口）."""
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "athena.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    run()
