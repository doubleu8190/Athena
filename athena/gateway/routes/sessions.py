"""会话管理路由 — 创建/列出/查询/删除会话、发送消息、恢复中断会话."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from athena.core.agent.workflow import AgentWorkflow
from athena.db.database import Database, get_database
from athena.models import Message, Session, Step, ToolCallRecord
from athena.utils.ids import generate_session_id
from athena.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    title: str = "New Session"


class SendMessageRequest(BaseModel):
    message: str
    system_prompt: str = ""


class InterruptedToolInfo(BaseModel):
    """中断工具调用的摘要信息."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    error_message: str | None = None


class InterruptedSessionInfo(BaseModel):
    """中断会话的恢复上下文 — 帮助用户决定是否恢复."""

    session_id: str
    title: str
    status: str
    interrupted_at: str | None = None  # 最后更新时间近似中断时间
    last_message_role: str | None = None
    last_message_preview: str | None = None  # 最后一条消息的前 200 字符
    pending_tools: list[InterruptedToolInfo] = Field(default_factory=list)
    recovery_hint: str = ""  # 恢复策略提示


class RecoverSessionResponse(BaseModel):
    """恢复操作的响应."""

    session_id: str
    status: str  # "recovering" | "idle" | "failed"
    message: str


async def _get_db() -> Database:
    from athena.config.settings import get_settings
    return await get_database(get_settings().sqlite_db_path)


async def _get_workflow() -> AgentWorkflow | None:
    """获取全局 AgentWorkflow 实例（由 main.py 注入）."""
    from athena.gateway.routes._runtime import get_workflow
    return get_workflow()


@router.post("")
async def create_session(req: CreateSessionRequest) -> Session:
    """创建新会话."""
    db = await _get_db()
    session_id = generate_session_id()
    session = await db.sessions.create(session_id, title=req.title)
    return session


@router.get("")
async def list_sessions() -> list[Session]:
    """列出所有会话."""
    db = await _get_db()
    return await db.sessions.list_all()


@router.get("/interrupted", response_model=list[InterruptedSessionInfo])
async def list_interrupted_sessions() -> list[InterruptedSessionInfo]:
    """列出所有中断/失败的会话及恢复上下文.

    前端可据此展示恢复面板，用户决定是否恢复。
    """
    db = await _get_db()
    sessions = await db.sessions.query_by_status(["interrupted", "running", "failed"])
    result: list[InterruptedSessionInfo] = []

    for session in sessions:
        # 获取最后一条消息作为恢复上下文
        messages = await db.messages.get_by_session(session.id, limit=1)
        last_msg = messages[-1] if messages else None

        last_role = str(last_msg.role) if last_msg else None
        last_preview = (last_msg.content or "")[:200] if last_msg else None

        # 获取中断的工具调用
        running_tools = await db.tool_calls.query(session.id, status="running")
        pending_tools = [
            InterruptedToolInfo(
                tool_call_id=tc.id,
                tool_name=tc.tool_name,
                arguments=tc.arguments if isinstance(tc.arguments, dict) else {},
                status=str(tc.status),
                error_message=tc.error_message,
            )
            for tc in running_tools
        ]

        # 恢复策略提示
        hint = _build_recovery_hint(last_role, pending_tools)

        result.append(InterruptedSessionInfo(
            session_id=session.id,
            title=session.title,
            status=str(session.status),
            interrupted_at=session.updated_at.isoformat() if session.updated_at else None,
            last_message_role=last_role,
            last_message_preview=last_preview,
            pending_tools=pending_tools,
            recovery_hint=hint,
        ))

    return result


@router.get("/{session_id}")
async def get_session(session_id: str) -> Session:
    """获取会话详情."""
    db = await _get_db()
    session = await db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


class UpdateSessionRequest(BaseModel):
    title: str


@router.patch("/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest) -> Session:
    """重命名会话."""
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title must not be empty")
    db = await _get_db()
    session = await db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.sessions.update(session_id, title=title)
    updated = await db.sessions.get(session_id)
    assert updated is not None
    return updated


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    """删除会话及其所有关联数据."""
    db = await _get_db()
    await db.sessions.delete(session_id)
    return {"status": "deleted", "session_id": session_id}


@router.get("/{session_id}/messages")
async def get_messages(session_id: str, limit: int | None = None) -> list[Message]:
    """获取会话消息列表."""
    db = await _get_db()
    if not await db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return await db.messages.get_by_session(session_id, limit=limit)


@router.post("/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest) -> dict[str, Any]:
    """发送消息到会话（同步返回结果，流式输出走 WebSocket）."""
    db = await _get_db()
    if not await db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    workflow = await _get_workflow()
    if workflow is None:
        raise HTTPException(status_code=503, detail="Agent workflow not initialized")

    try:
        result = await workflow.process_message(
            session_id=session_id,
            user_message=req.message,
        )
        return result
    except Exception as e:
        logger.exception("send_message_failed", session_id=session_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{session_id}/steps")
async def get_steps(session_id: str) -> list[Step]:
    """获取会话执行步骤."""
    db = await _get_db()
    if not await db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return await db.steps.get_by_session(session_id)


@router.get("/{session_id}/tool_calls")
async def get_tool_calls(session_id: str, status: str | None = None) -> list[ToolCallRecord]:
    """获取会话工具调用记录."""
    db = await _get_db()
    return await db.tool_calls.query(session_id, status=status)


@router.post("/{session_id}/stop")
async def stop_session(session_id: str) -> dict[str, str]:
    """请求停止会话当前运行."""
    from athena.gateway.routes._runtime import get_session_stop_event
    stop_event = get_session_stop_event(session_id)
    stop_event.set()
    return {"status": "stop_requested", "session_id": session_id}


# ── 恢复相关端点 ──────────────────────────────────────────────


@router.post("/{session_id}/recover", response_model=RecoverSessionResponse)
async def recover_session(session_id: str) -> RecoverSessionResponse:
    """手动触发会话恢复 — 完整执行 SessionRecovery 流程.

    恢复流程：
    1. 检查待审批请求
    2. 处理中断的工具调用
    3. 判断恢复起点
    4. 重新触发 Agent 运行（带重试）
    """
    db = await _get_db()
    session = await db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    current_status = str(session.status)
    if current_status not in ("interrupted", "running", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Session status is '{current_status}', only interrupted/running/failed sessions can be recovered",
        )

    workflow = await _get_workflow()
    if workflow is None:
        raise HTTPException(status_code=503, detail="Agent workflow not initialized")

    # 异步执行恢复，不阻塞响应
    from athena.core.recovery.session_recovery import SessionRecovery
    from athena.gateway.ws.manager import get_websocket_manager

    try:
        ws_manager = get_websocket_manager()
    except Exception:
        ws_manager = None  # WS 管理器未初始化时跳过
    recovery = SessionRecovery(db=db, ws_manager=ws_manager, agent_workflow=workflow)

    try:
        # 先标记为 recovering，防止重复触发
        await db.sessions.update(session_id, status="recovering")

        # 异步执行恢复流程
        asyncio.create_task(_execute_recovery(recovery, session_id))

        logger.info("session_recovery_triggered", session_id=session_id)
        return RecoverSessionResponse(
            session_id=session_id,
            status="recovering",
            message="恢复流程已启动",
        )
    except Exception as e:
        logger.error("recover_session_failed", session_id=session_id, error=str(e))
        await db.sessions.update(session_id, status="failed")
        raise HTTPException(status_code=500, detail=f"Recovery failed: {e}")


@router.post("/{session_id}/abandon", response_model=RecoverSessionResponse)
async def abandon_session(session_id: str) -> RecoverSessionResponse:
    """放弃中断的会话 — 重置状态但不触发恢复.

    清理孤儿 steps/tool_calls，将状态重置为 idle。
    用户可以在此基础上重新发消息。
    """
    db = await _get_db()
    session = await db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    current_status = str(session.status)
    if current_status not in ("interrupted", "running", "failed", "recovering"):
        raise HTTPException(
            status_code=409,
            detail=f"Session status is '{current_status}', nothing to abandon",
        )

    # 被动清理：重置会话状态 + 清理孤儿步骤
    await db.sessions.update(session_id, status="idle")
    await db.cleanup_interrupted_session(session_id)

    logger.info("session_abandoned", session_id=session_id)
    return RecoverSessionResponse(
        session_id=session_id,
        status="idle",
        message="会话已重置，可以继续发送消息",
    )


# ── 内部辅助函数 ──────────────────────────────────────────────


def _build_recovery_hint(
    last_role: str | None,
    pending_tools: list[InterruptedToolInfo],
) -> str:
    """根据中断上下文生成用户可读的恢复策略提示."""
    if last_role == "user":
        return "用户消息未得到响应，恢复将重新执行 Agent"
    if last_role == "assistant":
        if pending_tools:
            names = [t.tool_name for t in pending_tools]
            return f"工具 {', '.join(names)} 未完成执行，恢复将重新执行这些工具"
        return "Agent 回复正常完成，无需恢复"
    if last_role == "tool":
        return "工具结果未被处理，恢复将重新调用 LLM"
    return "会话中断，恢复将尝试从断点继续"


async def _execute_recovery(
    recovery: "SessionRecovery", session_id: str
) -> None:
    """异步执行恢复流程（被 asyncio.create_task 调用）."""
    try:
        await recovery._recover_session(session_id)
    except Exception as e:
        logger.error("recovery_execution_failed", session_id=session_id, error=str(e))
