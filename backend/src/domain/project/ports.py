"""项目管理仓储端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.project.model.entity import Project, ProjectPage


class ProjectReader(Protocol):
    """其他业务模块只读查询项目时使用的窄端口。"""

    async def get(self, owner_id: str, project_id: str) -> Project | None:
        """读取未删除项目。"""


class ProjectRepository(ProjectReader, Protocol):
    """项目持久化端口。"""

    async def create(self, project: Project) -> Project:
        """保存新项目。"""

    async def list_active(
        self,
        owner_id: str,
        *,
        offset: int,
        limit: int,
    ) -> ProjectPage:
        """分页读取未删除项目。"""

    async def rename(
        self,
        owner_id: str,
        project_id: str,
        name: str,
        updated_at: datetime,
    ) -> Project | None:
        """原子修改未删除项目的名称。"""

    async def soft_delete(
        self,
        owner_id: str,
        project_id: str,
        deleted_at: datetime,
    ) -> bool:
        """软删除项目；已删除项目仍视为存在。

        删除事务必须锁定项目并拒绝仍存在未删除绑定会话的删除，避免与会话
        创建或改绑并发时破坏占用不变量。
        """


__all__ = ["ProjectReader", "ProjectRepository"]
