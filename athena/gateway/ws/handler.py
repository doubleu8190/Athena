"""WebSocket 端点 — 双向事件流处理.

从 main.py 剥离的业务逻辑：WebSocket 连接管理、消息路由、
用户命令/审批/记忆等事件处理。

迁移理由：这些函数是 WebSocket 协议层的事件处理器，属于 gateway 层
业务逻辑，不应驻留在应用启动文件中。放在 gateway/ws/ 下与
WebSocketManager 同级，职责清晰。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

from athena.core.agent.workflow import AgentWorkflow
from athena.gateway.approval import get_approval_manager
from athena.gateway.ws.manager import get_websocket_manager
from athena.schemas.events import ClientEventType, EventType, build_event
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.core.recovery.session_recovery import SessionRecovery
    from athena.gateway.ws.manager import WebSocketManager

logger = get_logger(__name__)


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
                logger.info("ws_message_received", msg=msg)
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
            elif msg_type == ClientEventType.SESSION_RESUME:
                await _handle_session_resume(session_id, data)
            elif msg_type == ClientEventType.USER_COMMAND:
                await _handle_user_command(session_id, data)
            elif msg_type == ClientEventType.MEMORY_SAVE:
                await _handle_memory_save(session_id, data)
            else:
                logger.warning("unknown_ws_message", msg_type=msg_type)
    except WebSocketDisconnect:
        logger.info("ws_client_disconnected", session_id=session_id)
    except Exception:
        logger.exception("ws_endpoint_error", session_id=session_id)
    finally:
        await ws_manager.disconnect(session_id, websocket)


async def _handle_user_command(session_id: str, data: dict[str, Any]) -> None:
    """处理用户命令（异步执行，避免阻塞 WebSocket 接收）."""
    from athena.gateway.routes._runtime import (
        clear_session_stop_event,
        get_workflow,
        get_session_stop_event,
        reset_session_stop_event,
    )
    workflow: AgentWorkflow = get_workflow()
    if workflow is None:
        logger.error("workflow_not_initialized", session_id=session_id)
        return
    message = data.get("message", "")
    logger.info(
        "user_command_received",
        session_id=session_id,
        message_length=len(message),
    )
    # 传入会话级 stop 事件，使 SESSION_STOP / POST /stop 真正能终止 run
    stop_signal = get_session_stop_event(session_id)
    reset_session_stop_event(session_id)
    # 异步执行不等待，避免阻塞 ws 接收循环
    task = asyncio.create_task(
        workflow.process_message(
            session_id=session_id,
            user_message=message,
            stop_signal=stop_signal,
        )
    )
    # run 结束时消费掉 stop 事件，避免粘滞误停下一次 run（clear 此前无人调用）
    task.add_done_callback(lambda _t: clear_session_stop_event(session_id))


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
            metadata={"session_id": session_id, **metadata},
        )


async def _handle_session_resume(session_id: str, data: dict[str, Any]) -> None:
    """处理用户主动恢复会话.

    data 字段：
      mode: "recover" | "abandon"（默认 "recover"）
        - recover: 完整恢复流程（重触发 Agent）
        - abandon: 仅重置状态，不触发恢复
    """
    from athena.core.recovery.session_recovery import SessionRecovery
    from athena.db.database import get_database
    from athena.config.settings import get_settings

    ws_manager = get_websocket_manager()
    mode = data.get("mode", "recover")

    settings = get_settings()
    db = await get_database(settings.sqlite_db_path)
    session = await db.sessions.get(session_id)

    if not session:
        await ws_manager.send_to_session(
            session_id,
            build_event(
                EventType.ERROR,
                {"error": "session_not_found", "message": "会话不存在"},
                session_id=session_id,
            ),
        )
        return

    current_status = str(session.status)
    if current_status not in ("interrupted", "running", "failed"):
        await ws_manager.send_to_session(
            session_id,
            build_event(
                EventType.ERROR,
                {
                    "error": "invalid_session_status",
                    "message": f"会话状态为 '{current_status}'，无需恢复",
                },
                session_id=session_id,
            ),
        )
        return

    if mode == "abandon":
        # 仅重置状态
        await db.sessions.update(session_id, status="idle")
        await db.cleanup_interrupted_session(session_id)
        await ws_manager.send_to_session(
            session_id,
            build_event(
                EventType.SESSION_RECOVERED,
                {"mode": "abandon", "message": "会话已重置"},
                session_id=session_id,
            ),
        )
        logger.info("session_abandoned_via_ws", session_id=session_id)
        return

    # 完整恢复
    from athena.gateway.routes._runtime import get_workflow

    workflow = get_workflow()
    if workflow is None:
        await ws_manager.send_to_session(
            session_id,
            build_event(
                EventType.ERROR,
                {"error": "workflow_not_ready", "message": "Agent 未就绪"},
                session_id=session_id,
            ),
        )
        return

    recovery = SessionRecovery(db=db, ws_manager=ws_manager, agent_workflow=workflow)

    # 通知客户端恢复已开始
    await ws_manager.send_to_session(
        session_id,
        build_event(
            EventType.RECOVERY_START,
            {"session_id": session_id, "mode": "recover"},
            session_id=session_id,
        ),
    )

    # 异步执行恢复，不阻塞 WS 接收循环
    asyncio.create_task(_execute_ws_recovery(recovery, session_id, ws_manager))
    logger.info("session_recovery_triggered_via_ws", session_id=session_id)


async def _execute_ws_recovery(
    recovery: "SessionRecovery",
    session_id: str,
    ws_manager: "WebSocketManager",
) -> None:
    """异步执行恢复并通过 WS 推送结果."""
    try:
        await recovery._recover_session(session_id)
        await ws_manager.send_to_session(
            session_id,
            build_event(
                EventType.SESSION_RECOVERED,
                {"session_id": session_id, "message": "会话恢复成功"},
                session_id=session_id,
            ),
        )
    except Exception as e:
        logger.error("ws_recovery_failed", session_id=session_id, error=str(e))
        await ws_manager.send_to_session(
            session_id,
            build_event(
                EventType.ERROR,
                {
                    "error": "recovery_failed",
                    "message": f"恢复失败: {e}",
                },
                session_id=session_id,
            ),
        )
