"""会话历史一致性查询端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.events import ApplicationEvent
from domain.sessions import Message


@dataclass(frozen=True, slots=True)
class HistoryRunRecord:
    run_id: str
    message_id: str
    sequence: int


@dataclass(frozen=True, slots=True)
class SessionHistorySnapshot:
    runs: tuple[HistoryRunRecord, ...]
    messages: tuple[Message, ...]
    events: tuple[ApplicationEvent, ...]
    has_more: bool
    snapshot_event_seq: int


class SessionHistoryQueryPort(Protocol):
    async def read_snapshot(
        self,
        session_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> SessionHistorySnapshot: ...
