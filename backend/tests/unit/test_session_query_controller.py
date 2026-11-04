"""会话消息和运行列表 Controller 契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from application.observability import ProgressEntry, ProgressSource
from application.runs import RunQueryService
from application.sessions import SessionMessageQueryService, SessionService
from application.sessions.history_port import (
    HistoryRunRecord,
    HistoryTaskRecord,
    SessionHistorySnapshot,
)
from bootstrap import create_app
from domain.runs import RunStatus, RunSummary
from domain.sessions import Message, MessageRole, Session


class SessionRepository:
    """提供 Controller 测试所需的最小会话仓储。"""

    def __init__(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.items = {
            "session-1": Session.create(
                session_id="session-1",
                title="Session",
                now=now,
            ),
            "session-2": Session.create(
                session_id="session-2",
                title="Other Session",
                now=now,
            ),
        }

    async def create(self, value: Session) -> Session:
        self.items[value.id] = value
        return value

    async def get(self, session_id: str) -> Session | None:
        return self.items.get(session_id)

    async def list_sessions(self) -> list[Session]:
        return list(self.items.values())

    async def save(self, value: Session) -> Session:
        self.items[value.id] = value
        return value

    async def delete(self, session_id: str) -> bool:
        return self.items.pop(session_id, None) is not None


class RunRepository:
    """提供会话运行列表测试数据。"""

    async def list_for_session(self, session_id: str) -> list[RunSummary]:
        if session_id != "session-1":
            return []
        return [
            RunSummary(
                run_id="run-1",
                session_id=session_id,
                message_id="user-1",
                sequence=1,
                status=RunStatus.COMPLETED,
                root_thread_id="thread-1",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:01:00+00:00",
                started_at="2026-01-01T00:00:55+00:00",
                title="hello",
            )
        ]

    async def list_recent(
        self,
        *,
        limit: int,
        offset: int,
        session_id: str | None = None,
        terminal_only: bool = False,
    ) -> tuple[list[RunSummary], int]:
        values = [
            RunSummary(
                run_id="run-2",
                session_id="session-2",
                message_id="message-2",
                sequence=2,
                status=RunStatus.CANCELLED,
                root_thread_id="thread-2",
                created_at="2026-01-02T00:00:00Z",
            ),
            RunSummary(
                run_id="run-running",
                session_id="session-2",
                message_id="message-running",
                sequence=3,
                status=RunStatus.RUNNING,
                root_thread_id="thread-running",
                created_at="2026-01-03T00:00:00Z",
            ),
            *(await self.list_for_session("session-1")),
        ]
        if session_id is not None:
            values = [value for value in values if value.session_id == session_id]
        if terminal_only:
            values = [
                value
                for value in values
                if value.status
                in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }
            ]
        return values[offset : offset + limit], len(values)


class HistorySnapshot:
    """返回两页稳定历史并记录分页参数。"""

    def __init__(self) -> None:
        self.before_sequences: list[int | None] = []
        self._snapshot = _history_snapshot()

    async def read_snapshot(
        self,
        session_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> SessionHistorySnapshot:
        self.before_sequences.append(before_sequence)
        if before_sequence is not None:
            return SessionHistorySnapshot(
                runs=(),
                messages=(),
                progress=(),
                tasks=(),
                has_more=False,
                snapshot_event_seq=self._snapshot.snapshot_event_seq,
                snapshot_progress_ingest_seq=(
                    self._snapshot.snapshot_progress_ingest_seq
                ),
                snapshot_run_seq=self._snapshot.snapshot_run_seq,
            )
        return self._snapshot


def _history_snapshot() -> SessionHistorySnapshot:
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    runs = (
        HistoryRunRecord("run-new", "user-new", 2, "failed"),
        HistoryRunRecord("run-old", "user-old", 1, "completed"),
    )
    messages = (
        Message(
            id="user-old",
            session_id="session-1",
            role=MessageRole.USER,
            content="旧问题",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            run_id="run-old",
        ),
        Message(
            id="assistant-old",
            session_id="session-1",
            role=MessageRole.ASSISTANT,
            content="旧回答",
            timestamp=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            run_id="run-old",
        ),
        Message(
            id="user-new",
            session_id="session-1",
            role=MessageRole.USER,
            content="新问题",
            timestamp=base,
            run_id="run-new",
        ),
    )
    progress = (
        ProgressEntry(
            progress_id="root-2",
            session_id="session-1",
            run_id="run-new",
            source=ProgressSource.ROOT,
            sequence=2,
            content="Root 第二步",
            created_at=base.replace(second=2),
            ingest_sequence=6,
        ),
        ProgressEntry(
            progress_id="root-1",
            session_id="session-1",
            run_id="run-new",
            source=ProgressSource.ROOT,
            sequence=1,
            content="Root 第一步",
            created_at=base.replace(second=1),
            ingest_sequence=5,
        ),
        ProgressEntry(
            progress_id="task-a-2",
            session_id="session-1",
            run_id="run-new",
            execution_id="execution-a",
            task_id="task-shared",
            worker_id="worker-a",
            source=ProgressSource.WORKER,
            sequence=2,
            content="第一次执行第二步",
            created_at=base.replace(second=12),
            ingest_sequence=12,
        ),
        ProgressEntry(
            progress_id="task-b-1",
            session_id="session-1",
            run_id="run-new",
            execution_id="execution-b",
            task_id="task-shared",
            worker_id="worker-a",
            source=ProgressSource.WORKER,
            sequence=1,
            content="重试执行开始",
            created_at=base.replace(second=20),
            ingest_sequence=20,
        ),
        ProgressEntry(
            progress_id="task-a-1",
            session_id="session-1",
            run_id="run-new",
            execution_id="execution-a",
            task_id="task-shared",
            worker_id="worker-a",
            source=ProgressSource.WORKER,
            sequence=1,
            content="第一次执行开始",
            created_at=base.replace(second=11),
            ingest_sequence=11,
        ),
    )
    tasks = (
        HistoryTaskRecord(
            run_id="run-new",
            task_id="task-shared",
            execution_id="execution-a",
            worker_name="research-worker",
            task_title="检索资料",
            status="failed",
            created_at=base.replace(second=10),
            completed_at=base.replace(second=15),
            display_result=None,
            display_result_at=None,
        ),
        HistoryTaskRecord(
            run_id="run-new",
            task_id="task-shared",
            execution_id="execution-b",
            worker_name="research-worker",
            task_title="重试检索资料",
            status="completed",
            created_at=base.replace(second=19),
            completed_at=base.replace(second=25),
            display_result="找到三条资料",
            display_result_at=base.replace(second=24),
        ),
        HistoryTaskRecord(
            run_id="run-new",
            task_id="task-shared",
            execution_id="execution-c",
            worker_name="research-worker",
            task_title="取消后不再检索",
            status="cancelled",
            created_at=base.replace(second=26),
            completed_at=base.replace(second=27),
            display_result=None,
            display_result_at=None,
        ),
    )
    return SessionHistorySnapshot(
        runs=runs,
        messages=messages,
        progress=progress,
        tasks=tasks,
        has_more=True,
        snapshot_event_seq=30,
        snapshot_progress_ingest_seq=20,
        snapshot_run_seq=2,
    )


def make_client() -> tuple[TestClient, HistorySnapshot]:
    sessions = SessionRepository()
    history = HistorySnapshot()
    return (
        TestClient(
            create_app(
                session_service=SessionService(
                    sessions,
                    id_factory=lambda: "unused",
                ),
                message_query_service=SessionMessageQueryService(
                    sessions,
                    history,
                ),
                run_query_service=RunQueryService(
                    sessions,
                    RunRepository(),
                ),
            )
        ),
        history,
    )


def test_messages_return_lean_atomic_terminal_turns() -> None:
    """历史响应只暴露展示字段，并按用户消息和 Worker execution 原子分组。"""
    client, history_reader = make_client()

    response = client.get("/api/sessions/session-1/messages")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "next_cursor", "sync_token"}
    assert body["sync_token"]
    assert body["next_cursor"]
    assert [item["user"]["content"] for item in body["items"]] == [
        "旧问题",
        "新问题",
    ]

    old_turn, new_turn = body["items"]
    assert set(old_turn) == {"user", "root_process", "tasks", "assistant"}
    assert old_turn["assistant"] == {
        "time": "2026-01-01T00:01:00Z",
        "content": "旧回答",
    }
    assert set(new_turn) == {"user", "root_process", "tasks", "assistant"}
    assert new_turn["assistant"] is None
    assert [item["content"] for item in new_turn["root_process"]] == [
        "Root 第一步",
        "Root 第二步",
    ]
    assert all(
        set(item) == {"time", "content"}
        for item in new_turn["root_process"]
    )
    assert len(new_turn["tasks"]) == 3
    assert all(
        set(task) == {"status", "worker_name", "task_title", "process", "result"}
        for task in new_turn["tasks"]
    )
    assert [
        item["content"]
        for item in new_turn["tasks"][0]["process"]
    ] == ["第一次执行开始", "第一次执行第二步"]
    assert [
        item["content"]
        for item in new_turn["tasks"][1]["process"]
    ] == ["重试执行开始"]
    assert new_turn["tasks"][0]["result"]["content"] == "任务执行失败。"
    assert new_turn["tasks"][1]["result"]["content"] == "找到三条资料"
    assert new_turn["tasks"][2]["result"]["content"] == "任务已取消。"
    assert [task["status"] for task in new_turn["tasks"]] == [
        "failed",
        "completed",
        "cancelled",
    ]

    serialized = response.text
    for internal_field in (
        "run-new",
        "user-new",
        "execution-a",
        "execution-b",
        "progress_id",
        "task_id",
        "session_seq",
        "snapshot_event_seq",
    ):
        assert internal_field not in serialized

    next_page = client.get(
        "/api/sessions/session-1/messages",
        params={"cursor": body["next_cursor"]},
    )
    assert next_page.status_code == 200
    assert next_page.json()["items"] == []
    assert history_reader.before_sequences == [None, 1]


def test_messages_and_runs_return_404_for_unknown_session() -> None:
    client, _ = make_client()

    assert client.get("/api/sessions/missing/messages").status_code == 404
    assert client.get("/api/sessions/missing/runs").status_code == 404


def test_runs_terminal_only_filters_total_and_pagination() -> None:
    """终态筛选必须同时约束列表、total 与 has_more。"""
    client, _ = make_client()

    first = client.get(
        "/api/runs",
        params={"terminal_only": "true", "limit": 1, "offset": 0},
    )
    second = client.get(
        "/api/runs",
        params={"terminal_only": "true", "limit": 1, "offset": 1},
    )
    unfiltered = client.get("/api/runs")

    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert first.json()["has_more"] is True
    assert first.json()["items"][0]["run_id"] == "run-2"
    assert second.status_code == 200
    assert second.json()["total"] == 2
    assert second.json()["has_more"] is False
    assert second.json()["items"][0]["run_id"] == "run-1"
    assert unfiltered.status_code == 200
    assert unfiltered.json()["total"] == 3


def test_messages_reject_invalid_or_cross_session_cursor() -> None:
    client, _ = make_client()

    invalid = client.get(
        "/api/sessions/session-1/messages",
        params={"cursor": "not-a-cursor"},
    )
    cross_session = client.get(
        "/api/sessions/session-1/messages",
        params={"cursor": "c2Vzc2lvbi0yOjE"},
    )

    assert invalid.status_code == 400
    assert cross_session.status_code == 400
