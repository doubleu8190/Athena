"""个人工作偏好与显式记忆应用服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from common.date import as_utc, utc_now
from common.ids import new_id
from domain.preference import (
    MAX_USER_MEMORIES_PER_USER,
    PreferenceContext,
    PreferenceError,
    PreferenceRepository,
    UserMemory,
    UserMemoryList,
    UserMemoryNotFoundError,
    UserMemorySource,
    UserWorkProfile,
    validate_custom_instructions,
    validate_user_memory_content,
)


class PreferenceService:
    """编排用户个人工作偏好设置与显式备忘记忆的读写用例。"""

    def __init__(
        self,
        repository: PreferenceRepository,
        *,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = utc_now,
        max_memories: int = MAX_USER_MEMORIES_PER_USER,
    ) -> None:
        """注入仓储端口、ID 生成器、时钟与每用户记忆配额上限。"""
        if max_memories < 1:
            raise ValueError("max_memories_invalid")
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock
        self._max_memories = max_memories

    @property
    def max_memories(self) -> int:
        """返回每用户显式记忆硬性上限。"""
        return self._max_memories

    async def get_profile(self, user_id: str) -> UserWorkProfile:
        """读取当前用户工作偏好；尚无记录时返回默认空指令且默认开启记忆。"""
        profile = await self._repository.get_profile(user_id)
        if profile is None:
            return UserWorkProfile(
                user_id=user_id,
                custom_instructions="",
                memory_enabled=True,
                updated_at=None,
            )
        return profile

    async def update_profile(
        self,
        user_id: str,
        *,
        custom_instructions: str,
        memory_enabled: bool,
    ) -> UserWorkProfile:
        """校验并持久化用户个人工作偏好与显式记忆开关。"""
        normalized_instructions = validate_custom_instructions(custom_instructions)
        if not isinstance(memory_enabled, bool):
            raise PreferenceError("memory_enabled_invalid")
        return await self._repository.upsert_profile(
            user_id,
            custom_instructions=normalized_instructions,
            memory_enabled=memory_enabled,
            updated_at=self._now(),
        )

    async def list_memories(self, user_id: str) -> UserMemoryList:
        """按最近更新时间倒序返回当前用户的全部显式记忆及配额信息。"""
        items = await self._repository.list_memories(user_id)
        return UserMemoryList(
            items=items,
            total=len(items),
            limit=self._max_memories,
        )

    async def create_memory(
        self,
        user_id: str,
        content: str,
        *,
        source: UserMemorySource = UserMemorySource.MANUAL,
    ) -> UserMemory:
        """校验正文与配额后新增一条显式记忆；对话工具写入时在锁内复核开关状态。"""
        normalized_content = validate_user_memory_content(content)
        now = self._now()
        memory = UserMemory(
            id=self._id_factory("umem"),
            user_id=user_id,
            content=normalized_content,
            source=source,
            created_at=now,
            updated_at=now,
        )
        # 对话工具写入受 memory_enabled 开关约束；手动设置页允许在关闭注入时预先维护列表。
        return await self._repository.create_memory(
            memory,
            max_memories=self._max_memories,
            require_memory_enabled=(source is UserMemorySource.CHAT_EXPLICIT),
        )

    async def update_memory(
        self,
        user_id: str,
        memory_id: str,
        *,
        content: str,
    ) -> UserMemory:
        """更新当前用户的一条显式记忆正文；跨用户或不存在统一抛出未找到错误。"""
        normalized_content = validate_user_memory_content(content)
        updated = await self._repository.update_memory(
            user_id,
            memory_id,
            content=normalized_content,
            updated_at=self._now(),
        )
        if updated is None:
            raise UserMemoryNotFoundError("user_memory_not_found")
        return updated

    async def delete_memory(self, user_id: str, memory_id: str) -> None:
        """删除当前用户的一条显式记忆；跨用户或不存在统一抛出未找到错误。"""
        deleted = await self._repository.delete_memory(user_id, memory_id)
        if not deleted:
            raise UserMemoryNotFoundError("user_memory_not_found")

    async def get_context(self, user_id: str) -> PreferenceContext:
        """读取供主 Run 注入模型上下文的个人偏好与显式记忆快照。"""
        return await self._repository.get_context(user_id)

    async def is_memory_enabled(self, user_id: str) -> bool:
        """判断当前用户是否开启显式记忆开关（默认开启）。"""
        profile = await self.get_profile(user_id)
        return profile.memory_enabled

    def _now(self) -> datetime:
        """统一输出 UTC 时区时间。"""
        return as_utc(self._clock())


__all__ = ["PreferenceService"]
