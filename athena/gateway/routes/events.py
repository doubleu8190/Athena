"""面向浏览器客户端的会话级有序 SSE 事件流。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from athena.contracts.errors import ErrorDetail
from athena.container import get_runtime_container
from athena.infrastructure.sqlite.repositories import _json_loads

router = APIRouter(prefix="/sessions", tags=["events"])


def _format_sse_event(
    payload: dict[str, Any],
    event_type: str | None = None,
    session_seq: int | None = None,
) -> str:
    """编码单条 SSE 帧，并将会话序号写入 SSE id。"""
    lines: list[str] = []
    if session_seq is not None:
        lines.append(f"id: {session_seq}")
    if event_type:
        lines.append(f"event: {event_type}")
    lines.append("data: " + json.dumps(payload, ensure_ascii=False))
    return "\n".join(lines) + "\n\n"


def _event_record_to_payload(record: Any) -> dict[str, Any]:
    """将数据库事件转换为稳定的 v2 Envelope。"""
    return {
        "schema_version": 2,
        "session_seq": record.session_seq,
        "event_type": record.event_type,
        "durability": record.durability,
        "session_id": record.session_id,
        "run_id": record.run_id,
        "message_id": record.message_id,
        "attachment_id": record.attachment_id,
        "stream_id": record.stream_id,
        "stream_type": record.stream_type,
        "chunk_id": record.chunk_id,
        "is_complete": bool(record.is_complete),
        "parent_run_id": record.parent_run_id,
        "transition_id": record.transition_id,
        "payload": _json_loads(record.payload_json, {}),
        "occurred_at": record.occurred_at,
    }


@router.get("/{session_id}/events")
async def session_events(
    session_id: str, request: Request, after: int = 0
) -> StreamingResponse:
    """先重放 watermark 之前的事件，再消费同一总线的实时通知。"""
    runtime = get_runtime_container(request)
    agent_store = runtime.agent_store
    transport = runtime.realtime_transport
    if agent_store is None:
        raise HTTPException(status_code=500, detail=ErrorDetail.AGENT_STORE_NOT_FOUND)
    if transport is None:
        raise HTTPException(
            status_code=500, detail=ErrorDetail.REALTIME_TRANSPORT_NOT_FOUND
        )
    if not await runtime.db.sessions.get(session_id):
        raise HTTPException(status_code=404, detail=ErrorDetail.SESSION_NOT_FOUND)

    last_header = request.headers.get("last-event-id")
    if last_header and last_header.isdigit():
        after = int(last_header)

    async def stream() -> AsyncIterator[str]:
        queue, watermark = await agent_store.open_subscription(session_id)
        cursor = after
        try:
            for row in await agent_store.list_events_between(
                session_id, after, watermark
            ):
                seq = row.session_seq
                if seq <= cursor:
                    continue
                cursor = seq
                yield _format_sse_event(
                    _event_record_to_payload(row), row.event_type, seq
                )

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                seq = event.session_seq
                if seq is None:
                    # Realtime delta 只存在于在线连接中，不参与 Last-Event-ID。
                    yield _format_sse_event(
                        event.model_dump(mode="json"), event.event_type
                    )
                    continue
                if seq <= cursor:
                    continue
                cursor = seq
                yield _format_sse_event(
                    event.model_dump(mode="json"), event.event_type, seq
                )
        finally:
            await transport.close_subscription(session_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
