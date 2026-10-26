"""会话记忆开关持久化端口。"""

from __future__ import annotations

from typing import Protocol

from .settings import SessionMemorySettings


class SessionMemorySettingsPort(Protocol):
    """读取和保存指定会话的记忆偏好。"""

    async def get(self, session_id: str) -> SessionMemorySettings:
        """读取开关；尚无设置行时返回默认开启状态。"""
        ...

    async def save(self, settings: SessionMemorySettings) -> SessionMemorySettings:
        """原子保存读取和生成开关。"""
        ...


__all__ = ["SessionMemorySettingsPort"]
