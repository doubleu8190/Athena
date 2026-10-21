"""可靠投递 Run/Plan 取消事实到持久 SSE 事件流。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from domain.events import ApplicationEvent, EventDurability, EventType, EventPort
from domain.orchestration import (
    RunCancellationOutboxLease,
    RunCancellationOutboxRepository,
)

logger = logging.getLogger(__name__)


_EVENT_TYPES = {
    "PlanCancelling": (EventType.PLAN_CANCELLING, "cancelling", "plan"),
    "PlanCancelled": (EventType.PLAN_CANCELLED, "cancelled", "plan"),
    "RunCancelled": (EventType.RUN_CANCELLED, "cancelled", "run"),
}


class RunCancellationOutboxDispatcher:
    """通过可恢复租约投递取消通知；Outbox 状态与 SSE 历史分开保存。"""

    def __init__(
        self,
        outbox: RunCancellationOutboxRepository,
        events: EventPort,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
        poll_interval: float = 0.5,
    ) -> None:
        if not worker_id.strip() or lease_seconds <= 0 or poll_interval <= 0:
            raise ValueError("worker identity, lease, and polling must be positive")
        self._outbox = outbox
        self._events = events
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动持久 Outbox 拉取循环。"""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self.run(), name=f"athena-cancellation-outbox:{self._worker_id}"
        )

    async def stop(self) -> None:
        """停止领取，并等待当前投递结束。"""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self) -> None:
        """顺序投递到期消息；数据库错误由租约恢复和下轮重试兜底。"""
        while not self._stop.is_set():
            try:
                claimed = await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Run cancellation Outbox claim failed")
                claimed = False
            if not claimed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass

    async def dispatch_once(self) -> bool:
        """投递一个事件；无到期消息时返回 False。"""
        lease = await self._outbox.claim_next_cancellation_event(
            self._worker_id, lease_seconds=self._lease_seconds
        )
        if lease is None:
            return False
        try:
            event = _application_event(lease)
            await self._events.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = min(30.0, 0.5 * (2 ** min(lease.attempt - 1, 6)))
            await self._outbox.retry_cancellation_event(
                lease, delay_seconds=delay
            )
            logger.exception(
                "Run cancellation Outbox delivery failed: message=%s attempt=%s",
                lease.message_id,
                lease.attempt,
            )
            return True

        if not await self._outbox.mark_cancellation_event_published(lease):
            logger.warning(
                "Run cancellation Outbox lease expired before acknowledgement: %s",
                lease.message_id,
            )
        return True


def _application_event(lease: RunCancellationOutboxLease) -> ApplicationEvent:
    """把持久 Outbox 信封转换为稳定、可幂等重放的 SSE 事件。"""
    try:
        event_type, status, activity_type = _EVENT_TYPES[lease.event_type]
    except KeyError as exc:
        raise ValueError(f"unsupported cancellation event: {lease.event_type}") from exc

    activity_identity = (
        lease.plan_id if activity_type == "plan" else lease.run_id
    )
    activity_id = f"{activity_type}:{activity_identity}"
    payload: dict[str, Any] = {
        "activity_id": activity_id,
        "activity_type": activity_type,
        "name": "execution_plan" if activity_type == "plan" else "run",
        "plan_id": lease.plan_id,
        "status": status,
        "cancellation_epoch": lease.payload.get("cancellation_epoch"),
        "external_effect_status": lease.payload.get(
            "external_effect_status", "unknown"
        ),
        "outbox_message_id": lease.message_id,
    }
    return ApplicationEvent(
        event_type=event_type,
        durability=EventDurability.DURABLE,
        session_id=lease.session_id,
        run_id=lease.run_id,
        transition_id=f"orchestration-outbox:{lease.message_id}",
        occurred_at=lease.occurred_at,
        payload=payload,
    )


__all__ = ["RunCancellationOutboxDispatcher"]
