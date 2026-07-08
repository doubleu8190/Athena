"""IM message endpoints — Web console message + SSE streaming.

POST /api/v1/im/web/message — Send message, receive SSE stream of agent events.
POST /api/v1/im/web/message/confirm — Submit confirmation response.

Powered by LangGraph tool-calling agent graph (LLM decides: answer or call tools).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph
from pydantic import BaseModel

from athena.api.deps import get_config_dep, verify_api_key
from athena.config import Config
from athena.models.session import Session
from athena.core.llm_provider.manager import LLMProviderManager
from athena.core.message import UnifiedMessage
from athena.logging_config import get_logger
from athena.mcp_client.client import MCPClient
from athena.mcp_client.registry import ToolRegistry

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


@router.post("/im/web/message")
async def web_message(
    req: MessageRequest,
    config: Config = Depends(get_config_dep),
    api_key: str = Depends(verify_api_key),
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

    async def sse_event_stream() -> AsyncGenerator[str, None]:
        """Generate SSE events from the LangGraph agent graph."""
        harness = None
        checkpointer = None

        try:
            yield _sse_event(SseEvent.AGENT_THINKING, {})

            # ── Setup: singletons and graph ────────────────────────────
            from athena.core.llm_provider.manager import get_llm_manager
            from athena.core.context import ContextManager
            from athena.core.harness import get_harness
            from athena.mcp_client.client import get_mcp_client
            from athena.mcp_client.registry import get_tool_registry
            from athena.core.graph import build_agent_graph
            from athena.core.graph.agent_graph import create_checkpointer
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            llm_manager: LLMProviderManager = get_llm_manager()
            context_mgr: ContextManager = ContextManager(config)

            # Get or create session
            session = await context_mgr.get_or_create_session(
                unified.user_id, unified.channel, unified.chat_id
            )

            # Harness engine — process-wide singleton, auto-started on first call
            harness = await get_harness()

            mcp_client: MCPClient = get_mcp_client()
            tool_registry: ToolRegistry = get_tool_registry()

            # Set up checkpointer for persistence (required for interrupt/resume)
            
            checkpointer: AsyncSqliteSaver = await create_checkpointer(config.sqlite_db_path)

            # Build the agent graph
            graph: StateGraph = build_agent_graph(
                checkpointer=checkpointer,
            )
            
            # Initial state — checkpointer restores prior messages(/history);
            # we only seed the dynamic system context + the new user message.
            initial_state = {
                "messages": [HumanMessage(content=unified.content)],
            }

            # Config with all live objects
            graph_config = {
                "configurable": {
                    "thread_id": session.session_id,
                    "session_id": session.session_id,
                    "llm_manager": llm_manager,
                    "mcp_client": mcp_client,
                    "harness_engine": harness,
                    "tool_registry": tool_registry,
                    "sqlite_db_path": config.sqlite_db_path,
                }
            }

            # ── Stream graph execution ─────────────────────────────────
            # 使用 stream_mode="updates"
            # event：仅包含发生变化的节点的更新（即每个节点返回的字典）。
            # 结构：一个字典，键为节点名称，值为该节点的状态更新。                
            async for event in graph.astream(
                initial_state,
                config=graph_config,
                stream_mode="updates",
            ):
                # Handle interrupt (confirmation required)
                if "__interrupt__" in event:
                    interrupt_info = event["__interrupt__"]
                    for entry in interrupt_info:
                        yield _sse_event(SseEvent.CONFIRM_REQUIRED, {
                            "tool_name": entry.value.get("tool_name", ""),
                            "risk_level": entry.value.get("risk_level", "medium"),
                            "args_preview": str(entry.value.get("args", {})),
                            "cooling_off_seconds": entry.value.get("cooling_off_seconds", 0),
                            "reason": entry.value.get("reason", ""),
                        })
                    # After yielding the interrupt, we end the stream.
                    # The frontend will call /confirm to resume.
                    break

                # Convert LangGraph events to SSE events
                for node_name, node_output in event.items():
                    if node_name == "agent":
                        # ── Agent node output ──────────────────────────
                        messages = node_output.get("messages", [])
                        status = node_output.get("status", "")

                        for msg in messages:
                            if hasattr(msg, "content") and msg.content:
                                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                                if content.strip():
                                    yield _sse_event(SseEvent.TEXT_DELTA, {
                                        "content": content,
                                    })

                            if hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    tc_name = tc.get("name", "unknown")
                                    tc_args = tc.get("args", tc.get("arguments", {}))
                                    yield _sse_event(SseEvent.TOOL_CALL_START, {
                                        "tool_name": tc_name,
                                        "args_preview": str(tc_args)[:200],
                                    })

                        if status == "completed":
                            yield _sse_event(SseEvent.TASK_COMPLETED, {
                                "summary": "Response complete",
                            })
                        elif status == "failed":
                            yield _sse_event(SseEvent.TASK_FAILED, {
                                "error": "Agent failed to generate a response",
                            })

                    elif node_name == "confirm":
                        # ── Confirm node output (blocked/rejected tools) ──
                        c_messages = node_output.get("messages", [])
                        for msg in c_messages:
                            if hasattr(msg, "content") and hasattr(msg, "tool_call_id"):
                                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                                tool_name = getattr(msg, "name", "unknown")
                                yield _sse_event(SseEvent.TOOL_CALL_RESULT, {
                                    "tool_name": tool_name,
                                    "tool_call_id": getattr(msg, "tool_call_id", ""),
                                    "success": False,
                                    "output_preview": content[:500],
                                })

                    elif node_name == "tools":
                        # ── Tools node output ──────────────────────────
                        t_messages = node_output.get("messages", [])
                        for msg in t_messages:
                            if hasattr(msg, "content") and hasattr(msg, "tool_call_id"):
                                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                                is_error = '"error"' in content[:100] if content else False
                                tool_name = getattr(msg, "name", "unknown")

                                yield _sse_event(SseEvent.TOOL_CALL_RESULT, {
                                    "tool_name": tool_name,
                                    "tool_call_id": getattr(msg, "tool_call_id", ""),
                                    "success": not is_error,
                                    "output_preview": content[:500],
                                })

        except Exception as e:
            logger.error("web_sse_error", error=str(e))
            yield _sse_event(SseEvent.ERROR, {"code": "EXECUTION_ERROR", "message": str(e)})

        finally:
            # harness is a process-wide singleton — do NOT stop it here
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
    config: Config = Depends(get_config_dep),
    api_key: str = Depends(verify_api_key),
) -> StreamingResponse:
    """Submit confirmation and stream resumed graph execution via SSE.

    When the user approves/rejects a pending confirmation, this endpoint
    resumes the paused graph with ``Command(resume=decision)`` and streams
    the subsequent events (tool results, agent response, etc.) back as an
    SSE stream so the frontend can continue updating the same assistant
    message in real time.
    """
    from langgraph.types import Command
    from athena.core.graph import build_agent_graph
    from athena.core.graph.agent_graph import create_checkpointer

    # ── Resolve thread_id / user_id ────────────────────────────────────
    user_id = config.user.web.user_id
    chat_id = req.chat_id

    decision = "approved" if req.approved else "rejected"

    async def sse_resume_stream() -> AsyncGenerator[str, None]:
        """Generate SSE events from the resumed graph execution."""
        checkpointer = None

        try:
            # ── Setup: singletons and graph ────────────────────────────
            from athena.core.llm_provider.manager import get_llm_manager
            from athena.core.context import ContextManager
            from athena.core.harness import get_harness
            from athena.mcp_client.client import get_mcp_client
            from athena.mcp_client.registry import get_tool_registry

            llm_manager = get_llm_manager()
            context_mgr = ContextManager(config)

            session = await context_mgr.get_or_create_session(
                user_id, "web", chat_id
            )

            harness = await get_harness()
            mcp_client = get_mcp_client()
            tool_registry = get_tool_registry()

            checkpointer = await create_checkpointer(config.sqlite_db_path)

            # Build graph — topology must match web_message for interrupt/resume.
            graph = build_agent_graph(
                checkpointer=checkpointer,
            )

            graph_config = {
                "configurable": {
                    "thread_id": session.session_id,
                    "session_id": session.session_id,
                    "llm_manager": llm_manager,
                    "mcp_client": mcp_client,
                    "harness_engine": harness,
                    "tool_registry": tool_registry,
                    "sqlite_db_path": config.sqlite_db_path,
                }
            }

            logger.info(
                "web_confirm_resuming",
                session_id=session.session_id,
                approved=req.approved,
            )

            # ── Stream resumed execution ───────────────────────────────
            async for event in graph.astream(
                Command(resume=decision),
                config=graph_config,
                stream_mode="updates",
            ):
                # Handle nested interrupt (agent may call another tool)
                if "__interrupt__" in event:
                    interrupt_info = event["__interrupt__"]
                    for entry in interrupt_info:
                        yield _sse_event(SseEvent.CONFIRM_REQUIRED, {
                            "tool_name": entry.value.get("tool_name", ""),
                            "risk_level": entry.value.get("risk_level", "medium"),
                            "args_preview": str(entry.value.get("args", {})),
                            "cooling_off_seconds": entry.value.get("cooling_off_seconds", 0),
                            "reason": entry.value.get("reason", ""),
                        })
                    break

                for node_name, node_output in event.items():
                    if node_name == "agent":
                        messages = node_output.get("messages", [])
                        status = node_output.get("status", "")

                        for msg in messages:
                            if hasattr(msg, "content") and msg.content:
                                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                                if content.strip():
                                    yield _sse_event(SseEvent.TEXT_DELTA, {
                                        "content": content,
                                    })

                            if hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    tc_name = tc.get("name", "unknown")
                                    tc_args = tc.get("args", tc.get("arguments", {}))
                                    yield _sse_event(SseEvent.TOOL_CALL_START, {
                                        "tool_name": tc_name,
                                        "args_preview": str(tc_args)[:200],
                                    })

                        if status == "completed":
                            yield _sse_event(SseEvent.TASK_COMPLETED, {
                                "summary": "Response complete",
                            })
                        elif status == "failed":
                            yield _sse_event(SseEvent.TASK_FAILED, {
                                "error": "Agent failed to generate a response",
                            })

                    elif node_name == "tools":
                        t_messages = node_output.get("messages", [])
                        for msg in t_messages:
                            if hasattr(msg, "content") and hasattr(msg, "tool_call_id"):
                                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                                is_error = '"error"' in content[:100] if content else False
                                tool_name = getattr(msg, "name", "unknown")

                                yield _sse_event(SseEvent.TOOL_CALL_RESULT, {
                                    "tool_name": tool_name,
                                    "tool_call_id": getattr(msg, "tool_call_id", ""),
                                    "success": not is_error,
                                    "output_preview": content[:500],
                                })

            logger.info(
                "web_confirm_resumed",
                session_id=session.session_id,
                approved=req.approved,
            )

        except Exception as e:
            logger.error("web_confirm_error", error=str(e))
            yield _sse_event(SseEvent.ERROR, {"code": "EXECUTION_ERROR", "message": str(e)})

        finally:
            # harness is a process-wide singleton — do NOT stop it here
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
async def web_sessions(
    config: Config = Depends(get_config_dep),
) -> dict[str, list[dict[str, Any]]]:
    """Return all non-deleted web sessions for the current user.

    Queries the ``sessions`` table filtered by channel=web,
    user_id from config, and delete_time IS NULL.
    Results are ordered by created_at DESC.
    """
    from sqlalchemy import select

    from athena.models import get_session_maker
    from athena.models.session import Session as SessionModel

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
                "title": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else row.chat_id,
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
    config: Config = Depends(get_config_dep),
) -> dict[str, Any]:
    """Soft-delete a web session by setting delete_time.

    Only the session owner (matched by user_id) can delete it.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from athena.models import get_session_maker
    from athena.models.session import Session as SessionModel

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

            session.delete_time = datetime.now(timezone.utc)
            await db.commit()

        logger.info("web_session_deleted", chat_id=chat_id)
        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_web_session_error", chat_id=chat_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to delete session")


@router.get("/im/web/history")
async def web_history(
    chat_id: str,
    config: Config = Depends(get_config_dep),
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

    user_id = config.user.web.user_id
    session_id = ContextManager.make_session_id(user_id, "web", chat_id)

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
                result.append({
                    "role": "user",
                    "content": msg.content if isinstance(msg.content, str) else str(msg.content),
                    "timestamp": now_ms,
                })
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


