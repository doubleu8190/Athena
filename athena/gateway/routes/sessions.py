"""会话管理路由 — 创建/列出/查询/删除会话、发送消息、恢复中断会话。

提供会话的完整 CRUD 操作，以及消息发送、执行步骤查询、工具调用记录查询、
会话停止和恢复等端点。所有端点挂载在 ``/sessions`` 前缀下。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from athena.core.agent.workflow import AgentWorkflow
from athena.infrastructure.sqlite.database import Database
from athena.models import Message, Session, Step
from athena.utils.ids import generate_session_id
from athena.utils.logging import get_logger
from athena.runtime import runtime_from

if TYPE_CHECKING:
    from athena.core.recovery.session_recovery import SessionRecovery

logger = get_logger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    """创建会话请求体。"""

    title: str = "New Session"


class SendMessageRequest(BaseModel):
    """发送消息请求体。"""

    message: str
    system_prompt: str = ""
    attachment_ids: list[str] = Field(default_factory=list)


class InterruptedToolInfo(BaseModel):
    """中断工具调用的摘要信息。"""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    error_message: str | None = None


class InterruptedSessionInfo(BaseModel):
    """中断会话的恢复上下文 — 帮助用户决定是否恢复。

    Attributes:
        session_id: 会话 ID。
        title: 会话标题。
        status: 当前状态（interrupted/running/failed）。
        interrupted_at: 近似中断时间（最后更新时间）。
        last_message_role: 最后一条消息的角色。
        last_message_preview: 最后一条消息的前 200 字符。
        pending_tools: 未完成的工具调用列表。
        recovery_hint: 恢复策略提示文本。
    """

    session_id: str
    title: str
    status: str
    interrupted_at: str | None = None
    last_message_role: str | None = None
    last_message_preview: str | None = None
    pending_tools: list[InterruptedToolInfo] = Field(default_factory=list)
    recovery_hint: str = ""


class RecoverSessionResponse(BaseModel):
    """恢复操作的响应。"""

    session_id: str
    status: str  # "recovering" | "idle" | "failed"
    message: str


class ToolCallResponse(BaseModel):
    """前端工具调用视图 DTO。

    数据库领域模型使用 raw_output/error_message；前端和 WebSocket 使用
    output/error。这里做一次字段收敛，避免历史回放和实时流展示不一致。
    """

    id: str
    session_id: str
    step_id: str
    run_id: str | None = None
    tool_name: str
    arguments: dict[str, Any]
    output: str | None = None
    error: str | None = None
    error_stack: str | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float = 0
    risk_level: str = "low"


async def _db_for(request: Request) -> Database:
    """从请求上下文获取数据库实例。"""
    return runtime_from(request).db


async def _workflow_for(request: Request) -> AgentWorkflow:
    """从请求上下文获取 AgentWorkflow 实例。"""
    return runtime_from(request).workflow


@router.post("")
async def create_session(req: CreateSessionRequest, request: Request) -> Session:
    """创建新会话."""
    db = await _db_for(request)
    session_id = generate_session_id()
    session = await db.sessions.create(session_id, title=req.title)
    return session


@router.get("")
async def list_sessions(request: Request) -> list[Session]:
    """列出所有会话."""
    db = await _db_for(request)
    return await db.sessions.list_all()


@router.get("/interrupted", response_model=list[InterruptedSessionInfo])
async def list_interrupted_sessions(request: Request) -> list[InterruptedSessionInfo]:
    """列出所有中断/失败的会话及恢复上下文.

    前端可据此展示恢复面板，用户决定是否恢复。
    """
    db = await _db_for(request)
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
async def get_session(session_id: str, request: Request) -> Session:
    """获取会话详情."""
    db = await _db_for(request)
    session = await db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


class UpdateSessionRequest(BaseModel):
    title: str


@router.patch("/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest, request: Request) -> Session:
    """重命名会话."""
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title must not be empty")
    db = await _db_for(request)
    session = await db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.sessions.update(session_id, title=title)
    updated = await db.sessions.get(session_id)
    assert updated is not None
    return updated


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict[str, str]:
    """删除会话及其所有关联数据."""
    db = await _db_for(request)
    await db.files.delete_session(session_id)
    await runtime_from(request).file_runtime.cleanup_unreferenced_blobs()
    await db.sessions.delete(session_id)
    return {"status": "deleted", "session_id": session_id}


@router.get("/{session_id}/messages")
async def get_messages(session_id: str, request: Request, limit: int | None = None) -> list[Message]:
    """获取会话消息列表."""
    db = await _db_for(request)
    if not await db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return await db.messages.get_by_session(session_id, limit=limit)


@router.post("/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest, request: Request) -> dict[str, Any]:
    """发送消息到会话（同步返回结果，流式输出走 WebSocket）."""
    db = await _db_for(request)
    if not await db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    workflow = await _workflow_for(request)
    try:
        result = await workflow.process_message(
            session_id=session_id,
            user_message=req.message,
            attachment_ids=req.attachment_ids,
        )
        return result
    except Exception as e:
        logger.exception("send_message_failed", session_id=session_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{session_id}/steps")
async def get_steps(session_id: str, request: Request) -> list[Step]:
    """获取会话执行步骤."""
    db = await _db_for(request)
    if not await db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return await db.steps.get_by_session(session_id)


@router.get("/{session_id}/tool_calls")
async def get_tool_calls(session_id: str, request: Request, status: str | None = None) -> list[ToolCallResponse]:
    """获取会话工具调用记录."""
    db = await _db_for(request)
    if not await db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    tool_calls = await db.tool_calls.query(session_id, status=status)
    steps = await db.steps.get_by_session(session_id)
    run_by_step = {step.id: step.run_id for step in steps}
    risk_level = runtime_from(request).tool_manager.get_risk_level
    return [
        ToolCallResponse(
            id=tc.id,
            session_id=tc.session_id,
            step_id=tc.step_id,
            run_id=run_by_step.get(tc.step_id),
            tool_name=tc.tool_name,
            arguments=tc.arguments,
            output=tc.raw_output,
            error=tc.error_message,
            error_stack=tc.error_stack,
            status=getattr(tc.status, "value", str(tc.status)),
            started_at=tc.started_at,
            completed_at=tc.completed_at,
            duration_ms=tc.duration_ms,
            risk_level=risk_level(tc.tool_name),
        )
        for tc in tool_calls
    ]


@router.post("/{session_id}/stop")
async def stop_session(session_id: str, request: Request) -> dict[str, str]:
    """请求停止会话当前运行."""
    from athena.gateway.routes._runtime import get_session_stop_event
    stop_event = get_session_stop_event(session_id)
    stop_event.set()
    return {"status": "stop_requested", "session_id": session_id}


# ── 恢复相关端点 ──────────────────────────────────────────────


@router.post("/{session_id}/recover", response_model=RecoverSessionResponse)
async def recover_session(session_id: str, request: Request) -> RecoverSessionResponse:
    """手动触发会话恢复 — 完整执行 SessionRecovery 流程.

    恢复流程：
    1. 检查待审批请求
    2. 处理中断的工具调用
    3. 判断恢复起点
    4. 重新触发 Agent 运行（带重试）
    """
    db = await _db_for(request)
    session = await db.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    current_status = str(session.status)
    if current_status not in ("interrupted", "running", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Session status is '{current_status}', only interrupted/running/failed sessions can be recovered",
        )

    workflow = await _workflow_for(request)
    # 异步执行恢复，不阻塞响应
    from athena.core.recovery.session_recovery import SessionRecovery
    ws_manager = runtime_from(request).websocket_manager
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
async def abandon_session(session_id: str, request: Request) -> RecoverSessionResponse:
    """放弃中断的会话 — 重置状态但不触发恢复.

    清理孤儿 steps/tool_calls，将状态重置为 idle。
    用户可以在此基础上重新发消息。
    """
    db = await _db_for(request)
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
    """根据中断上下文生成用户可读的恢复策略提示。

    Args:
        last_role: 最后一条消息的角色（user/assistant/tool）。
        pending_tools: 未完成的工具调用列表。

    Returns:
        恢复策略提示文本。
    """
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
    """异步执行恢复流程（被 asyncio.create_task 调用）。

    Args:
        recovery: 会话恢复器实例。
        session_id: 会话 ID。
    """
    try:
        await recovery._recover_session(session_id)
    except Exception as e:
        logger.error("recovery_execution_failed", session_id=session_id, error=str(e))
