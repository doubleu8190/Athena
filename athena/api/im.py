"""IM message endpoints — WebSocket + SSE streaming.

WebSocket:
  WS /api/v1/ws/im/web — Bidirectional real-time communication
    Client → Server: {type: 'message'|'confirm', ...}
    Server → Client: {type: 'event_name', data: {...}}

SSE (fallback, kept for backward compat):
  POST /api/v1/im/web/message — Send message, receive SSE stream
  POST /api/v1/im/web/message/confirm — Submit confirmation response

Powered by LangGraph tool-calling agent graph (LLM decides: answer or call tools).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from datetime import UTC
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from athena.config import get_config
from athena.core.message import UnifiedMessage
from athena.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["im"])


# ── SSE event types ──────────────────────────────────────────────────────────


class SseEvent(StrEnum):
    """Well-known SSE event names emitted by the web message endpoint.

    The frontend keys off these strings to update the UI in real time.
    """

    # Agent thinking / text streaming
    AGENT_THINKING = "agent_thinking"
    TEXT_DELTA = "text_delta"

    # Tool execution
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_RESULT = "tool_call_result"

    # Human-in-the-loop
    CONFIRM_REQUEST = "confirm_request"
    CONFIRM_REQUIRED = "confirm_required"
    CONFIRM_TIMEOUT = "confirm_timeout"

    # Terminal
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"

    # General
    ERROR = "error"

    # ── Deprecated (kept for backward compat, not emitted by agent graph) ──
    PLAN_GENERATING = "plan_generating"
    PLAN_GENERATED = "plan_generated"
    PLAN_FAILED = "plan_failed"
    SUBTASK_STARTED = "subtask_started"
    SUBTASK_COMPLETED = "subtask_completed"
    SUBTASK_FAILED = "subtask_failed"
    SUBTASK_FALLBACK = "subtask_fallback"
    SUBTASK_SKIPPED = "subtask_skipped"


class MessageRequest(BaseModel):
    content: str
    chat_id: str | None = None
    attachments: list[dict[str, Any]] | None = None


class ConfirmRequest(BaseModel):
    chat_id: str
    approved: bool


# ── Graph execution helpers ─────────────────────────────────────────


async def _dispatch_node_events(
    event: dict,
    send_event: Callable[[SseEvent, dict], Any],
) -> bool:
    """Dispatch node-level events from a single graph update.

    Returns True if the task reached a completed state.
    """
    task_completed = False
    for node_name, node_output in event.items():
        if node_name == "agent":
            messages = node_output.get("messages", [])
            status = node_output.get("status", "")

            for msg in messages:
                if hasattr(msg, "content") and msg.content:
                    content = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    if content.strip():
                        await send_event(
                            SseEvent.TEXT_DELTA,
                            {"content": content},
                        )

                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        tc_name = tc.get("name", "unknown")
                        tc_args = tc.get("args", tc.get("arguments", {}))
                        tc_id = tc.get("id", "")
                        await send_event(
                            SseEvent.TOOL_CALL_START,
                            {
                                "tool_name": tc_name,
                                "tool_call_id": tc_id,
                                "args_preview": str(tc_args)[:200],
                            },
                        )

            if status == "completed":
                task_completed = True
                await send_event(
                    SseEvent.TASK_COMPLETED,
                    {"summary": "Response complete"},
                )
            elif status == "failed":
                await send_event(
                    SseEvent.TASK_FAILED,
                    {"error": "Agent failed to generate a response"},
                )

        elif node_name == "confirm":
            c_messages = node_output.get("messages", [])
            for msg in c_messages:
                if hasattr(msg, "content") and hasattr(msg, "tool_call_id"):
                    content = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    tool_name = getattr(msg, "name", "unknown")
                    await send_event(
                        SseEvent.TOOL_CALL_RESULT,
                        {
                            "tool_call_id": getattr(msg, "tool_call_id", ""),
                            "tool_name": tool_name,
                            "success": False,
                            "output_preview": content[:500],
                        },
                    )

        elif node_name == "tools":
            t_messages = node_output.get("messages", [])
            for msg in t_messages:
                if hasattr(msg, "content") and hasattr(msg, "tool_call_id"):
                    content = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    is_error = '"error"' in content[:100] if content else False
                    tool_name = getattr(msg, "name", "unknown")
                    await send_event(
                        SseEvent.TOOL_CALL_RESULT,
                        {
                            "tool_call_id": getattr(msg, "tool_call_id", ""),
                            "tool_name": tool_name,
                            "success": not is_error,
                            "output_preview": content[:500],
                        },
                    )
    return task_completed


class _SSEEventCollector:
    """Adapter that bridges the send_event callback interface to SSE string yields.

    Usage::

        collector = _SSEEventCollector()
        await _dispatch_node_events(event, collector)
        for sse_str in collector.drain():
            yield sse_str
    """

    def __init__(self) -> None:
        self.events: list[str] = []

    async def __call__(self, event_type: SseEvent, data: dict) -> None:
        self.events.append(_sse_event(event_type, data))

    def drain(self) -> list[str]:
        """Return collected events and clear the buffer."""
        result = self.events
        self.events = []
        return result


async def _handle_confirmation_request(
    pending_tools: list[dict[str, Any]],
    send_event: Callable[[SseEvent, dict], Any],
    pending_approvals: dict[str, asyncio.Future] | None = None,
    chat_id: str | None = None,
    approval_timeout: float = 60.0,
) -> dict[str, str] | None:
    """Handle confirmation requests for tools needing user approval.

    Sends a ``CONFIRM_REQUEST`` event for each pending tool and waits
    for the user's confirmation via ``asyncio.Future`` +
    ``asyncio.wait_for``.

    When ``pending_approvals`` and ``chat_id`` are provided (WebSocket
    mode), creates a Future per tool and waits for client responses.
    Otherwise returns ``None`` (SSE fallback — client must confirm via
    the separate /confirm endpoint).

    Returns a dict mapping ``tool_call_id`` → ``"approved"`` | ``"rejected"``
    on success, or ``None`` if the flow should be aborted.

    Raises ``TimeoutError`` if the first confirmation times out.
    """
    if not pending_tools:
        return {}

    # Send confirmation request for the first pending tool
    tc = pending_tools[0]
    tool_call_id = tc.get("id", "")
    tool_name = tc.get("name", "")
    risk_level = tc.get("_harness_risk_level", "medium")
    cooling_off = tc.get("_harness_cooling_off", 0)
    reason = tc.get("_harness_reason", "")
    tool_args = tc.get("arguments", {})

    await send_event(
        SseEvent.CONFIRM_REQUEST,
        {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "risk_level": risk_level,
            "args_preview": str(tool_args)[:200],
            "cooling_off_seconds": cooling_off,
            "reason": reason,
            "message": (
                f"Operation requires confirmation: {tool_name}. "
                f"Reason: {reason or 'high-risk operation'}. "
                f"Risk level: {risk_level}. "
                f"Approve or reject within {int(approval_timeout)}s."
            ),
        },
    )

    if pending_approvals is None or chat_id is None:
        return None

    future = asyncio.Future()
    # Use tool_call_id as the key for granular per-tool confirmation
    approval_key = f"{chat_id}:{tool_call_id}"
    pending_approvals[approval_key] = future

    try:
        decision = await asyncio.wait_for(future, timeout=approval_timeout)
        logger.info(
            "confirm_resolved",
            tool_call_id=tool_call_id,
            decision=decision,
            chat_id=chat_id,
        )
        return {tool_call_id: decision}
    except TimeoutError:
        logger.warning("confirm_timeout", tool_call_id=tool_call_id, chat_id=chat_id)
        await send_event(
            SseEvent.CONFIRM_TIMEOUT,
            {
                "tool_call_id": tool_call_id,
                "chat_id": chat_id,
                "message": f"Confirmation timed out for {tool_name}",
            },
        )
        return None
    finally:
        pending_approvals.pop(approval_key, None)


async def _run_agent_graph(
    graph: CompiledStateGraph,
    graph_config: RunnableConfig,
    initial_input: dict | Any,
    send_event: Callable[[SseEvent, dict], Any],
    pending_approvals: dict[str, asyncio.Future] | None = None,
    chat_id: str | None = None,
    approval_timeout: float = 60.0,
) -> bool:
    """Execute the agent graph and dispatch events via send_event callback.

    Shared by both WebSocket and SSE endpoints. Returns True if the task
    completed normally, False if interrupted or failed.

    Uses state-based confirmation: when the graph returns
    ``_awaiting_confirmation``, this function sends a ``CONFIRM_REQUEST``
    to the frontend, waits for user input via ``asyncio.Future`` +
    ``asyncio.wait_for``, then re-invokes the graph targeting the
    ``confirm`` node with the collected decisions.

    No ``interrupt()`` or ``Command(resume=decision)`` is used — all
    confirmation handling is externalized to the application layer.
    """

    task_completed = False
    all_decisions: dict[str, str] = {}

    while True:
        # Build input with accumulated confirmation decisions
        input_data: Any = initial_input
        if all_decisions:
            if isinstance(input_data, dict):
                input_data = {**input_data, "_confirmation_results": all_decisions}

        loop_done = False
        try:
            async for event in graph.astream(
                input_data,
                config=graph_config,
                stream_mode="updates",
            ):
                # Check for awaiting confirmation signal
                for node_name, node_output in event.items():
                    if node_name == "confirm" and node_output.get("_awaiting_confirmation"):
                        pending_tools = node_output["_awaiting_confirmation"]
                        decisions = await _handle_confirmation_request(
                            pending_tools,
                            send_event,
                            pending_approvals,
                            chat_id,
                            approval_timeout,
                        )
                        if decisions is None:
                            return False
                        all_decisions.update(decisions)
                        loop_done = True
                        break

                if loop_done:
                    break

                node_completed = await _dispatch_node_events(event, send_event)
                if node_completed:
                    task_completed = True

            if not loop_done:
                # Graph completed without needing confirmation
                break

        except Exception as e:
            logger.error("graph_execution_error", error=str(e))
            await send_event(SseEvent.ERROR, {"message": str(e)})
            return False

    return task_completed


async def _fire_extraction(
    session_id: str, user_id: str
) -> None:
    """Fire-and-forget conversation extraction."""
    try:
        from athena.tasks.dispatcher import dispatch_extract_conversation
        await dispatch_extract_conversation(session_id, user_id)
    except Exception as err:
        logger.warning("extraction_trigger_failed", error=str(err))


# ── WebSocket endpoint ─────────────────────────────────────────────


@router.websocket("/ws/im/web")
async def web_socket(websocket: WebSocket) -> None:
    """Bidirectional WebSocket for real-time agent communication.

    Client → Server message protocol:
      {type: "message", content: "...", chat_id: "..."}
      {type: "confirm", chat_id: "...", approved: true/false}

    Server → Client event protocol:
      {type: "event_name", data: {...}}

    Uses asyncio.Future + asyncio.wait_for for confirmation waiting,
    keeping a single WebSocket connection throughout the agent lifecycle.
    """
    await websocket.accept()
    config = get_config()
    user = config.user.web

    if not user.user_id:
        await websocket.send_json(
            {"type": SseEvent.ERROR.value, "data": {"message": "Web channel not configured"}}
        )
        await websocket.close(code=1008, reason="channel not configured")
        return

    pending_approvals: dict[str, asyncio.Future] = {}

    async def send_ws_event(event_type: SseEvent, data: dict) -> None:
        """Send a typed event over WebSocket."""
        await websocket.send_json({"type": event_type.value, "data": data})

    async def run_message_task(msg: dict) -> None:
        """Execute the agent graph for a user message with Future-based confirmations."""
        from athena.core.context import ContextManager
        from athena.core.graph import build_agent_graph
        from athena.core.graph.agent_graph import create_checkpointer

        content = msg.get("content", "")
        chat_id = msg.get("chat_id", "")
        if not chat_id:
            await send_ws_event(SseEvent.ERROR, {"message": "chat_id is required"})
            return

        await send_ws_event(SseEvent.AGENT_THINKING, {})

        unified = UnifiedMessage(
            message_id=f"ws_{asyncio.get_event_loop().time()}",
            channel="web",
            user_id=user.user_id,
            chat_id=chat_id,
            content=content,
            attachments=msg.get("attachments", []),
            chat_type="private",
        )

        checkpointer = None
        try:
            context_mgr = ContextManager()
            session = await context_mgr.get_or_create_session(
                unified.user_id, unified.channel, unified.chat_id
            )

            checkpointer = await create_checkpointer(config.sqlite_db_path)
            graph = build_agent_graph(checkpointer=checkpointer)

            initial_state = {"messages": [HumanMessage(content=unified.content)]}
            graph_config: RunnableConfig = {
                "configurable": {"thread_id": session.session_id}
            }

            task_completed = await _run_agent_graph(
                graph,
                graph_config,
                initial_state,
                send_ws_event,
                pending_approvals=pending_approvals,
                chat_id=chat_id,
            )

            if task_completed:
                await _fire_extraction(session.session_id, unified.user_id)

        except Exception as e:
            logger.error("ws_message_error", error=str(e))
            await send_ws_event(SseEvent.ERROR, {"message": str(e)})
        finally:
            if checkpointer is not None:
                await checkpointer.conn.close()

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "")

            if msg_type == "message":
                asyncio.create_task(run_message_task(msg))

            elif msg_type == "confirm":
                chat_id = msg.get("chat_id", "")
                tool_call_id = msg.get("tool_call_id", "")
                approved = msg.get("approved", False)
                # Use compound key: chat_id:tool_call_id for per-tool confirmation
                approval_key = f"{chat_id}:{tool_call_id}" if tool_call_id else chat_id
                future = pending_approvals.get(approval_key)
                if future and not future.done():
                    future.set_result("approved" if approved else "rejected")
                else:
                    await send_ws_event(
                        SseEvent.ERROR,
                        {
                            "message": (
                                f"No pending confirmation for "
                                f"tool_call_id={tool_call_id} in chat={chat_id}"
                            ),
                        },
                    )

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            else:
                await send_ws_event(
                    SseEvent.ERROR,
                    {"message": f"Unknown message type: {msg_type}"},
                )
    except WebSocketDisconnect:
        logger.info("ws_client_disconnected")
    except Exception as e:
        logger.error("ws_error", error=str(e))


# ── SSE endpoints (fallback / backward compat) ─────────────────────


@router.post("/im/web/message")
async def web_message(
    req: MessageRequest,
) -> StreamingResponse:
    """Send a message via the Web console channel.

    Returns an SSE stream with tool-calling agent events:
    - agent_thinking — LLM is generating a response
    - text_delta — LLM text output (streamed or full)
    - tool_call_start, tool_call_result — tool execution progress
    - confirm_required — high-risk operation needs user approval
    - task_completed, task_failed — terminal state
    - error — execution error
    """
    config = get_config()
    user = config.user.web
    if not user.user_id:
        raise HTTPException(status_code=400, detail="Web channel is not configured")
    if not req.chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required for web messages")

    # Build UnifiedMessage
    unified = UnifiedMessage(
        message_id=f"web_{asyncio.get_event_loop().time()}",
        channel="web",
        user_id=user.user_id,
        chat_id=req.chat_id,
        content=req.content,
        attachments=req.attachments or [],
        chat_type="private",
    )

    async def sse_event_stream() -> AsyncGenerator[str]:
        """Generate SSE events from the LangGraph agent graph."""
        checkpointer = None

        try:
            yield _sse_event(SseEvent.AGENT_THINKING, {})

            from athena.core.context import ContextManager
            from athena.core.graph import build_agent_graph
            from athena.core.graph.agent_graph import create_checkpointer

            context_mgr: ContextManager = ContextManager()
            session = await context_mgr.get_or_create_session(
                unified.user_id, unified.channel, unified.chat_id
            )

            checkpointer = await create_checkpointer(config.sqlite_db_path)
            graph: CompiledStateGraph = build_agent_graph(checkpointer=checkpointer)

            initial_state = {"messages": [HumanMessage(content=unified.content)]}
            graph_config: RunnableConfig = {
                "configurable": {"thread_id": session.session_id}
            }

            collector = _SSEEventCollector()
            task_completed = False
            sse_decisions: dict[str, str] = {}

            while True:
                input_state: Any = initial_state
                if sse_decisions:
                    input_state = {**initial_state, "_confirmation_results": sse_decisions}

                loop_done = False
                async for event in graph.astream(
                    input_state,
                    config=graph_config,
                    stream_mode="updates",
                ):
                    # Detect awaiting confirmation signal
                    for node_name, node_output in event.items():
                        if node_name == "confirm" and node_output.get("_awaiting_confirmation"):
                            pending_tools = node_output["_awaiting_confirmation"]
                            decisions = await _handle_confirmation_request(
                                pending_tools, collector
                            )
                            for sse_str in collector.drain():
                                yield sse_str
                            if decisions is None:
                                loop_done = True
                                break
                            sse_decisions.update(decisions)
                            loop_done = True
                            break

                    if loop_done:
                        break

                    node_completed = await _dispatch_node_events(event, collector)
                    for sse_str in collector.drain():
                        yield sse_str
                    if node_completed:
                        task_completed = True

                if not loop_done:
                    break

            if task_completed:
                try:
                    from athena.tasks.dispatcher import dispatch_extract_conversation
                    await dispatch_extract_conversation(
                        session.session_id, unified.user_id
                    )
                except Exception as extract_err:
                    logger.warning("extraction_trigger_failed", error=str(extract_err))

        except Exception as e:
            logger.error("web_sse_error", error=str(e))
            yield _sse_event(SseEvent.ERROR, {"code": "EXECUTION_ERROR", "message": str(e)})

        finally:
            if checkpointer is not None:
                await checkpointer.conn.close()

    return StreamingResponse(
        sse_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/im/web/message/confirm")
async def web_confirm(
    req: ConfirmRequest,
) -> StreamingResponse:
    """Submit confirmation and stream resumed graph execution via SSE.

    When the user approves/rejects a pending confirmation, this endpoint
    re-invokes the graph with the confirmation decision passed via state
    (``_confirmation_results``) and streams the subsequent events (tool
    results, agent response, etc.) back as an SSE stream.

    No ``Command(resume=decision)`` is used — the decision is injected
    via state and the graph resumes from the ``confirm`` node.
    """

    from athena.core.graph import build_agent_graph
    from athena.core.graph.agent_graph import create_checkpointer

    # ── Resolve thread_id / user_id ────────────────────────────────────
    config = get_config()
    user_id = config.user.web.user_id
    chat_id = req.chat_id

    decision = "approved" if req.approved else "rejected"

    async def sse_resume_stream() -> AsyncGenerator[str]:
        """Generate SSE events from the resumed graph execution."""
        checkpointer = None

        try:
            from athena.core.context import ContextManager

            context_mgr = ContextManager()
            session = await context_mgr.get_or_create_session(user_id, "web", chat_id)

            checkpointer = await create_checkpointer(config.sqlite_db_path)
            graph: CompiledStateGraph = build_agent_graph(checkpointer=checkpointer)

            graph_config: RunnableConfig = {
                "configurable": {"thread_id": session.session_id}
            }

            logger.info(
                "web_confirm_resuming",
                session_id=session.session_id,
                approved=req.approved,
            )

            collector = _SSEEventCollector()
            # Build initial state with the confirmation decision
            confirm_state: dict[str, Any] = {
                "_confirmation_results": {"_pending": decision},
            }

            async for event in graph.astream(
                confirm_state,
                config=graph_config,
                stream_mode="updates",
            ):
                # Detect awaiting confirmation for nested confirmations
                for node_name, node_output in event.items():
                    if node_name == "confirm" and node_output.get("_awaiting_confirmation"):
                        pending_tools = node_output["_awaiting_confirmation"]
                        # For SSE resume, we can't wait — just send the request
                        await _handle_confirmation_request(pending_tools, collector)
                        for sse_str in collector.drain():
                            yield sse_str
                        return  # End stream; user must confirm via another /confirm call

                await _dispatch_node_events(event, collector)
                for sse_str in collector.drain():
                    yield sse_str

            logger.info(
                "web_confirm_resumed",
                session_id=session.session_id,
                approved=req.approved,
            )

        except Exception as e:
            logger.error("web_confirm_error", error=str(e))
            yield _sse_event(SseEvent.ERROR, {"code": "EXECUTION_ERROR", "message": str(e)})

        finally:
            if checkpointer is not None:
                await checkpointer.conn.close()

    return StreamingResponse(
        sse_resume_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/im/web/sessions")
async def web_sessions() -> dict[str, list[dict[str, Any]]]:
    """Return all non-deleted web sessions for the current user.

    Queries the ``sessions`` table filtered by channel=web,
    user_id from config, and delete_time IS NULL.
    Results are ordered by created_at DESC.
    """
    from sqlalchemy import select

    from athena.models import get_session_maker
    from athena.models.session import Session as SessionModel

    config = get_config()
    user_id = config.user.web.user_id
    session_maker = get_session_maker(config.sqlite_db_path)

    try:
        async with session_maker() as db:
            result = await db.execute(
                select(SessionModel)
                .where(
                    SessionModel.channel == "web",
                    SessionModel.user_id == user_id,
                    SessionModel.delete_time.is_(None),
                )
                .order_by(SessionModel.created_at.desc())
            )
            rows = result.scalars().all()

        sessions = [
            {
                "id": row.chat_id,
                "title": row.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if row.created_at
                else row.chat_id,
                "createdAt": int(row.created_at.timestamp() * 1000) if row.created_at else 0,
            }
            for row in rows
        ]
        return {"sessions": sessions}

    except Exception as e:
        logger.error("web_sessions_error", error=str(e))
        return {"sessions": []}


@router.delete("/im/web/session/{chat_id}")
async def delete_web_session(
    chat_id: str,
) -> dict[str, Any]:
    """Soft-delete a web session by setting delete_time.

    Only the session owner (matched by user_id) can delete it.
    """
    from datetime import datetime

    from sqlalchemy import select

    from athena.models import get_session_maker
    from athena.models.session import Session as SessionModel

    config = get_config()
    user_id = config.user.web.user_id
    session_maker = get_session_maker(config.sqlite_db_path)

    try:
        async with session_maker() as db:
            result = await db.execute(
                select(SessionModel).where(
                    SessionModel.chat_id == chat_id,
                    SessionModel.user_id == user_id,
                    SessionModel.channel == "web",
                )
            )
            session = result.scalar_one_or_none()

            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            session.delete_time = datetime.now(UTC)
            await db.commit()

        logger.info("web_session_deleted", chat_id=chat_id)
        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_web_session_error", chat_id=chat_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to delete session") from e


@router.get("/im/web/history")
async def web_history(
    chat_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return message history for a web session from the LangGraph checkpointer.

    The checkpointer stores full agent state (including messages) keyed by
    ``thread_id``.  This endpoint reads the latest checkpoint and extracts
    the user-visible messages (HumanMessage / AIMessage) in the format
    expected by the frontend chat store.
    """
    import time as _time

    from langchain_core.messages import AIMessage, HumanMessage

    from athena.core.context import ContextManager
    from athena.core.graph.agent_graph import create_checkpointer

    config = get_config()
    user_id = config.user.web.user_id
    session_id = ContextManager.make_session_id(user_id, "web", chat_id)
    logger.info("web_history", session_id=session_id)
    checkpointer = None
    try:
        checkpointer = await create_checkpointer(config.sqlite_db_path)
        checkpoint_tuple = await checkpointer.aget_tuple(
            {"configurable": {"thread_id": session_id}}
        )

        if checkpoint_tuple is None:
            return {"messages": []}

        # Messages are stored in channel_values by the add_messages reducer
        channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})
        raw_messages = channel_values.get("messages", [])

        now_ms = int(_time.time() * 1000)
        result: list[dict[str, Any]] = []
        for msg in raw_messages:
            if isinstance(msg, HumanMessage):
                result.append(
                    {
                        "role": "user",
                        "content": msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content),
                        "timestamp": now_ms,
                    }
                )
            elif isinstance(msg, AIMessage):
                # Skip empty AI messages (tool-calling stubs with no content)
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if not content.strip() and not msg.tool_calls:
                    continue
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                    "timestamp": now_ms,
                }
                if msg.tool_calls:
                    entry["toolCalls"] = [
                        {
                            "tool_name": tc.get("name", "unknown"),
                            "args_preview": str(tc.get("args", {}))[:200],
                            "status": "success",
                        }
                        for tc in msg.tool_calls
                    ]
                result.append(entry)

        # Assign stable IDs after filtering
        for idx, item in enumerate(result):
            item["id"] = f"hist_{chat_id}_{idx}"

        return {"messages": result}

    except Exception as e:
        logger.error("web_history_error", chat_id=chat_id, error=str(e))
        return {"messages": []}
    finally:
        if checkpointer is not None:
            await checkpointer.conn.close()


def _sse_event(event_type: SseEvent, data: dict) -> str:
    """Format a Server-Sent Events message."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type.value}\ndata: {payload}\n\n"
