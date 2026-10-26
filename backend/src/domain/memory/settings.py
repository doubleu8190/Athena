"""会话级记忆读写开关。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionMemorySettings:
    """分别控制本会话读取历史记忆和生成新记忆。"""

    session_id: str
    use_memories: bool = True
    generate_memories: bool = True
    project_id: str | None = None


__all__ = ["SessionMemorySettings"]
