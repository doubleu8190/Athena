"""项目领域实体。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Project:
    """可以容纳聊天和项目级记忆的命名空间。"""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    deleted_time: datetime | None = None


__all__ = ["Project"]
