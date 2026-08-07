"""会话管理路由 — 创建/列出/查询/删除会话、发送消息."""

from __future__ import annotations

import asyncio
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
    session = await db.create_session(session_id, title=req.title)
    return session


@router.get("")
async def list_sessions() -> list[Session]:
    """列出所有会话."""
    db = await _get_db()
    return await db.list_sessions()


@router.get("/{session_id}")
async def get_session(session_id: str) -> Session:
    """获取会话详情."""
    db = await _get_db()
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    """删除会话及其所有关联数据."""
    db = await _get_db()
    await db.delete_session(session_id)
    return {"status": "deleted", "session_id": session_id}


@router.get("/{session_id}/messages")
async def get_messages(session_id: str, limit: int | None = None) -> list[Message]:
    """获取会话消息列表."""
    db = await _get_db()
    if not await db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return await db.get_messages(session_id, limit=limit)


@router.post("/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest) -> dict[str, Any]:
    """发送消息到会话（同步返回结果，流式输出走 WebSocket）."""
    db = await _get_db()
    if not await db.get_session(session_id):
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
    if not await db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return await db.get_steps(session_id)


@router.get("/{session_id}/tool_calls")
async def get_tool_calls(session_id: str, status: str | None = None) -> list[ToolCallRecord]:
    """获取会话工具调用记录."""
    db = await _get_db()
    return await db.query_tool_calls(session_id, status=status)


@router.post("/{session_id}/stop")
async def stop_session(session_id: str) -> dict[str, str]:
    """请求停止会话当前运行."""
    from athena.gateway.routes._runtime import get_session_stop_event
    stop_event = get_session_stop_event(session_id)
    stop_event.set()
    return {"status": "stop_requested", "session_id": session_id}
