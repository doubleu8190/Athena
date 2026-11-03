"""Session 消息和运行列表 Controller 契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from application.runs import RunQueryService
from application.sessions.history_port import HistoryRunRecord, SessionHistorySnapshot
from application.sessions.query_service import (
    _event_content,
    _project_events,
    _result_text,
)
from application.sessions import SessionMessageQueryService, SessionService
from bootstrap import create_app
from domain.events import ApplicationEvent, EventDurability
from domain.runs import RunStatus, RunSummary
from domain.sessions import (
    Message,
    MessageRole,
    Session,
)
from infrastructure.persistence.postgres.repositories.session_history_repository import (
    _continuous_visible_prefix_watermark,
    _history_snapshot_event_seq,
)


class SessionRepository:
    def __init__(self) -> None:
        self.items = {
            "session-1": Session.create(
                session_id="session-1",
                title="Session",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            "session-2": Session.create(
                session_id="session-2",
                title="Other Session",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        }

    async def create(self, value):
        self.items[value.id] = value
        return value

    async def get(self, session_id):
        return self.items.get(session_id)

    async def list_sessions(self):
        return list(self.items.values())

    async def save(self, value):
        self.items[value.id] = value
        return value

    async def delete(self, session_id):
        return self.items.pop(session_id, None) is not None


class MessageRepository:
    async def save(self, message):
        return message.id

    async def get(self, message_id):
        return None

    async def list_by_session(self, session_id, *, limit=None):
        values = [
            Message(
                id="message-1",
                session_id=session_id,
                role=MessageRole.USER,
                content="hello",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                run_id="run-1",
            ),
        ]
        return values[:limit] if limit is not None else values

    async def list_after(self, session_id, after_id):
        return []


class RunRepository:
    async def list_for_session(self, session_id):
        if session_id != "session-1":
            return []
        return [
            RunSummary(
                run_id="run-1",
                session_id=session_id,
                message_id="message-1",
                sequence=1,
                status=RunStatus.COMPLETED,
                root_thread_id="thread-1",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:01:00+00:00",
                started_at="2026-01-01T00:00:55+00:00",
                title="hello",
            )
        ]

    async def list_recent(self, *, limit, offset, session_id=None):
        values = [
            RunSummary("run-2", "session-2", "message-2", 2, RunStatus.CANCELLED, "thread-2", created_at="2026-01-02T00:00:00Z"),
            *(await self.list_for_session("session-1")),
        ]
        if session_id is not None:
            values = [value for value in values if value.session_id == session_id]
        return values[offset:offset + limit], len(values)


class EventHistory:
    def __init__(self):
        self.events = [
            ApplicationEvent(
                event_type="node.completed",
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=4,
                run_id="run-1",
                payload={
                    "activity_id": "node:run-1:prepare_request",
                    "name": "prepare_request",
                    "status": "completed",
                    "duration_ms": 12,
                    "message": "must not be exposed",
                    "arguments": {"command": "private command"},
                    "output": "private output",
                    "error": {"code": "node_failed", "message": "safe diagnostic", "stack": "private stack"},
                },
            ),
            ApplicationEvent(
                event_type="task.started",
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=5,
                run_id="run-1",
                payload={"task_id": "task-1", "execution_id": "exec-1", "title": "检索资料"},
            ),
            ApplicationEvent(
                event_type="task.completed",
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=6,
                run_id="run-1",
                payload={
                    "task_id": "task-1",
                    "execution_id": "exec-1",
                    "title": "检索资料",
                    "result_summary": "找到三条资料",
                },
            ),
            ApplicationEvent(
                event_type="task.started",
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=7,
                run_id="run-1",
                payload={"task_id": "task-1", "execution_id": "exec-2", "title": "重试检索", "attempt": 2},
            ),
            ApplicationEvent(
                event_type="task.retrying",
                durability=EventDurability.DURABLE,
                session_id="session-1",
                session_seq=8,
                run_id="run-1",
                payload={"task_id": "task-1", "execution_id": "exec-1", "title": "检索资料", "attempt": 2},
            ),
        ]

    async def list_events_for_run(self, session_id, run_id, *, after=0, limit=200):
        values = [event for event in self.events if event.session_id == session_id and event.run_id == run_id and event.session_seq > after]
        return values[:limit], len(values) > limit


def make_client(events=None) -> TestClient:
    sessions = SessionRepository()
    session_service = SessionService(sessions, id_factory=lambda: "unused")
    messages = MessageRepository()
    runs = RunRepository()
    events = events or EventHistory()
    class HistorySnapshot:
        async def read_snapshot(self, session_id, *, before_sequence, limit):
            summaries = await runs.list_for_session(session_id)
            summaries = [run for run in summaries if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}]
            summaries.sort(key=lambda run: run.sequence, reverse=True)
            if before_sequence is not None:
                summaries = [run for run in summaries if run.sequence < before_sequence]
            selected = summaries[:limit]
            messages = await MessageRepository().list_by_session(session_id)
            snapshot_events = tuple(event for event in events.events if event.session_id == session_id and any(event.run_id == run.run_id for run in selected))
            return SessionHistorySnapshot(
                runs=tuple(HistoryRunRecord(run.run_id, run.message_id, run.sequence) for run in selected),
                messages=tuple(messages),
                events=snapshot_events,
                has_more=len(summaries) > limit,
                snapshot_event_seq=max(
                    (event.session_seq or 0 for event in snapshot_events),
                    default=0,
                ),
            )
    return TestClient(
        create_app(
            session_service=session_service,
            message_query_service=SessionMessageQueryService(sessions, HistorySnapshot()),
            run_query_service=RunQueryService(sessions, runs, events),
        )
    )


def test_session_messages_and_runs_are_dto_mapped() -> None:
    event_history = EventHistory()
    client = make_client(event_history)

    messages = client.get("/api/sessions/session-1/messages")
    assert messages.status_code == 200
    assert messages.json()["items"][0]["user"]["content"] == "hello"
    assert messages.json()["items"][0]["user"]["id"] == "message-1"
    assert messages.json()["items"][0]["run_id"] == "run-1"
    assert messages.json()["items"][0]["assistant"] is None
    assert messages.json()["items"][0]["root_process"][0]["content"] == "处理完成：prepare_request"
    assert messages.json()["items"][0]["tasks"][0]["content"] == "找到三条资料"
    assert len(messages.json()["items"][0]["tasks"][0]["process"]) == 3
    assert len(messages.json()["items"][0]["tasks"]) == 2
    assert messages.json()["items"][0]["tasks"][0]["time"] == event_history.events[2].occurred_at.isoformat().replace("+00:00", "Z")
    assert messages.json()["items"][0]["tasks"][0]["session_seq"] == 6
    assert next(
        item["session_seq"]
        for item in messages.json()["items"][0]["tasks"][0]["process"]
        if item["content"] == "任务执行成功：检索资料"
    ) == 6
    assert messages.json()["snapshot_event_seq"] == 8

    runs = client.get("/api/sessions/session-1/runs")
    assert runs.status_code == 200
    assert runs.json()[0]["run_id"] == "run-1"
    assert runs.json()[0]["status"] == "completed"


def test_session_messages_and_runs_return_404_for_unknown_session() -> None:
    client = make_client()
    assert client.get("/api/sessions/missing/messages").status_code == 404
    assert client.get("/api/sessions/missing/runs").status_code == 404


def test_run_timeline_is_scoped_and_filters_sensitive_payload_fields() -> None:
    client = make_client(EventHistory())

    timeline = client.get("/api/sessions/session-1/runs/run-1/timeline")
    assert timeline.status_code == 200
    body = timeline.json()
    assert body["next_after_seq"] == 8
    assert body["events"][0]["payload"]["name"] == "prepare_request"
    assert body["events"][0]["payload"]["error"] == {"code": "node_failed", "message": "safe diagnostic"}
    assert "message" not in body["events"][0]["payload"]
    assert "arguments" not in body["events"][0]["payload"]
    assert "output" not in body["events"][0]["payload"]

    assert client.get("/api/sessions/session-2/runs/run-1/timeline").status_code == 404


def test_session_history_rejects_bad_cursor_and_returns_only_terminal_runs() -> None:
    client = make_client()
    assert client.get("/api/sessions/session-1/messages?cursor=0").status_code == 400
    assert client.get("/api/sessions/session-1/messages?limit=0").status_code == 422


def test_history_event_labels_preserve_terminal_status_and_retrieval_events() -> None:
    assert _event_content("node.started", {"name": "planning"}) == "开始处理：planning"
    assert _event_content("node.completed", {"name": "planning"}) == "处理完成：planning"
    assert _event_content("node.failed", {"name": "planning"}) == "处理失败：planning"
    assert _event_content("plan.cancelling", {}) == "正在停止执行计划"
    assert _event_content("retrieval.provider.failed", {"provider": "memory", "status": "timeout"}) == "memory检索失败"
    assert _event_content("retrieval.selection.completed", {"context_count": 3}) == "已选择 3 条资料用于回答"
    assert _event_content("tool.completed", {"tool_name": "search", "status": "failed"}) == "工具调用失败：search"
    assert _event_content("run.started", {}) == "任务开始执行"
    assert _event_content("run.resumed", {}) == "任务继续执行"
    assert _event_content("run.completed", {}) == "任务已完成"
    assert _event_content("llm.started", {}) == "正在调用模型"
    assert _event_content("llm.completed", {"status": "completed"}) == "模型调用完成"
    assert _event_content("llm.completed", {"status": "failed"}) == "模型调用失败"
    assert _event_content("llm.validation_failed", {}) == "模型输出校验失败"
    assert _event_content("agent.waiting_for_files", {}) == "等待文件处理完成后继续"
    assert _event_content("attachment_updated", {}) == "附件状态已更新"
    assert _event_content("file_processing_started", {}) == "正在处理文件"
    assert _event_content("file_processing_completed", {}) == "文件已处理完成"
    assert _event_content("file_processing_failed", {}) == "文件处理失败"


def test_worker_events_group_by_execution_and_result_uses_terminal_time() -> None:
    started = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    completed = datetime(2026, 1, 1, 0, 0, 4, tzinfo=timezone.utc)
    retried = datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
    events = [
        ApplicationEvent(
            event_type="task.started",
            durability=EventDurability.DURABLE,
            session_id="session-1",
            session_seq=1,
            run_id="run-1",
            occurred_at=started,
            payload={
                "task_id": "task-1",
                "execution_id": "exec-1",
                "execution_generation": 0,
                "title": "检索资料",
            },
        ),
        ApplicationEvent(
            event_type="llm.completed",
            durability=EventDurability.DURABLE,
            session_id="session-1",
            session_seq=2,
            run_id="run-1",
            occurred_at=completed,
            payload={
                "parent_activity_id": "task:task-1:exec:0",
                "status": "completed",
            },
        ),
        ApplicationEvent(
            event_type="task.completed",
            durability=EventDurability.DURABLE,
            session_id="session-1",
            session_seq=3,
            run_id="run-1",
            occurred_at=completed,
            payload={
                "task_id": "task-1",
                "execution_id": "exec-1",
                "execution_generation": 0,
                "title": "检索资料",
                "result_summary": "完成",
            },
        ),
        ApplicationEvent(
            event_type="task.retrying",
            durability=EventDurability.DURABLE,
            session_id="session-1",
            session_seq=4,
            run_id="run-1",
            occurred_at=retried,
            payload={
                "task_id": "task-1",
                "execution_id": "exec-1",
                "execution_generation": 0,
                "title": "检索资料",
            },
        ),
    ]

    root, tasks = _project_events(events)

    assert root == []
    assert len(tasks) == 1
    assert tasks[0].time == completed
    assert tasks[0].content == "完成"
    assert tasks[0].session_seq == 3
    assert [item.content for item in tasks[0].process] == [
        "任务已开始：检索资料",
        "模型调用完成",
        "任务执行成功：检索资料",
        "任务准备重试：检索资料",
    ]


def test_worker_result_text_only_uses_reviewed_summary_field() -> None:
    payload = {
        "result_summary": "可展示摘要",
        "result": {
            "output": "原始结果",
            "authorization": "secret-token",
        },
        "output": "原始输出",
        "content": "敏感正文",
        "result_ref": "private://result",
        "metadata": {"api_key": "secret-key"},
    }

    assert _result_text(payload) == "可展示摘要"
    assert _result_text({
        "result": {"summary": "未经审查的嵌套摘要"},
        "output": "原始输出",
        "content": "敏感正文",
        "result_ref": "private://result",
    }) is None


def test_history_snapshot_event_seq_never_crosses_active_or_unconsumed_events() -> None:
    # 可见事件连续返回时，候选 max 可以成为已消费前缀。
    assert _continuous_visible_prefix_watermark(4, None) == 4

    # 未返回的 visible 事件是缺口；它之前的隐藏 seq 可以直接跳过。
    assert _continuous_visible_prefix_watermark(4, 3) == 2
    assert _continuous_visible_prefix_watermark(5, 4) == 3
    assert _continuous_visible_prefix_watermark(0, None) == 0

    # active 事件早于较晚的终态 outbox 时，水位必须退回到 active 之前。
    assert _history_snapshot_event_seq(0, None) == 0
    assert _history_snapshot_event_seq(4, None) == 4
    assert _history_snapshot_event_seq(4, 3) == 2

    # 向前翻页只消费旧终态事件 1，不能因 active 或会话后续事件推进游标。
    assert _history_snapshot_event_seq(1, 3) == 1

    # active 事件位于首条事件时，游标稳定钳制在零。
    assert _history_snapshot_event_seq(2, 1) == 0


def test_run_history_includes_all_sessions_with_pagination_and_session_filter() -> None:
    """全局查询不能受当前会话限制，分页和会话筛选应各自正确。"""
    client = make_client()
    first = client.get("/api/runs?limit=1").json()
    assert first["total"] == 2
    assert first["has_more"] is True
    assert first["items"][0]["session_id"] == "session-2"
    second = client.get("/api/runs?limit=1&offset=1").json()
    assert second["has_more"] is False
    assert second["items"][0]["run_id"] == "run-1"
    scoped = client.get("/api/runs?session_id=session-1").json()
    assert scoped["total"] == 1
    assert scoped["items"][0]["started_at"] == "2026-01-01T00:00:55+00:00"
    assert scoped["items"][0]["title"] == "hello"
    assert client.get("/api/runs?session_id=missing").status_code == 404
    assert client.get("/api/runs?limit=201").status_code == 422
    assert client.get("/api/runs?offset=-1").status_code == 422


def test_run_timeline_preserves_execution_start_in_summary() -> None:
    """时间线分页也必须保留摘要起点，以便缺少开始事件时计算耗时。"""
    body = make_client(EventHistory()).get("/api/sessions/session-1/runs/run-1/timeline").json()
    assert body["run"]["started_at"] == "2026-01-01T00:00:55+00:00"
