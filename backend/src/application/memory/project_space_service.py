"""显式项目记忆空间目录用例。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from domain.memory import MemoryProjectSpace, MemoryProjectSpaceRepositoryPort


class MemoryProjectSpaceService:
    """创建和列出可由用户明确绑定的项目记忆空间。"""

    def __init__(
        self,
        repository: MemoryProjectSpaceRepositoryPort,
        *,
        id_factory: Callable[[], str],
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory

    async def list(self) -> list[MemoryProjectSpace]:
        """返回显式创建的项目空间目录。"""
        return await self._repository.list()

    async def forget(self, project_id: str) -> bool:
        """删除项目空间，并抑制它的记忆、回顾和旧队列来源。"""
        return await self._repository.forget(project_id)

    async def create(self, name: str) -> MemoryProjectSpace:
        """创建一个不与同名项目自动合并的项目空间。

        参数：name 是用户提交的显示名称；空间身份由服务端新 ID 决定。
        返回：已保存的项目空间。
        异常：名称为空或超过 120 字时抛出 ValueError；存储错误向上传播。
        """
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("memory_project_name_invalid")
        now = datetime.now(timezone.utc).isoformat()
        project = MemoryProjectSpace(
            id=self._id_factory(),
            name=normalized,
            created_at=now,
            updated_at=now,
        )
        return await self._repository.create(project)


__all__ = ["MemoryProjectSpaceService"]
