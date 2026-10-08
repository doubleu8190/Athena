"""事件历史查询端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import ApplicationEvent


class EventHistoryPort(Protocol):
    async def list_events_between(self, session_id: str, after: int = 0, upto: int | None = None) -> list[ApplicationEvent]: ...


__all__ = ["EventHistoryPort"]
