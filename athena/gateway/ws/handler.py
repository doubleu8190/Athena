"""WebSocket 端点 — 双向事件流处理.

负责:
- WebSocket 连接建立/断开管理（委托 WebSocketManager）
- 客户端消息路由分发（按 msg_type 分派到对应处理器）
- 用户命令 / 审批响应 / 记忆保存 / 会话恢复等事件处理

设计说明:
- 单一全局连接，通过 SUBSCRIBE 消息切换订阅的会话，连接本身不复用重建
- 各处理器以 ``_handle_*`` 命名，与 WebSocketManager 同级放置，职责清晰
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from athena.core.agent.workflow import AgentWorkflow
from athena.gateway.approval import get_approval_manager
from athena.gateway.ws.manager import get_websocket_manager
from athena.gateway.ws.events import ClientEventType, EventType, build_event
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.core.recovery.session_recovery import SessionRecovery
    from athena.gateway.ws.manager import WebSocketManager

logger = get_logger(__name__)

router = APIRouter(tags=["ws"])

# 需要 session_id 的客户端消息类型。
# SUBSCRIBE 是唯一允许空 session_id 的消息（空 = 退订）；
# 其余会话级消息 session_id 必填，缺失/为空统一拒绝，避免 "" 与 None 两套折叠逻辑并存。
_SESSION_SCOPED_TYPES = frozenset(
    {
        ClientEventType.SESSION_STOP,
        ClientEventType.SESSION_RESUME,
        ClientEventType.USER_COMMAND,
        ClientEventType.MEMORY_SAVE,
    }
)


async def _send_ws_error(websocket: WebSocket, error: str, message: str) -> None:
    """向单个连接回推 ERROR 事件."""
    await websocket.send_text(
        json.dumps(
            build_event(EventType.ERROR, {"error": error, "message": message}),
            ensure_ascii=False,
        )
    )


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket 端点 — 单一全局连接，按订阅路由事件.

    客户端支持的消息类型:
        USER_COMMAND / APPROVAL_RESPONSE / APPROVAL_CANCEL / SESSION_STOP /
        SESSION_RESUME / MEMORY_SAVE / SUBSCRIBE / PING

    会话切换通过 SUBSCRIBE 消息（data.session_id）实现，连接本身不复用重建。
    收到无效 JSON 时回推 ERROR 事件后继续接收，不中断连接。
    除 SUBSCRIBE 外的会话级消息（USER_COMMAND 等）session_id 必填，
    缺失/为空时回推 ERROR 事件并跳过该消息（不当作退订或空会话处理）。

    Args:
        websocket: FastAPI 传入的 WebSocket 连接实例。

    Returns:
        None。连接断开或异常时返回。

    Raises:
        WebSocketDisconnect: 客户端断开连接（在函数内部捕获处理）。
    """
    ws_manager = get_websocket_manager()
    await ws_manager.connect(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                logger.info("ws_message_received", msg=msg)
            except json.JSONDecodeError:
                # 无效 JSON：回推错误事件后继续，不中断连接
                await websocket.send_text(
                    json.dumps(
                        build_event(EventType.ERROR, {"error": "invalid json"}),
                        ensure_ascii=False,
                    )
                )
                continue

            msg_type = msg.get("type", "")
            data = msg.get("data", {})

            if msg_type == ClientEventType.SUBSCRIBE:
                session_id = data.get("session_id") or None
                await ws_manager.subscribe(websocket, session_id)
                # 与旧语义对齐：订阅成功后回推 SESSION_START
                if session_id:
                    await ws_manager.send_to_session(
                        session_id,
                        build_event(
                            EventType.SESSION_START,
                            {"session_id": session_id},
                            session_id=session_id,
                        ),
                    )
            elif msg_type == ClientEventType.PING:
                await websocket.send_text(
                    json.dumps(
                        build_event(EventType.PONG, {"echo": data}),
                        ensure_ascii=False,
                    )
                )
            elif msg_type == ClientEventType.APPROVAL_RESPONSE:
                await _handle_approval_response(data)
            elif msg_type == ClientEventType.APPROVAL_CANCEL:
                await _handle_approval_cancel(data)
            elif msg_type in _SESSION_SCOPED_TYPES:
                # 会话级消息：session_id 必填。缺失/为空统一拒绝并回推 ERROR，
                # 不再把 "" 折叠成退订（or None）或当成真会话（or ""）处理，
                # 与 SUBSCRIBE 的"空 = 退订"语义区分。
                session_id = data.get("session_id", "")
                if not session_id:
                    logger.warning(
                        "ws_message_missing_session_id",
                        msg_type=msg_type,
                    )
                    await _send_ws_error(
                        websocket,
                        "missing_session_id",
                        f"消息 {msg_type} 缺少 session_id",
                    )
                elif msg_type == ClientEventType.USER_COMMAND:
                    # 隐式订阅：防御"订阅命令未先到达"的竞态，
                    # 保证命令触发的事件流能回到本连接
                    await ws_manager.subscribe(websocket, session_id)
                    await _handle_user_command(session_id, data)
                elif msg_type == ClientEventType.SESSION_STOP:
                    from athena.gateway.routes._runtime import (
                        get_session_stop_event,
                    )

                    get_session_stop_event(session_id).set()
                elif msg_type == ClientEventType.SESSION_RESUME:
                    await _handle_session_resume(session_id, data)
                elif msg_type == ClientEventType.MEMORY_SAVE:
                    await _handle_memory_save(session_id, data)
            else:
                logger.warning("unknown_ws_message", msg_type=msg_type)
    except WebSocketDisconnect:
        logger.info("ws_client_disconnected")
    except Exception:
        logger.exception("ws_endpoint_error")
    finally:
        await ws_manager.disconnect(websocket)


# ------------------------------------------------------------------
# 消息处理器（按消息类型分派）
# ------------------------------------------------------------------


async def _handle_user_command(session_id: str, data: dict[str, Any]) -> None:
    """处理用户命令.

    将消息提交给 AgentWorkflow.process_message() 异步执行，
    不等待结果，避免阻塞 WebSocket 接收循环。

    Args:
        session_id: 会话 ID。
        data: 命令数据，需包含 "message" 字段（用户消息内容）。
    """
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
    raw_attachment_ids: object = data.get("attachment_ids", [])
    attachment_ids: list[str] = []
    if isinstance(raw_attachment_ids, list):
        for item in raw_attachment_ids:
            if isinstance(item, str):
                attachment_ids.append(item)
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
            attachment_ids=attachment_ids,
            stop_signal=stop_signal,
        )
    )
    # run 结束时消费掉 stop 事件，避免粘滞误停下一次 run（clear 此前无人调用）
    task.add_done_callback(lambda _t: clear_session_stop_event(session_id))


async def _handle_approval_response(data: dict[str, Any]) -> None:
    """处理审批响应（允许/拒绝）.

    Args:
        data: 审批数据，需包含:
            - approval_id: 审批请求 ID
            - action: "allow"（允许）或 "deny"（拒绝）
    """
    approval_id = data.get("approval_id", "")
    action = data.get("action", "")
    if not approval_id or action not in ("allow", "deny"):
        return
    manager = get_approval_manager()
    await manager.respond_approval(approval_id, action)


async def _handle_approval_cancel(data: dict[str, Any]) -> None:
    """处理审批取消（用户放弃审批，工具执行将被终止）.

    Args:
        data: 审批数据，需包含 approval_id（审批请求 ID）。
    """
    approval_id = data.get("approval_id", "")
    if not approval_id:
        return
    manager = get_approval_manager()
    await manager.cancel_approval(approval_id)


async def _handle_memory_save(session_id: str, data: dict[str, Any]) -> None:
    """处理主动记忆保存（用户在前端手动保存记忆）.

    通过 workflow 内部对象链 (_memory_retrieval._memory) 定位 MemoryManager，
    写入内容时自动注入 session_id 元数据用于来源追踪。

    Args:
        session_id: 会话 ID。
        data: 记忆数据，需包含:
            - content: 记忆文本内容
            - metadata: 可选附加元数据（dict）
    """
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

    仅当会话状态为 interrupted / running / failed 时才允许恢复。
    其他状态回推 ERROR 事件。

    Args:
        session_id: 会话 ID。
        data: 恢复数据，可选 mode 字段:
            - mode="recover"（默认）: 完整恢复流程，重新触发 Agent 执行
            - mode="abandon": 仅重置状态为 idle，不触发恢复

    Raises:
        WebSocketDisconnect: 由异步恢复任务抛出时被捕获并记录日志。
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
    """异步执行会话恢复并通过 WebSocket 推送结果.

    作为独立任务运行（由 _handle_session_resume 创建），
    成功后推送 SESSION_RECOVERED，失败推送 ERROR。

    Args:
        recovery: 会话恢复器实例。
        session_id: 会话 ID。
        ws_manager: WebSocket 管理器，用于推送事件。
    """
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
