"""项目领域实体。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Project:
    """可以容纳聊天和项目级记忆的命名空间。"""

    id: str
    name: str
    created_at: str
    updated_at: str
    deleted_time: str | None = None


__all__ = ["Project"]
