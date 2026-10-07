"""会话级有序 SSE 事件流。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from application.events import (
    EventSessionNotFoundError,
    EventStreamService,
)
from domain.events import ApplicationEvent


def _event_payload(event: ApplicationEvent) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "session_seq": event.session_seq,
        "event_type": str(event.event_type),
        "durability": str(event.durability),
        "session_id": event.session_id,
        "run_id": event.run_id,
        "message_id": event.message_id,
        "attachment_id": event.attachment_id,
        "stream_id": event.stream_id,
        "stream_type": event.stream_type,
        "chunk_id": event.chunk_id,
        "is_complete": event.is_complete,
        "parent_run_id": event.parent_run_id,
        "transition_id": event.transition_id,
        "occurred_at": event.occurred_at.isoformat(),
        "payload": event.payload,
    }


def _format_event(event: ApplicationEvent) -> str:
    lines: list[str] = []
    if event.session_seq is not None:
        lines.append(f"id: {event.session_seq}")
    if event.event_type:
        lines.append(f"event: {event.event_type}")
    lines.append("data: " + json.dumps(_event_payload(event), ensure_ascii=False))
    return "\n".join(lines) + "\n\n"


def build_events_router(
    stream_service_factory: Callable[[], EventStreamService],
) -> APIRouter:
    router = APIRouter(prefix="/sessions", tags=["events"])

    def service() -> EventStreamService:
        return stream_service_factory()

    @router.get("/{session_id}/events")
    async def session_events(
        session_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        events: EventStreamService = Depends(service),
    ) -> StreamingResponse:
        last_header = request.headers.get("last-event-id")
        if last_header and last_header.isdigit():
            after = int(last_header)
        try:
            queue, watermark = await events.open(session_id, after)
        except EventSessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc

        async def stream() -> AsyncIterator[str]:
            cursor = after
            try:
                for event in await events.replay(session_id, after, watermark):
                    if event.session_seq is None or event.session_seq <= cursor:
                        continue
                    cursor = event.session_seq
                    yield _format_event(event)

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if event.session_seq is None:
                        yield _format_event(event)
                        continue
                    if event.session_seq <= cursor:
                        continue
                    cursor = event.session_seq
                    yield _format_event(event)
            finally:
                await events.close(session_id, queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = ["build_events_router"]
