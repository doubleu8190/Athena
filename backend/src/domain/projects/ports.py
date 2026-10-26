"""项目仓储端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import Project


class ProjectRepositoryPort(Protocol):
    """访问项目目录并执行项目生命周期操作。"""

    async def create(self, project: Project) -> Project:
        """保存新项目。"""
        ...

    async def get(self, project_id: str) -> Project | None:
        """读取一个未删除项目。"""
        ...

    async def list(self) -> list[Project]:
        """按最近聊天活动列出未删除项目。"""
        ...

    async def rename(self, project_id: str, name: str) -> Project | None:
        """更新项目名称；项目不存在时返回 None。"""
        ...

    async def delete(self, project_id: str) -> bool:
        """清理项目记忆并删除项目及其聊天。"""
        ...


__all__ = ["ProjectRepositoryPort"]
