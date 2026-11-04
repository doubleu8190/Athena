"""聊天用户可见 SSE 的注册、快照、恢复和清理测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from application.events import AssistantPrefix, EventStreamService
from application.events.stream_service import (
    _snapshot_payload,
    decode_sync_token,
    encode_sync_token,
)
from application.observability import ProgressEntry, ProgressSource
from application.sessions.query_service import _task_response
from application.sessions.history_port import (
    ActiveSessionSnapshot,
    HistoryRunRecord,
    HistoryTaskRecord,
    SessionTurnSnapshot,
)
from bootstrap import create_app
from domain.events import ApplicationEvent, EventDurability, EventType
from domain.sessions import Message, MessageRole, Session
from interfaces.http.sse.events import _durable_event, build_events_router


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Sessions:
    """只提供现有会话，验证不存在的订阅在读取快照前返回 404。"""

    async def get(self, session_id: str):
        return (
            Session.create(session_id=session_id, title="Session", now=NOW)
            if session_id == "session-1"
            else None
        )


class Events:
    """模拟先注册订阅、再从持久事件和实时队列读取的端口。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[ApplicationEvent] = asyncio.Queue()
        self.replayed = [
            ApplicationEvent(
                event_type=EventType.RUN_STARTED,
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=5,
                run_id="run-1",
            ),
            ApplicationEvent(
                event_type=EventType.NODE_STARTED,
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=6,
                run_id="run-1",
                payload={"node": "decide_root_action"},
            ),
            ApplicationEvent(
                event_type=EventType.MESSAGE_COMPLETED,
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=7,
                run_id="run-1",
                payload={"content": "重复完成"},
            ),
            ApplicationEvent(
                event_type=EventType.RUN_CANCELLED,
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=8,
                run_id="run-1",
            ),
        ]
        self.closed: list[tuple[str, asyncio.Queue[ApplicationEvent]]] = []

    async def open_subscription(self, session_id):
        return self.queue, 4

    async def list_events_between(self, session_id, after=0, upto=None, limit=256):
        return [
            event for event in self.replayed if event.session_seq > after
        ][:limit]

    async def close_subscription(self, session_id, queue):
        self.closed.append((session_id, queue))


class History:
    """快照水位为 4，使持久事件 5–7 均由恢复路径发送。"""

    async def read_stream_snapshot(
        self,
        session_id,
        *,
        after_event_sequence,
        after_progress_ingest_sequence,
        after_run_sequence,
        active_run_id,
        limit,
        include_latest_if_empty,
    ):
        return ActiveSessionSnapshot((), 4, 0, 1)


class Observability:
    """返回一条带展示元数据的已落库 Progress，并验证不发送 Trace。"""

    async def list_progress_after(self, session_id, after):
        if after >= 1:
            return []
        return [
            ProgressEntry(
                progress_id="progress-1",
                session_id=session_id,
                run_id="run-1",
                execution_id="execution-1",
                task_id="task-1",
                worker_id="worker-1",
                source=ProgressSource.WORKER,
                sequence=1,
                content="正在整理资料",
                created_at=NOW,
                ingest_sequence=1,
                worker_name="Worker A",
                task_title="整理资料",
            )
        ]


class EmptyObservability:
    async def list_progress_after(self, session_id, after):
        return []


def _event_id(chunks: list[str], event_type: str) -> str:
    for chunk in chunks:
        if f"event: {event_type}\n" in chunk:
            return next(
                line.removeprefix("id: ")
                for line in chunk.splitlines()
                if line.startswith("id: ")
            )
    raise AssertionError(f"event not found: {event_type}")


def test_stream_emits_snapshot_public_progress_and_replay_without_trace() -> None:
    events = Events()
    events.queue.put_nowait(
        ApplicationEvent(
            event_type=EventType.ASSISTANT_DELTA,
            durability=EventDurability.REALTIME,
            session_id="session-1",
            run_id="run-1",
            message_id="assistant:run-1",
            stream_id="answer-run-1",
            chunk_id=1,
            payload={"delta": "答案"},
        )
    )
    service = EventStreamService(
        Sessions(),
        events,
        history_reader=History(),
        observability=Observability(),
    )
    app = FastAPI()
    router = build_events_router(service)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/sessions/{session_id}/stream"
    )
    app.include_router(router)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/stream",
            "headers": [],
            "app": app,
        }
    )
    disconnect_values = iter([False, True])

    async def is_disconnected() -> bool:
        return next(disconnect_values)

    request.is_disconnected = is_disconnected  # type: ignore[method-assign]
    cursor = encode_sync_token(
        session_id="session-1",
        event_sequence=4,
        progress_ingest_sequence=0,
        run_sequence=1,
    )

    async def run() -> list[str]:
        response = await endpoint("session-1", request, 0, cursor)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(run())
    assert "event: session.snapshot" in chunks[0]
    assert "event: progress.created" in chunks[1]
    assert '"worker_name":"Worker A"' in chunks[1]
    assert '"task_title":"整理资料"' in chunks[1]
    assert "event: turn.started" in chunks[2]
    assert "event: turn.cancelled" in chunks[3]
    assert "node.started" not in "".join(chunks)
    assert "event: assistant.completed" not in "".join(chunks)
    assert "event: assistant.delta" in chunks[4]
    assert events.closed == [("session-1", events.queue)]


def test_progress_and_assistant_event_tokens_keep_active_run_for_resume() -> None:
    """活动 Run 未终态时，任一已发送事件的令牌都必须锚定该 Run。"""
    active_turn = SessionTurnSnapshot(
        HistoryRunRecord("run-1", "message-1", 1, "running"),
        (),
        (),
        (),
    )

    class InitialHistory(History):
        async def read_stream_snapshot(
            self,
            session_id,
            *,
            after_event_sequence,
            after_progress_ingest_sequence,
            after_run_sequence,
            active_run_id,
            limit,
            include_latest_if_empty,
        ):
            return ActiveSessionSnapshot((active_turn,), 4, 0, 1)

    class ResumeHistory(History):
        def __init__(self) -> None:
            self.active_run_ids: list[str | None] = []

        async def read_stream_snapshot(
            self,
            session_id,
            *,
            after_event_sequence,
            after_progress_ingest_sequence,
            after_run_sequence,
            active_run_id,
            limit,
            include_latest_if_empty,
        ):
            self.active_run_ids.append(active_run_id)
            turns = (active_turn,) if active_run_id == "run-1" else ()
            return ActiveSessionSnapshot(turns, 4, 1, 1)

    events = Events()
    events.replayed = []
    events.queue.put_nowait(
        ApplicationEvent(
            event_type=EventType.ASSISTANT_DELTA,
            durability=EventDurability.REALTIME,
            session_id="session-1",
            run_id="run-1",
            message_id="assistant:run-1",
            stream_id="answer-run-1",
            chunk_id=1,
            payload={"delta": "答案"},
        )
    )
    service = EventStreamService(
        Sessions(),
        events,
        history_reader=InitialHistory(),
        observability=Observability(),
    )
    app = FastAPI()
    router = build_events_router(service)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/sessions/{session_id}/stream"
    )
    app.include_router(router)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/stream",
            "headers": [],
            "app": app,
        }
    )
    disconnect_values = iter([False, True])

    async def is_disconnected() -> bool:
        return next(disconnect_values)

    request.is_disconnected = is_disconnected  # type: ignore[method-assign]
    cursor = encode_sync_token(
        session_id="session-1",
        event_sequence=4,
        progress_ingest_sequence=0,
        run_sequence=1,
    )

    async def run_initial() -> list[str]:
        response = await endpoint("session-1", request, 0, cursor)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(run_initial())
    progress_token = _event_id(chunks, "progress.created")
    assistant_token = _event_id(chunks, "assistant.delta")

    for token in (progress_token, assistant_token):
        history = ResumeHistory()
        resume_events = Events()
        resume_events.replayed = []
        resume_service = EventStreamService(
            Sessions(),
            resume_events,
            history_reader=history,
            observability=EmptyObservability(),
        )
        resume_app = FastAPI()
        resume_router = build_events_router(resume_service)
        resume_endpoint = next(
            route.endpoint
            for route in resume_router.routes
            if getattr(route, "path", None) == "/sessions/{session_id}/stream"
        )
        resume_app.include_router(resume_router)
        resume_request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/sessions/session-1/stream",
                "headers": [],
                "app": resume_app,
            }
        )

        async def resume_disconnected() -> bool:
            return True

        resume_request.is_disconnected = resume_disconnected  # type: ignore[method-assign]

        async def run_resume() -> list[str]:
            response = await resume_endpoint("session-1", resume_request, 0, token)
            return [chunk async for chunk in response.body_iterator]

        resumed = asyncio.run(run_resume())

        assert history.active_run_ids == ["run-1"]
        assert "event: session.snapshot" in resumed[0]
        assert '"run_id":"run-1"' in resumed[0]
        assert '"run_status":"running"' in resumed[0]


def test_durable_assistant_completed_replays_when_realtime_notification_is_lost() -> None:
    """实时队列没有完成通知时，持久回放仍必须发送 assistant.completed。"""
    events = Events()
    events.queue = asyncio.Queue()
    events.replayed = [
        ApplicationEvent(
            event_type=EventType.ASSISTANT_COMPLETED,
            durability=EventDurability.DURABLE,
            session_id="session-1",
            session_seq=7,
            run_id="run-1",
            message_id="assistant:run-1",
            payload={
                "content": "完整回答",
                "started_at": NOW.isoformat(),
                "kind": "final",
            },
        )
    ]
    service = EventStreamService(
        Sessions(),
        events,
        history_reader=History(),
        observability=EmptyObservability(),
    )
    app = FastAPI()
    router = build_events_router(service)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/sessions/{session_id}/stream"
    )
    app.include_router(router)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/stream",
            "headers": [],
            "app": app,
        }
    )

    async def is_disconnected() -> bool:
        return True

    request.is_disconnected = is_disconnected  # type: ignore[method-assign]
    cursor = encode_sync_token(
        session_id="session-1",
        event_sequence=4,
        progress_ingest_sequence=0,
        run_sequence=1,
    )

    async def run() -> list[str]:
        response = await endpoint("session-1", request, 0, cursor)
        return [chunk async for chunk in response.body_iterator]

    body = "".join(asyncio.run(run()))

    assert "event: assistant.completed" in body
    assert '"content":"完整回答"' in body
    assert '"started_at":"2026-01-01T00:00:00+00:00"' in body
    assert '"kind":"final"' in body


def test_terminal_failed_snapshot_does_not_expose_partial_assistant_prefix() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = _snapshot_payload(
        ActiveSessionSnapshot(
            (
                SessionTurnSnapshot(
                    HistoryRunRecord("run-1", "message-1", 1, "failed"),
                    (),
                    (),
                    (),
                ),
            ),
            0,
            0,
            1,
        ),
        {"run-1": AssistantPrefix(
            content="尚未完成的半截回答",
            started_at=now,
            completed=False,
            last_chunk_id=1,
        )},
    )

    assert payload["turns"][0]["assistant"] is None


def test_active_snapshot_keeps_unfinished_worker_result_null() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = _snapshot_payload(
        ActiveSessionSnapshot(
            (
                SessionTurnSnapshot(
                    HistoryRunRecord("run-1", "message-1", 1, "running"),
                    (),
                    (),
                    (
                        HistoryTaskRecord(
                            run_id="run-1",
                            task_id="task-1",
                            execution_id="execution-1",
                            worker_name="Worker A",
                            task_title="整理资料",
                            status="running",
                            created_at=now,
                            completed_at=None,
                            display_result=None,
                            display_result_at=None,
                        ),
                    ),
                ),
            ),
            0,
            0,
            1,
        ),
        {},
    )

    assert payload["turns"][0]["tasks"][0]["result"] is None


def test_task_result_keeps_execution_identity_for_retry_grouping() -> None:
    event = EventStreamService.response(
        ApplicationEvent(
            event_type=EventType.TASK_COMPLETED,
            durability=EventDurability.DURABLE,
            session_id="session-1",
            session_seq=9,
            run_id="run-1",
            payload={
                "task_id": "task-1",
                "execution_id": "execution-1",
                "worker_name": "Worker A",
                "title": "整理资料",
                "display_result": "任务完成",
            },
        )
    )

    public = _durable_event(event, sync_token="token")

    assert public is not None
    assert public["payload"]["task_id"] == "task-1"
    assert public["payload"]["execution_id"] == "execution-1"
    assert public["payload"]["status"] == "completed"


def test_task_result_never_projects_machine_output() -> None:
    event = EventStreamService.response(
        ApplicationEvent(
            event_type=EventType.TASK_COMPLETED,
            durability=EventDurability.DURABLE,
            session_id="session-1",
            session_seq=9,
            run_id="run-1",
            payload={
                "task_id": "task-1",
                "execution_id": "execution-1",
                "worker_name": "Worker A",
                "title": "整理资料",
                "result_summary": "机器结果不得展示",
                "error": {"message": "机器错误不得展示"},
            },
        )
    )

    public = _durable_event(event, sync_token="token")
    history = _task_response(
        HistoryTaskRecord(
            run_id="run-1",
            task_id="task-1",
            execution_id="execution-1",
            worker_name="Worker A",
            task_title="整理资料",
            status="completed",
            created_at=NOW,
            completed_at=NOW,
            display_result=None,
            display_result_at=None,
        ),
        [],
    )

    assert public is not None
    assert public["payload"]["result"]["content"] == "任务已完成。"
    assert (
        public["payload"]["result"]["content"]
        == history.result.content
    )
    assert "机器结果" not in str(public)
    assert "机器错误" not in str(public)


def test_completed_snapshot_discards_realtime_prefix_but_replays_unseen_completion() -> None:
    """快照已含正文时，仍需回放水位之后尚未消费的完成事件。"""
    events = Events()
    events.replayed = [
        ApplicationEvent(
            event_type=EventType.ASSISTANT_COMPLETED,
            durability=EventDurability.DURABLE,
            session_id="session-1",
            session_seq=7,
            run_id="run-1",
            message_id="assistant:run-1",
            payload={
                "content": "重复完成",
                "started_at": NOW.isoformat(),
                "kind": "final",
            },
        )
    ]
    events.queue.put_nowait(
        ApplicationEvent(
            event_type=EventType.ASSISTANT_DELTA,
            durability=EventDurability.REALTIME,
            session_id="session-1",
            run_id="run-1",
            message_id="assistant:run-1",
            stream_id="answer-run-1",
            chunk_id=1,
            payload={"delta": "重复正文"},
        )
    )

    class CompletedHistory:
        async def read_stream_snapshot(
            self,
            session_id,
            *,
            after_event_sequence,
            after_progress_ingest_sequence,
            after_run_sequence,
            active_run_id,
            limit,
            include_latest_if_empty,
        ):
            return ActiveSessionSnapshot(
                (
                    SessionTurnSnapshot(
                        HistoryRunRecord("run-1", "message-1", 1, "completed"),
                        (
                            Message(
                                id="message-1",
                                session_id=session_id,
                                role=MessageRole.USER,
                                content="问题",
                                timestamp=NOW,
                                run_id="run-1",
                            ),
                            Message(
                                id="assistant:run-1",
                                session_id=session_id,
                                role=MessageRole.ASSISTANT,
                                content="完整回答",
                                timestamp=NOW,
                                run_id="run-1",
                            ),
                        ),
                        (),
                        (),
                    ),
                ),
                4,
                0,
                1,
            )

    service = EventStreamService(
        Sessions(),
        events,
        history_reader=CompletedHistory(),
        observability=Observability(),
    )
    app = FastAPI()
    router = build_events_router(service)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/sessions/{session_id}/stream"
    )
    app.include_router(router)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/stream",
            "headers": [],
            "app": app,
        }
    )
    disconnect_values = iter([False, True])

    async def is_disconnected() -> bool:
        return next(disconnect_values)

    request.is_disconnected = is_disconnected  # type: ignore[method-assign]
    cursor = encode_sync_token(
        session_id="session-1",
        event_sequence=4,
        progress_ingest_sequence=0,
        run_sequence=1,
    )

    async def run() -> str:
        response = await endpoint("session-1", request, 0, cursor)
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(run())

    assert '"content":"完整回答"' in body
    assert "重复正文" not in body
    assert "event: assistant.completed" in body
    assert '"content":"重复完成"' in body
    assert '"kind":"final"' in body
    assert '"run_status":"completed"' in body
    assert "event: assistant.delta" not in body


def test_snapshot_watermark_covering_completion_relies_on_snapshot_state() -> None:
    """快照水位已覆盖完成事件时，由快照的 run_status/assistant 表达完成事实。"""
    events = Events()
    events.replayed = [
        ApplicationEvent(
            event_type=EventType.ASSISTANT_COMPLETED,
            durability=EventDurability.DURABLE,
            session_id="session-1",
            session_seq=7,
            run_id="run-1",
            message_id="assistant:run-1",
            payload={
                "content": "完整回答",
                "started_at": NOW.isoformat(),
                "kind": "final",
            },
        )
    ]

    class CompletedHistory:
        async def read_stream_snapshot(
            self,
            session_id,
            *,
            after_event_sequence,
            after_progress_ingest_sequence,
            after_run_sequence,
            active_run_id,
            limit,
            include_latest_if_empty,
        ):
            return ActiveSessionSnapshot(
                (
                    SessionTurnSnapshot(
                        HistoryRunRecord("run-1", "message-1", 1, "completed"),
                        (
                            Message(
                                id="message-1",
                                session_id=session_id,
                                role=MessageRole.USER,
                                content="问题",
                                timestamp=NOW,
                                run_id="run-1",
                            ),
                            Message(
                                id="assistant:run-1",
                                session_id=session_id,
                                role=MessageRole.ASSISTANT,
                                content="完整回答",
                                timestamp=NOW,
                                run_id="run-1",
                            ),
                        ),
                        (),
                        (),
                    ),
                ),
                7,
                0,
                1,
            )

    service = EventStreamService(
        Sessions(),
        events,
        history_reader=CompletedHistory(),
        observability=EmptyObservability(),
    )
    app = FastAPI()
    router = build_events_router(service)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/sessions/{session_id}/stream"
    )
    app.include_router(router)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/stream",
            "headers": [],
            "app": app,
        }
    )

    async def is_disconnected() -> bool:
        return True

    request.is_disconnected = is_disconnected  # type: ignore[method-assign]
    cursor = encode_sync_token(
        session_id="session-1",
        event_sequence=4,
        progress_ingest_sequence=0,
        run_sequence=1,
    )

    async def run() -> str:
        response = await endpoint("session-1", request, 0, cursor)
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(run())

    assert '"run_status":"completed"' in body
    assert '"completed":true' in body
    assert '"content":"完整回答"' in body
    assert "event: assistant.completed" not in body


def test_session_snapshot_carries_every_recovered_terminal_turn() -> None:
    def turn(run_id: str, sequence: int, content: str) -> SessionTurnSnapshot:
        return SessionTurnSnapshot(
            HistoryRunRecord(run_id, f"message-{sequence}", sequence, "completed"),
            (
                Message(
                    id=f"message-{sequence}",
                    session_id="session-1",
                    role=MessageRole.USER,
                    content=content,
                    timestamp=NOW.replace(second=sequence),
                    run_id=run_id,
                ),
                Message(
                    id=f"assistant:{run_id}",
                    session_id="session-1",
                    role=MessageRole.ASSISTANT,
                    content=f"{content}的答复",
                    timestamp=NOW.replace(second=sequence, microsecond=1),
                    run_id=run_id,
                ),
            ),
            (),
            (),
        )

    payload = _snapshot_payload(
        ActiveSessionSnapshot(
            (turn("run-1", 1, "第一问"), turn("run-2", 2, "第二问")),
            8,
            2,
            2,
        ),
        {},
    )

    assert [item["run_id"] for item in payload["turns"]] == ["run-1", "run-2"]
    assert payload["turns"][0]["assistant"]["content"] == "第一问的答复"
    assert payload["turns"][1]["assistant"]["content"] == "第二问的答复"


def test_sync_token_carries_boundary_active_run_without_exposing_structure() -> None:
    token = encode_sync_token(
        session_id="session-1",
        event_sequence=8,
        progress_ingest_sequence=3,
        run_sequence=2,
        active_run_id="run-active",
    )

    assert decode_sync_token(token, session_id="session-1") == (
        8,
        3,
        2,
        "run-active",
    )


@pytest.mark.asyncio
async def test_snapshot_token_carries_deterministic_active_run_boundary() -> None:
    def active_turn(sequence: int, status: str) -> SessionTurnSnapshot:
        run_id = f"run-{sequence}"
        return SessionTurnSnapshot(
            HistoryRunRecord(
                run_id,
                f"message-{sequence}",
                sequence,
                status,
            ),
            (),
            (),
            (),
        )

    class ActiveHistory:
        async def read_stream_snapshot(self, *args, **kwargs):
            return ActiveSessionSnapshot(
                (
                    active_turn(1, "queued"),
                    active_turn(2, "stopping"),
                ),
                8,
                3,
                2,
            )

    service = EventStreamService(
        Sessions(),
        Events(),
        history_reader=ActiveHistory(),
        observability=Observability(),
    )

    _, _, _, token = await service.snapshot(
        "session-1",
        include_latest_if_empty=True,
    )

    assert decode_sync_token(token, session_id="session-1") == (
        8,
        3,
        2,
        "run-2",
    )


def test_last_event_id_takes_priority_over_a_stale_query_token() -> None:
    class HeaderHistory(History):
        def __init__(self) -> None:
            self.after_event_sequence: int | None = None

        async def read_stream_snapshot(
            self,
            session_id,
            *,
            after_event_sequence,
            after_progress_ingest_sequence,
            after_run_sequence,
            active_run_id,
            limit,
            include_latest_if_empty,
        ):
            self.after_event_sequence = after_event_sequence
            return ActiveSessionSnapshot((), 3, 0, 1)

    events = Events()
    history = HeaderHistory()
    service = EventStreamService(
        Sessions(),
        events,
        history_reader=history,
        observability=Observability(),
    )
    app = FastAPI()
    router = build_events_router(service)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/sessions/{session_id}/stream"
    )
    app.include_router(router)
    header_token = encode_sync_token(
        session_id="session-1",
        event_sequence=3,
        progress_ingest_sequence=0,
        run_sequence=1,
    )
    stale_query_token = encode_sync_token(
        session_id="session-1",
        event_sequence=1,
        progress_ingest_sequence=0,
        run_sequence=0,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/stream",
            "headers": [(b"last-event-id", header_token.encode("ascii"))],
            "app": app,
        }
    )
    disconnect_values = iter([False, True])

    async def is_disconnected() -> bool:
        return next(disconnect_values)

    request.is_disconnected = is_disconnected  # type: ignore[method-assign]

    async def run() -> list[str]:
        response = await endpoint("session-1", request, 0, stale_query_token)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(run())

    assert history.after_event_sequence == 3
    assert "node.started" not in "".join(chunks)
    assert events.closed == [("session-1", events.queue)]


def test_offline_recovery_emits_bounded_snapshot_pages() -> None:
    def recovered_turn(sequence: int) -> SessionTurnSnapshot:
        run_id = f"run-{sequence}"
        return SessionTurnSnapshot(
            HistoryRunRecord(
                run_id,
                f"message-{sequence}",
                sequence,
                "completed",
            ),
            (
                Message(
                    id=f"message-{sequence}",
                    session_id="session-1",
                    role=MessageRole.USER,
                    content=f"第{sequence}轮",
                    timestamp=NOW.replace(second=sequence),
                    run_id=run_id,
                ),
            ),
            (),
            (),
        )

    class PagedHistory:
        async def read_stream_snapshot(
            self,
            session_id,
            *,
            after_event_sequence,
            after_progress_ingest_sequence,
            after_run_sequence,
            active_run_id,
            limit,
            include_latest_if_empty,
        ):
            after = after_run_sequence or 0
            turns = tuple(
                recovered_turn(sequence)
                for sequence in range(after + 1, min(after + 3, 7))
            )
            return ActiveSessionSnapshot(
                turns,
                0,
                0,
                turns[-1].run.sequence if turns else after,
                has_more=bool(turns and turns[-1].run.sequence < 6),
            )

    events = Events()
    events.replayed = []
    service = EventStreamService(
        Sessions(),
        events,
        history_reader=PagedHistory(),
        observability=Observability(),
    )
    app = FastAPI()
    router = build_events_router(service)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/sessions/{session_id}/stream"
    )
    app.include_router(router)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/stream",
            "headers": [],
            "app": app,
        }
    )
    disconnect_values = iter([False, True])

    async def is_disconnected() -> bool:
        return next(disconnect_values)

    request.is_disconnected = is_disconnected  # type: ignore[method-assign]
    cursor = encode_sync_token(
        session_id="session-1",
        event_sequence=0,
        progress_ingest_sequence=0,
        run_sequence=0,
    )

    async def run() -> list[str]:
        response = await endpoint("session-1", request, 0, cursor)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(run())
    body = "".join(chunks)

    assert body.count("event: session.snapshot") == 3
    for sequence in range(1, 7):
        assert f"第{sequence}轮" in body
    assert events.closed == [("session-1", events.queue)]


def test_snapshot_database_failure_closes_registered_subscription() -> None:
    class FailingHistory:
        async def read_stream_snapshot(self, *args, **kwargs):
            raise RuntimeError("snapshot unavailable")

    events = Events()
    service = EventStreamService(
        Sessions(),
        events,
        history_reader=FailingHistory(),
        observability=Observability(),
    )
    app = FastAPI()
    router = build_events_router(service)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/sessions/{session_id}/stream"
    )
    app.include_router(router)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sessions/session-1/stream",
            "headers": [],
            "app": app,
        }
    )

    async def run() -> None:
        await endpoint("session-1", request, 0, None)

    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        asyncio.run(run())
    assert events.closed == [("session-1", events.queue)]


def test_stream_rejects_unknown_session_and_invalid_queries() -> None:
    client = TestClient(
        create_app(event_stream_service=EventStreamService(Sessions(), Events()))
    )
    assert client.get("/api/sessions/missing/stream").status_code == 404
    assert client.get("/api/sessions/session-1/stream?after=-1").status_code == 422
    assert client.get("/api/sessions/session-1/stream?sync_token=bad").status_code == 400
