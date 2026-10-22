"""记录 Root 与 Worker 模型调用的统一活动事件。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from domain.events import ApplicationEvent, EventDurability
from domain.events.ports import EventPort


class LLMInvoker(Protocol):
    """支持异步消息调用的模型接口。"""

    async def invoke(self, messages: Sequence[Any]) -> Any: ...


async def invoke_llm_with_activity(
    llm: LLMInvoker,
    event_publisher: EventPort | None,
    messages: Sequence[Any],
    *,
    session_id: str,
    run_id: str,
    parent_activity_id: str,
) -> Any:
    """调用模型并发布开始、完成或失败事件。
    模型错误继续交给工作流处理；活动事件发布失败则不改变模型调用结果。
    """
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
        result = await llm.invoke(messages)
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
__all__ = ["LLMInvoker", "invoke_llm_with_activity"]
