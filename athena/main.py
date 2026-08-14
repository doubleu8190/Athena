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

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from athena.config.settings import get_settings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import set_memory_manager
from athena.db.database import close_database, get_database

from athena.gateway.approval import ApprovalManager
from athena.gateway.routes import api_router

from athena.gateway.ws.handler import router as websocket_router
from athena.gateway.ws.manager import WebSocketManager
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
    from athena.gateway.ws.manager import set_websocket_manager

    ws_manager = WebSocketManager()
    set_websocket_manager(ws_manager)

    # 3. 审批管理器（绑定 ws + db）
    from athena.gateway.approval import set_approval_manager

    approval_manager = ApprovalManager(
        approval_timeout=settings.approval_timeout,
        websocket_manager=ws_manager,
        db=db,
    )
    set_approval_manager(approval_manager)

    # 4. 工具管理器（注册内置工具）
    from athena.core.tools.manager import UnifiedToolManager, set_tool_manager
    from athena.core.tools.builtin.registry import register_builtin_tools

    tool_manager = UnifiedToolManager(approval_manager=approval_manager)
    await register_builtin_tools(tool_manager, db)
    set_tool_manager(tool_manager)

    # 4.5 MCP 服务器管理器（恢复已持久化的 MCP Server）
    from athena.core.tools.mcp.manager import MCPManager, set_mcp_manager

    mcp_manager = MCPManager(tool_manager=tool_manager, db=db)
    set_mcp_manager(mcp_manager)
    await mcp_manager.load_persisted()

    # 5. LLM / 记忆 / 压缩
    from athena.core.llm.provider import set_llm_provider

    llm = LLMProvider.from_settings(settings=settings)
    set_llm_provider(llm)

    # 副 Provider：用于检索、摘要、事实提取、压缩等轻量任务
    llm_secondary = LLMProvider.from_secondary_settings(settings=settings) or llm

    from athena.core.memory.memory import MemoryManager

    memory_manager = MemoryManager(settings=settings)
    try:
        await memory_manager.initialize()
    except Exception as e:
        logger.warning("memory_init_skipped", error=str(e))
        raise e
    set_memory_manager(memory_manager)

    # 后台看门狗：周期 flush 访问统计 + 清理过期记忆
    memory_flush_task = asyncio.create_task(memory_manager.run_periodic_flush())

    from athena.core.memory.retrieval import (
        HybridRetrievalManager,
        MemoryRetrievalService,
    )

    retrieval_manager = HybridRetrievalManager(
        llm_secondary, memory_manager, settings=settings
    )
    memory_retrieval = MemoryRetrievalService(retrieval_manager)

    from athena.core.memory.summarizer import ConversationSummarizer

    conversation_summarizer = ConversationSummarizer(
        llm_secondary, memory_manager, settings=settings
    )

    from athena.core.memory.summarizer import FactExtractor

    fact_extractor = FactExtractor(llm_secondary)

    from athena.core.compression.compressor import ContextCompressor

    compressor = ContextCompressor(llm=llm_secondary, db=db, settings=settings)

    # 6. AgentWorkflow
    from athena.core.agent.workflow import AgentWorkflow
    from athena.gateway.routes._runtime import set_workflow

    workflow = AgentWorkflow(
        llm=llm,
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
    set_workflow(workflow)

    # 7. 被动会话恢复（project_memory 约束：通知用户 → 等待确认 → 执行恢复）
    from athena.gateway.recovery import recover_interrupted_sessions

    await recover_interrupted_sessions(db)

    logger.info("athena_started")
    yield

    # 关闭清理
    logger.info("athena_shutting_down")
    try:
        await approval_manager.cancel_all_pending("")
    except Exception:
        pass
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
# WebSocket 端点（/ws）— 路由在 gateway/ws/handler.py 中定义
app.include_router(websocket_router)


def run() -> None:
    """启动 uvicorn 服务器（命令行入口）."""
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
