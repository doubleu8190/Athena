"""会话历史一致性查询端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from domain.sessions import Message
from application.observability import ProgressEntry


@dataclass(frozen=True, slots=True)
class HistoryRunRecord:
    run_id: str
    message_id: str
    sequence: int
    status: str


@dataclass(frozen=True, slots=True)
class HistoryTaskRecord:
    """一次 Worker execution 的稳定展示事实。"""

    run_id: str
    task_id: str
    execution_id: str
    worker_name: str
    task_title: str
    status: str
    created_at: datetime
    completed_at: datetime | None
    display_result: str | None
    display_result_at: datetime | None


@dataclass(frozen=True, slots=True)
class SessionHistorySnapshot:
    runs: tuple[HistoryRunRecord, ...]
    messages: tuple[Message, ...]
    progress: tuple[ProgressEntry, ...]
    tasks: tuple[HistoryTaskRecord, ...]
    has_more: bool
    snapshot_event_seq: int
    snapshot_progress_ingest_seq: int
    active_run_id: str | None = None
    snapshot_run_seq: int = 0


@dataclass(frozen=True, slots=True)
class SessionTurnSnapshot:
    """SSE 快照中一个 Run 的完整、不可拆分展示事实。"""

    run: HistoryRunRecord
    messages: tuple[Message, ...]
    progress: tuple[ProgressEntry, ...]
    tasks: tuple[HistoryTaskRecord, ...]


@dataclass(frozen=True, slots=True)
class ActiveSessionSnapshot:
    """覆盖客户端历史水位之后一个有界页的可见事实。"""

    turns: tuple[SessionTurnSnapshot, ...]
    snapshot_event_seq: int
    snapshot_progress_ingest_seq: int
    snapshot_run_seq: int
    has_more: bool = False


class SessionHistoryQueryPort(Protocol):
    async def read_snapshot(
        self,
        session_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> SessionHistorySnapshot: ...

    async def read_stream_snapshot(
        self,
        session_id: str,
        *,
        after_event_sequence: int,
        after_progress_ingest_sequence: int,
        after_run_sequence: int | None,
        active_run_id: str | None,
        limit: int = 20,
        include_latest_if_empty: bool = False,
    ) -> ActiveSessionSnapshot: ...
