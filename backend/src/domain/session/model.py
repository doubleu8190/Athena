"""会话目录与可见性领域对象及业务异常。"""

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


class SessionNotFoundError(LookupError):
    """会话不存在、已经删除或不属于当前用户。"""


class InvalidSessionTitleError(ValueError):
    """会话标题不满足展示名称约束。"""


class InvalidSessionUpdateError(ValueError):
    """会话更新请求没有可修改字段。"""


class InvalidSessionFilterError(ValueError):
    """会话列表筛选参数组合无效。"""


__all__ = [
    "InvalidSessionFilterError",
    "InvalidSessionTitleError",
    "InvalidSessionUpdateError",
    "Session",
    "SessionNotFoundError",
    "SessionPage",
    "SessionScope",
]
