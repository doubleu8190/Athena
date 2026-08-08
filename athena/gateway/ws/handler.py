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
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from athena.core.agent.workflow import AgentWorkflow
from athena.gateway.approval import get_approval_manager
from athena.gateway.ws.manager import get_websocket_manager
from athena.schemas.events import ClientEventType, EventType, build_event
from athena.utils.logging import get_logger

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
