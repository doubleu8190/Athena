"""会话目录与可见性领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SessionScope:
    """标识一次按所有者查询会话的可见范围。"""

    session_id: str
    owner_id: str


@dataclass(frozen=True, slots=True)
class Session:
    """用户可见的会话目录对象。"""

    id: str
    owner_id: str
    title: str | None
    project_id: str | None
    last_message_sequence: int
    created_at: datetime
    updated_at: datetime
    deleted_time: datetime | None


@dataclass(frozen=True, slots=True)
class SessionPage:
    """按最近修改时间排序的有效会话分页。"""

    items: tuple[Session, ...]
    total: int


__all__ = ["Session", "SessionPage", "SessionScope"]
