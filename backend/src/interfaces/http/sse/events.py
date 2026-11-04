"""只承载用户可见聊天内容的会话 SSE。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from application.events import EventSessionNotFoundError, EventStreamService
from application.events.responses import EventResponse
from application.events.stream_service import decode_sync_token, encode_sync_token
from application.observability import ProgressEntry

_PUBLIC_DURABLE_EVENTS = frozenset(
    {
        "run.started",
        "run.resumed",
        "run.failed",
        "run.cancelled",
        "task.completed",
        "task.failed",
        "task.cancelled",
        "assistant.completed",
    }
)
_REALTIME_ASSISTANT_EVENTS = frozenset(
    {
        "assistant.started",
        "assistant.delta",
    }
)


def _public_envelope(
    *,
    event_type: str,
    session_id: str,
    sync_token: str,
    occurred_at: datetime,
    payload: dict[str, Any],
    run_id: str | None = None,
) -> dict[str, Any]:
    """构造不含内部 Trace 身份的聊天 SSE 信封。"""
    return {
        "schema_version": 3,
        "event_type": event_type,
        "session_id": session_id,
        "run_id": run_id,
        "sync_token": sync_token,
        "occurred_at": occurred_at.isoformat(),
        "payload": payload,
    }


def _format_event(value: dict[str, Any]) -> str:
    """格式化具名 SSE 事件。"""
    event_type = str(value["event_type"])
    return (
        f"id: {value['sync_token']}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _progress_event(entry: ProgressEntry, sync_token: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "progress_id": entry.progress_id,
        "execution_id": entry.execution_id,
        "task_id": entry.task_id,
        "worker_id": entry.worker_id,
        "source": entry.source.value,
        "sequence": entry.sequence,
        "content": entry.content,
        "created_at": entry.created_at.isoformat(),
    }
    if entry.worker_name is not None:
        payload["worker_name"] = entry.worker_name
    if entry.task_title is not None:
        payload["task_title"] = entry.task_title
    return _public_envelope(
        event_type="progress.created",
        session_id=entry.session_id,
        sync_token=sync_token,
        occurred_at=entry.created_at,
        payload=payload,
        run_id=entry.run_id,
    )


def _durable_event(
    event: EventResponse,
    *,
    sync_token: str,
) -> dict[str, Any] | None:
    """把内部事件投影成固定聊天事件集合。"""
    event_type = event.event_type
    payload = dict(event.payload)
    public_payload: dict[str, Any]
    if event_type in {"run.started", "run.resumed"}:
        public_type = "turn.started"
        public_payload = {}
        run_id = event.run_id
    elif event_type == "run.failed":
        public_type = "turn.failed"
        public_payload = {
            "error": {
                key: value
                for key, value in dict(payload.get("error") or {}).items()
                if key in {"code", "message", "retryable"}
            }
        }
        run_id = event.run_id
    elif event_type == "run.cancelled":
        public_type = "turn.cancelled"
        public_payload = {}
        run_id = event.run_id
    elif event_type in {"task.completed", "task.failed", "task.cancelled"}:
        public_type = "task.result"
        result_time = event.occurred_at or datetime.now(timezone.utc)
        # 机器输出可能包含 Worker 私有业务内容，只能由显式的用户可读结果进入聊天投影。
        result = payload.get("display_result")
        if not isinstance(result, str) or not result.strip():
            result = (
                "任务已完成。"
                if event_type == "task.completed"
                else "任务已取消。"
                if event_type == "task.cancelled"
                else "任务执行失败。"
            )
        public_payload = {
            "task_id": payload.get("task_id"),
            "execution_id": payload.get("execution_id"),
            "worker_name": payload.get("worker_name") or "Worker",
            "task_title": payload.get("title") or payload.get("objective") or "Worker 任务",
            "status": {
                "task.completed": "completed",
                "task.failed": "failed",
                "task.cancelled": "cancelled",
            }[event_type],
            "result": {"time": result_time.isoformat(), "content": result},
        }
        run_id = event.run_id
    elif event_type == "assistant.completed":
        public_type = "assistant.completed"
        public_payload = {
            "content": payload.get("content"),
            "started_at": payload.get("started_at"),
            "kind": payload.get("kind", "final"),
        }
        run_id = event.run_id
    else:
        return None
    return _public_envelope(
        event_type=public_type,
        session_id=event.session_id,
        sync_token=sync_token,
        occurred_at=event.occurred_at or datetime.now(timezone.utc),
        payload=public_payload,
        run_id=run_id,
    )


def build_events_router(stream_service: EventStreamService) -> APIRouter:
    """构建 `/stream`，并保留 `/events` 作为同一用户可见协议别名。"""
    router = APIRouter(prefix="/sessions", tags=["chat-stream"])

    async def session_stream(
        session_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        sync_token: str | None = Query(default=None),
    ) -> StreamingResponse:
        """订阅用户可见事件；数据库先注册订阅，再生成快照与逐页投影。"""
        header_token = request.headers.get("last-event-id")
        try:
            (
                event_cursor,
                progress_cursor,
                run_cursor,
                active_run_id,
            ) = decode_sync_token(
                header_token or sync_token,
                session_id=session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_sync_token") from exc
        if not header_token and not sync_token:
            event_cursor = max(event_cursor, after)
        has_sync_token = bool(sync_token or header_token)
        if not has_sync_token:
            run_cursor = None
        include_latest_if_empty = run_cursor is None
        queue: asyncio.Queue[ApplicationEvent] | None = None
        subscription_closed = False
        completed_assistant_runs: set[str] = set()
        close_lock = asyncio.Lock()

        async def close_subscription_once() -> None:
            """确保预建连失败、流结束和后台清理合计只关闭一次。"""
            nonlocal subscription_closed
            async with close_lock:
                if subscription_closed or queue is None:
                    return
                subscription_closed = True
                await stream_service.close(session_id, queue)

        try:
            # 必须先注册队列；随后生成的 snapshot 水位覆盖此前所有事实。
            queue, _ = await stream_service.open_subscription(session_id)
            (
                first_snapshot_payload,
                first_snapshot,
                _,
                first_snapshot_token,
            ) = await stream_service.snapshot(
                session_id,
                after_event_sequence=event_cursor,
                after_progress_ingest_sequence=progress_cursor,
                after_run_sequence=run_cursor,
                active_run_id=active_run_id,
                include_latest_if_empty=include_latest_if_empty,
            )
        except EventSessionNotFoundError as exc:
            await close_subscription_once()
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except Exception:
            await close_subscription_once()
            raise

        async def emit_durable() -> AsyncIterator[str]:
            nonlocal event_cursor
            while True:
                page = await stream_service.replay(
                    session_id, event_cursor, None, limit=256
                )
                if not page:
                    return
                for event in page:
                    if event.session_seq is None or event.session_seq <= event_cursor:
                        continue
                    next_event_cursor = event.session_seq
                    if event.event_type not in _PUBLIC_DURABLE_EVENTS:
                        event_cursor = next_event_cursor
                        continue
                    token = encode_sync_token(
                        session_id=session_id,
                        event_sequence=next_event_cursor,
                        progress_ingest_sequence=progress_cursor,
                        run_sequence=run_cursor,
                        active_run_id=active_run_id,
                    )
                    public = _durable_event(event, sync_token=token)
                    if public is not None:
                        yield _format_event(public)
                    event_cursor = next_event_cursor
                if len(page) < 256:
                    return

        async def emit_progress() -> AsyncIterator[str]:
            nonlocal progress_cursor
            while True:
                page = await stream_service.list_progress_after(
                    session_id, progress_cursor
                )
                if not page:
                    return
                for entry in page:
                    next_progress_cursor = max(
                        progress_cursor, entry.ingest_sequence
                    )
                    token = encode_sync_token(
                        session_id=session_id,
                        event_sequence=event_cursor,
                        progress_ingest_sequence=next_progress_cursor,
                        run_sequence=run_cursor,
                        active_run_id=active_run_id,
                    )
                    yield _format_event(_progress_event(entry, token))
                    progress_cursor = next_progress_cursor
                if len(page) < 256:
                    return

        async def stream() -> AsyncIterator[str]:
            nonlocal event_cursor, progress_cursor, run_cursor, active_run_id
            if queue is None:
                raise RuntimeError("chat_stream_subscription_missing")
            snapshot_payload = first_snapshot_payload
            snapshot_active = first_snapshot
            snapshot_token = first_snapshot_token
            snapshot_assistants: dict[str, object] = {}
            try:
                while True:
                    yield _format_event(
                        _public_envelope(
                            event_type="session.snapshot",
                            session_id=session_id,
                            sync_token=snapshot_token,
                            occurred_at=datetime.now(timezone.utc),
                            payload=snapshot_payload,
                            run_id=(
                                snapshot_payload.get("run_id")
                                if isinstance(snapshot_payload.get("run_id"), str)
                                else None
                            ),
                        )
                    )
                    (
                        page_event_cursor,
                        page_progress_cursor,
                        page_run_cursor,
                        page_active_run_id,
                    ) = decode_sync_token(
                        snapshot_token,
                        session_id=session_id,
                    )
                    event_cursor = page_event_cursor
                    progress_cursor = page_progress_cursor
                    run_cursor = page_run_cursor
                    active_run_id = page_active_run_id
                    turns = snapshot_payload.get("turns")
                    if isinstance(turns, list):
                        for turn in turns:
                            if (
                                not isinstance(turn, dict)
                                or not isinstance(turn.get("run_id"), str)
                                or not isinstance(turn.get("assistant"), dict)
                            ):
                                continue
                            run_id = str(turn["run_id"])
                            assistant = turn["assistant"]
                            snapshot_assistants[run_id] = assistant
                            if (
                                isinstance(assistant, dict)
                                and assistant.get("completed") is True
                            ):
                                completed_assistant_runs.add(run_id)
                    if snapshot_active is None or not snapshot_active.has_more:
                        break
                    (
                        snapshot_payload,
                        snapshot_active,
                        _,
                        snapshot_token,
                    ) = await stream_service.snapshot(
                        session_id,
                        after_event_sequence=event_cursor,
                        after_progress_ingest_sequence=progress_cursor,
                        after_run_sequence=run_cursor,
                        include_latest_if_empty=False,
                    )
                async for item in emit_progress():
                    yield item
                async for item in emit_durable():
                    yield item
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        async for item in emit_progress():
                            yield item
                        async for item in emit_durable():
                            yield item
                        yield ": heartbeat\n\n"
                        continue
                    if event.event_type in _REALTIME_ASSISTANT_EVENTS:
                        if event.run_id in completed_assistant_runs:
                            # assistant 已在快照中完整落库时，队列中同一 Run 的
                            # started、delta 和 completed 都只是旧前缀，必须整体丢弃。
                            continue
                        snapshot_assistant = snapshot_assistants.get(
                            event.run_id or ""
                        )
                        if isinstance(snapshot_assistant, dict):
                            if (
                                event.event_type == "assistant.completed"
                                and snapshot_assistant.get("completed") is True
                            ):
                                continue
                            if (
                                event.event_type in {
                                    "assistant.started",
                                    "assistant.delta",
                                }
                                and int(event.chunk_id or 0)
                                <= int(snapshot_assistant.get("last_chunk_id") or 0)
                            ):
                                continue
                        token = encode_sync_token(
                            session_id=session_id,
                            event_sequence=event_cursor,
                            progress_ingest_sequence=progress_cursor,
                            run_sequence=run_cursor,
                            active_run_id=active_run_id,
                        )
                        yield _format_event(
                            _public_envelope(
                                event_type=str(event.event_type),
                                session_id=session_id,
                                sync_token=token,
                                occurred_at=event.occurred_at,
                                payload=dict(event.payload),
                                run_id=event.run_id,
                            )
                        )
                    else:
                        async for item in emit_progress():
                            yield item
                        async for item in emit_durable():
                            yield item
            finally:
                await close_subscription_once()

        try:
            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                },
                background=BackgroundTask(close_subscription_once),
            )
        except Exception:
            await close_subscription_once()
            raise

    router.add_api_route(
        "/{session_id}/stream",
        session_stream,
        methods=["GET"],
        name="session_chat_stream",
    )
    router.add_api_route(
        "/{session_id}/events",
        session_stream,
        methods=["GET"],
        name="legacy_session_chat_stream",
        include_in_schema=False,
    )
    return router


__all__ = ["build_events_router"]
