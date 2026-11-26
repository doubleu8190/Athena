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

    async def update(
        self,
        owner_id: str,
        project_id: str,
        *,
        update_name: bool,
        name: str | None,
        update_description: bool,
        description: str | None,
        update_instructions: bool,
        instructions: str | None,
        update_default_skills: bool,
        default_skill_ids: tuple[str, ...] | None,
        updated_at: datetime,
    ) -> Project | None:
        """原子局部更新未删除项目的名称、简介、背景指令或默认技能。"""

    async def soft_delete(
        self,
        owner_id: str,
        project_id: str,
        deleted_at: datetime,
        *,
        detach_sessions: bool = False,
    ) -> bool:
        """软删除项目；已删除项目仍视为存在。

        删除事务必须锁定项目行：
        - detach_sessions=False 时拒绝仍存在未删除绑定会话的删除；
        - detach_sessions=True 时先检查项目下不存在活跃 Run，随后批量解绑会话并清理关联知识库。
        """


__all__ = ["ProjectReader", "ProjectRepository"]
