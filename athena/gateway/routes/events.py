"""面向浏览器客户端的会话级有序 SSE 事件流。"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from athena.contracts.errors import ErrorDetail
from athena.runtime import runtime_from

router = APIRouter(prefix="/sessions", tags=["events"])


def _sse(data: dict, event: str | None = None, event_id: int | None = None) -> str:
    """编码单条 SSE 帧；event_id 对外语义为 session_seq。"""
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event:
        lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False))
    return "\n".join(lines) + "\n\n"


def _row_payload(row) -> dict:
    """将数据库事件转换为稳定的 v2 Envelope。"""
    session_seq = row.session_seq or row.event_id
    return {
        "schema_version": 2,
        "session_seq": session_seq,
        "event_id": session_seq,
        "event_type": row.event_type,
        "durability": row.durability,
        "session_id": row.session_id,
        "run_id": row.run_id,
        "stream_id": row.stream_id,
        "stream_type": row.stream_type,
        "chunk_id": row.chunk_id,
        "is_complete": bool(row.is_complete),
        "parent_run_id": row.parent_run_id,
        "producer_id": row.producer_id,
        "sequence": row.sequence,
        "payload": json.loads(row.payload_json),
        "occurred_at": row.occurred_at,
    }


def _snapshot_payload(snapshot) -> dict:
    """将最新流快照编码为不占用 SSE 游标的恢复消息。"""
    return {
        "schema_version": 2,
        "event_type": "stream.snapshot",
        "session_id": snapshot.session_id,
        "run_id": snapshot.run_id,
        "stream_id": snapshot.stream_id,
        "stream_type": snapshot.stream_type,
        "version": snapshot.version,
        "last_chunk_id": snapshot.last_chunk_id,
        "content": snapshot.content,
        "content_length": snapshot.content_length,
        "status": snapshot.status,
        "updated_at": snapshot.updated_at,
    }


@router.get("/{session_id}/events")
async def session_events(
    session_id: str, request: Request, after: int = 0
) -> StreamingResponse:
    """先重放 watermark 之前的事件，再消费同一总线的实时通知。"""
    runtime = runtime_from(request)
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
            for row in await agent_store.events_between(session_id, after, watermark):
                seq = row.session_seq or row.event_id
                if seq <= cursor:
                    continue
                cursor = seq
                yield _sse(_row_payload(row), row.event_type, seq)

            # 快照用于修复客户端本地缺口；它不推进 Last-Event-ID。
            for snapshot in await agent_store.snapshots_for_session(session_id):
                yield _sse(_snapshot_payload(snapshot), "stream.snapshot")

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                seq = event.session_seq or event.event_id
                if seq is None or seq <= cursor:
                    continue
                cursor = seq
                yield _sse(event.model_dump(mode="json"), event.event_type, seq)
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
