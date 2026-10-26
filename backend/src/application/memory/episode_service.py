"""任务回顾的管理用例。"""

from __future__ import annotations

from domain.memory import MemoryEpisode, MemoryEpisodeRepositoryPort


class MemoryEpisodeManagementService:
    """为 MemoryView 提供分页回顾和遗忘能力。"""

    def __init__(self, repository: MemoryEpisodeRepositoryPort) -> None:
        self._repository = repository

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None
    ) -> tuple[list[MemoryEpisode], int]:
        """读取最近的任务回顾和总数。"""
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("memory_episode_pagination_invalid")
        return await self._repository.list_recent(
            limit=limit, offset=offset, session_id=session_id
        )

    async def forget(self, episode_id: str) -> bool:
        """遗忘任务回顾，阻止相同回合重建回顾与 procedure。"""
        return await self._repository.forget(episode_id)


__all__ = ["MemoryEpisodeManagementService"]
