"""项目用例。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from domain.projects import Project, ProjectRepositoryPort


class ProjectNotFoundError(LookupError):
    """目标项目不存在或已经删除。"""


class ProjectService:
    """创建和管理聊天项目。"""

    def __init__(
        self,
        repository: ProjectRepositoryPort,
        *,
        id_factory: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存项目仓储及可替换的 ID、时钟来源。"""
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def list(self) -> list[Project]:
        """返回未删除项目，顺序由仓储按最近聊天活动决定。"""
        return await self._repository.list()

    async def get(self, project_id: str) -> Project:
        """按 ID 读取项目；找不到时抛出 ProjectNotFoundError。"""
        project = await self._repository.get(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    async def create(self, name: str) -> Project:
        """创建项目并分配稳定 ID；名称允许与现有项目相同。"""
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("project_name_invalid")
        now = self._clock().astimezone(timezone.utc).isoformat()
        return await self._repository.create(
            Project(
                id=self._id_factory(),
                name=normalized,
                created_at=now,
                updated_at=now,
            )
        )

    async def rename(self, project_id: str, name: str) -> Project:
        """更新项目名称；ID 与会话、记忆归属保持不变。"""
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("project_name_invalid")
        project = await self._repository.rename(project_id, normalized)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    async def delete(self, project_id: str) -> None:
        """删除项目和项目级数据；活动 Run 会由仓储拒绝删除。"""
        if not await self._repository.delete(project_id):
            raise ProjectNotFoundError(project_id)


__all__ = ["ProjectNotFoundError", "ProjectService"]
