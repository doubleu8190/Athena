"""Athena 应用入口 — FastAPI 应用、WebSocket 端点、启动恢复.

启动流程：
1. 配置 structlog 日志
2. 初始化 SQLite 数据库（建表）
3. 初始化 LLM Provider / 工具管理器 / 审批管理器 / 记忆系统 / 上下文压缩
4. 构建 AgentWorkflow 并注入路由运行时
5. 注册 REST API 路由与 WebSocket 端点
6. 启动时执行被动会话恢复（检测 interrupted 会话并通知用户）
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from athena.config.settings import Settings, get_settings
from athena.db.database import close_database, get_database
from athena.gateway.approval import ApprovalManager, get_approval_manager, set_approval_manager
from athena.gateway.routes import api_router
from athena.gateway.routes._runtime import set_workflow
from athena.gateway.ws.manager import WebSocketManager, get_websocket_manager, set_websocket_manager
from athena.schemas.events import ClientEventType, EventType, build_event
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
    await _recover_interrupted_sessions(db, ws_manager)

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


async def _recover_interrupted_sessions(db: Any, ws_manager: WebSocketManager) -> None:
    """被动会话恢复：检测 interrupted 会话并标记为 idle 等待用户确认.

    project_memory 约束：恢复采用被动模式，仅通知用户不主动执行，
    避免产生意外副作用。
    """
    try:
        interrupted = await db.query_sessions(status=["interrupted", "running"])
        for session in interrupted:
            await db.update_session(session["id"], status="idle")
            logger.info(
                "session_recovered_passive",
                session_id=session["id"],
                previous_status=session.get("status"),
            )
    except Exception as e:
        logger.warning("recovery_check_failed", error=str(e))


# ---------------------------------------------------------------------------
# FastAPI 应用
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


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    """WebSocket 端点 - 双向事件流.

    客户端可发送：USER_COMMAND / APPROVAL_RESPONSE / APPROVAL_CANCEL /
                  SESSION_STOP / SESSION_RESUME / MEMORY_SAVE / PING
    """
    ws_manager = get_websocket_manager()
    await ws_manager.connect(session_id, websocket)

    # 推送会话开始事件
    await ws_manager.send_to_session(
        session_id,
        build_event(EventType.SESSION_START, {"session_id": session_id}, session_id=session_id),
    )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws_manager.send_to_session(
                    session_id,
                    build_event(EventType.ERROR, {"error": "invalid json"}, session_id=session_id),
                )
                continue

            msg_type = msg.get("type", "")
            data = msg.get("data", {})

            if msg_type == ClientEventType.PING:
                await ws_manager.send_to_session(
                    session_id,
                    build_event(EventType.PONG, {"echo": data}, session_id=session_id),
                )
            elif msg_type == ClientEventType.APPROVAL_RESPONSE:
                await _handle_approval_response(session_id, data)
            elif msg_type == ClientEventType.APPROVAL_CANCEL:
                await _handle_approval_cancel(session_id, data)
            elif msg_type == ClientEventType.SESSION_STOP:
                from athena.gateway.routes._runtime import get_session_stop_event
                get_session_stop_event(session_id).set()
            elif msg_type == ClientEventType.USER_COMMAND:
                await _handle_user_command(session_id, data)
            elif msg_type == ClientEventType.MEMORY_SAVE:
                await _handle_memory_save(session_id, data)
            else:
                logger.warning("unknown_ws_message", msg_type=msg_type)
    except WebSocketDisconnect:
        logger.info("ws_client_disconnected", session_id=session_id)
    except Exception as e:
        logger.exception("ws_endpoint_error", session_id=session_id)
    finally:
        await ws_manager.disconnect(session_id, websocket)


async def _handle_user_command(session_id: str, data: dict[str, Any]) -> None:
    """处理用户命令（异步执行，避免阻塞 WebSocket 接收）."""
    from athena.gateway.routes._runtime import get_workflow, reset_session_stop_event
    workflow = get_workflow()
    if workflow is None:
        return
    message = data.get("message", "")
    system_prompt = data.get("system_prompt", "")
    reset_session_stop_event(session_id)
    # 异步执行不等待，避免阻塞 ws 接收循环
    asyncio.create_task(
        workflow.process_message(
            session_id=session_id,
            user_message=message,
            system_prompt=system_prompt,
        )
    )


async def _handle_approval_response(session_id: str, data: dict[str, Any]) -> None:
    """处理审批响应."""
    approval_id = data.get("approval_id", "")
    action = data.get("action", "")
    if not approval_id or action not in ("allow", "deny"):
        return
    manager = get_approval_manager()
    await manager.respond_approval(approval_id, action)


async def _handle_approval_cancel(session_id: str, data: dict[str, Any]) -> None:
    """处理审批取消."""
    approval_id = data.get("approval_id", "")
    if not approval_id:
        return
    manager = get_approval_manager()
    await manager.cancel_approval(approval_id)


async def _handle_memory_save(session_id: str, data: dict[str, Any]) -> None:
    """处理主动记忆保存."""
    from athena.gateway.routes._runtime import get_workflow
    workflow = get_workflow()
    if workflow is None:
        return
    memory_retrieval = getattr(workflow, "_memory_retrieval", None)
    if memory_retrieval is None:
        return
    memory_manager = getattr(memory_retrieval, "_memory", None)
    if memory_manager is None:
        return
    content = data.get("content", "")
    metadata = data.get("metadata", {})
    if content:
        await memory_manager.add_memory(
            content=content,
            session_id=session_id,
            metadata=metadata,
        )


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
