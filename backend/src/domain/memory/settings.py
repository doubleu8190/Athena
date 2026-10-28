"""会话级记忆读取设置。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionMemorySettings:
    """控制本会话是否读取历史记忆。"""

    session_id: str
    use_memories: bool = True
    project_id: str | None = None


__all__ = ["SessionMemorySettings"]
