"""记录 Root 与 Worker 模型调用的统一活动事件。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4

from domain.events import ApplicationEvent, EventDurability
from domain.events.ports import EventPort
from domain.integrations import LLMPort, StructuredOutputSchema


async def invoke_llm_with_activity(
    llm: LLMPort,
    event_publisher: EventPort | None,
    messages: list[Any],
    *,
    session_id: str,
    run_id: str,
    parent_activity_id: str,
) -> Any:
    """调用普通模型输出并发布开始、完成或失败事件。"""
    return await _invoke_with_activity(
        lambda: llm.invoke(messages),
        event_publisher,
        session_id=session_id,
        run_id=run_id,
        parent_activity_id=parent_activity_id,
    )


async def invoke_structured_llm_with_activity(
    llm: LLMPort,
    event_publisher: EventPort | None,
    messages: list[Any],
    schema: StructuredOutputSchema,
    *,
    session_id: str,
    run_id: str,
    parent_activity_id: str,
) -> Any:
    """调用结构化模型输出并发布开始、完成或失败事件。"""
    return await _invoke_with_activity(
        lambda: llm.invoke_structured(messages, schema),
        event_publisher,
        session_id=session_id,
        run_id=run_id,
        parent_activity_id=parent_activity_id,
    )


async def _invoke_with_activity(
    invoke: Callable[[], Awaitable[Any]],
    event_publisher: EventPort | None,
    *,
    session_id: str,
    run_id: str,
    parent_activity_id: str,
) -> Any:
    """统一包裹模型调用与活动事件；事件故障不覆盖模型结果。"""
    call_id = f"llm:{uuid4().hex}"
    started_at = datetime.now(timezone.utc)
    started_clock = perf_counter()
    await _publish_llm_event(
        event_publisher,
        event_type="llm.started",
        session_id=session_id,
        run_id=run_id,
        call_id=call_id,
        parent_activity_id=parent_activity_id,
        status="running",
        started_at=started_at,
    )
    try:
        result = await invoke()
    except Exception as error:
        await _publish_llm_event(
            event_publisher,
            event_type="llm.completed",
            session_id=session_id,
            run_id=run_id,
            call_id=call_id,
            parent_activity_id=parent_activity_id,
            status="failed",
            started_at=started_at,
            duration_ms=int((perf_counter() - started_clock) * 1000),
            error=type(error).__name__,
        )
        raise
    await _publish_llm_event(
        event_publisher,
        event_type="llm.completed",
        session_id=session_id,
        run_id=run_id,
        call_id=call_id,
        parent_activity_id=parent_activity_id,
        status="completed",
        started_at=started_at,
        duration_ms=int((perf_counter() - started_clock) * 1000),
    )
    return result


async def _publish_llm_event(
    event_publisher: EventPort | None,
    *,
    event_type: str,
    session_id: str,
    run_id: str,
    call_id: str,
    parent_activity_id: str,
    status: str,
    started_at: datetime,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    """写入一条模型活动事件；遥测故障不能覆盖业务执行结果。"""
    if event_publisher is None:
        return
    payload: dict[str, Any] = {
        "activity_id": call_id,
        "activity_type": "llm",
        "call_id": call_id,
        "name": "llm.generate",
        "parent_activity_id": parent_activity_id,
        "status": status,
        "started_at": started_at.isoformat(),
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    if error:
        payload["error"] = {
            "code": "llm_failed",
            "message": error,
            "stage": "invoke",
        }
    try:
        await event_publisher.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                payload=payload,
            )
        )
    except Exception:
        return
__all__ = ["invoke_llm_with_activity", "invoke_structured_llm_with_activity"]
