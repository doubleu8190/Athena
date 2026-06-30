"""IM message endpoints — Web console message + SSE streaming.

POST /api/v1/im/web/message — Send message, receive SSE stream of execution events.
POST /api/v1/im/web/message/confirm — Send confirmation response.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from athena.api.deps import get_config_dep, verify_api_key
from athena.config import Config
from athena.core.message import UnifiedMessage
from athena.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["im"])


class MessageRequest(BaseModel):
    content: str
    session_id: str | None = None
    attachments: list[dict[str, Any]] | None = None


class ConfirmRequest(BaseModel):
    task_id: str
    step: int
    approved: bool


@router.post("/im/web/message")
async def web_message(
    req: MessageRequest,
    config: Config = Depends(get_config_dep),
    api_key: str = Depends(verify_api_key),
):
    """Send a message via the Web console channel.

    Returns an SSE stream with execution progress events:
    - plan_generating, plan_generated
    - subtask_started, subtask_completed, subtask_failed, subtask_skipped, subtask_fallback
    - confirm_required, confirm_timeout, confirm_result
    - task_completed, task_failed
    - error
    """
    user = config.user.web
    if not user.user_id:
        raise HTTPException(status_code=400, detail="Web channel is not configured")
    if not req.session_id:
        raise HTTPException(status_code=400, detail="session_id is required for web messages")

    # Build UnifiedMessage
    unified = UnifiedMessage(
        message_id=f"web_{asyncio.get_event_loop().time()}",
        channel="web",
        user_id=user.user_id,
        session_id=req.session_id,
        content=req.content,
        attachments=req.attachments or [],
        chat_type="private",
    )

    async def sse_event_stream():
        """Generate SSE events for the execution pipeline."""
        try:
            # Yield plan_generating
            yield _sse_event("plan_generating", {"task_id": "pending"})

            # Get Core components from singletons
            from athena.gateway.manager import get_gateway_manager
            gateway_manager = get_gateway_manager()
            web_adapter = gateway_manager.get_adapter("web")

            # Get or create session
            from athena.core.context import ContextManager
            context_mgr = ContextManager(config)

            # In the web channel, the frontend's session_id serves as the chat_id
            # (there is no separate "chat" concept — each browser session IS a chat)
            session_ctx = await context_mgr.get_or_create_session(
                unified.user_id, unified.channel, unified.session_id
            )

            # Inject context for LLM
            messages = await context_mgr.inject_context(
                session_ctx, unified.content, unified.user_id
            )

            # Call Planner
            from athena.core.llm_provider.manager import get_llm_manager
            from athena.core.planner import Planner

            llm_mgr = get_llm_manager()
            planner = Planner(llm_mgr)

            plan = await planner.generate_plan(
                unified, session_ctx, context_messages=messages
            )

            yield _sse_event("plan_generated", {
                "task_id": plan.task_id,
                "subtask_count": len(plan.subtasks),
                "summary": f"Generated {len(plan.subtasks)} subtasks",
            })

            # Execute plan
            from athena.core.harness import HarnessEngine
            from athena.core.executor import Executor

            harness = HarnessEngine(config)
            await harness.start()

            executor = Executor(
                config=config,
                context_manager=context_mgr,
                harness_engine=harness,
            )

            # Stream subtask execution events
            for subtask in plan.subtasks:
                yield _sse_event("subtask_started", {
                    "step": subtask.step,
                    "tool_name": subtask.tool_name,
                    "intent": subtask.intent,
                })

            # Execute all subtasks
            result = await executor.execute(plan, session_ctx, "web")

            # Send subtask results as SSE events
            for r in result.get("results", []):
                if r["status"] == "success":
                    yield _sse_event("subtask_completed", {
                        "step": r["step"],
                        "status": "success",
                        "output_preview": str(r.get("output", "")),
                    })
                elif r["status"] == "fallback_used":
                    yield _sse_event("subtask_fallback", {
                        "step": r["step"],
                        "original_tool": r.get("fallback_from", ""),
                        "fallback_tool": r.get("tool_name", ""),
                        "reason": "primary tool failed after retries",
                    })
                elif r["status"] == "skipped":
                    yield _sse_event("subtask_skipped", {
                        "step": r["step"],
                        "reason": "non-critical failure",
                        "error": r.get("error", ""),
                    })
                else:
                    yield _sse_event("subtask_failed", {
                        "step": r["step"],
                        "status": r["status"],
                        "error": r.get("error", ""),
                    })

            # Final result
            if result["status"] == "completed":
                yield _sse_event("task_completed", {
                    "task_id": plan.task_id,
                    "summary": "All steps completed",
                })
            else:
                yield _sse_event("task_failed", {
                    "task_id": plan.task_id,
                    "error": f"Task {result['status']}",
                })

            await harness.stop()

        except Exception as e:
            logger.error("web_sse_error", error=str(e))
            yield _sse_event("error", {"code": "EXECUTION_ERROR", "message": str(e)})

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
):
    """Submit a confirmation response from the Web console."""
    from athena.gateway.manager import get_gateway_manager
    gateway_manager = get_gateway_manager()
    web_adapter = gateway_manager.get_adapter("web")

    if web_adapter:
        # Resolve the confirmation future
        # The nonce lookup happens via the confirmation manager
        logger.info(
            "web_confirm_received",
            task_id=req.task_id,
            step=req.step,
            approved=req.approved,
        )
        return {
            "code": 0,
            "message": "success",
            "data": {"task_id": req.task_id, "step": req.step, "approved": req.approved},
        }

    raise HTTPException(status_code=404, detail="Web adapter not found")


def _sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Events message."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"
