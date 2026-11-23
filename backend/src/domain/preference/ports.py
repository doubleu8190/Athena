"""个人工作偏好与显式记忆仓储端口协议。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.preference.model import PreferenceContext, UserMemory, UserWorkProfile


class PreferenceRepository(Protocol):
    """个人工作偏好与显式记忆持久化端口，向应用层屏蔽数据库实现细节。"""

    async def get_profile(self, user_id: str) -> UserWorkProfile | None:
        """读取用户个人工作偏好记录；尚未配置过时返回 None。"""
        ...

    async def upsert_profile(
        self,
        user_id: str,
        *,
        custom_instructions: str,
        memory_enabled: bool,
        updated_at: datetime,
    ) -> UserWorkProfile:
        """在用户行锁保护下原子创建或更新个人工作偏好。"""
        ...

    async def list_memories(self, user_id: str) -> tuple[UserMemory, ...]:
        """按 updated_at DESC, id DESC 返回当前用户全部显式记忆。"""
        ...

    async def create_memory(
        self,
        memory: UserMemory,
        *,
        max_memories: int,
        require_memory_enabled: bool = False,
    ) -> UserMemory:
        """锁定当前用户行后校验开关与配额上限，并原子写入一条显式记忆。"""
        ...

    async def update_memory(
        self,
        user_id: str,
        memory_id: str,
        *,
        content: str,
        updated_at: datetime,
    ) -> UserMemory | None:
        """更新当前用户的单条显式记忆正文；不存在或跨用户访问返回 None。"""
        ...

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        """删除当前用户的单条显式记忆；不存在或跨用户访问返回 False。"""
        ...

    async def get_context(self, user_id: str) -> PreferenceContext:
        """读取供主 Run 注入的完整个人工作偏好与显式记忆上下文。"""
        ...


__all__ = ["PreferenceRepository"]
