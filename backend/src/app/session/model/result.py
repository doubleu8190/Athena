"""供 HTTP 层使用的会话应用结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SessionResult:
    """不暴露所有者与软删除状态的稳定会话结果。"""

    id: str
    title: str | None
    project_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SessionPageResult:
    """会话应用分页结果。"""

    items: tuple[SessionResult, ...]
    total: int


__all__ = ["SessionPageResult", "SessionResult"]
