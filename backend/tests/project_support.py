"""项目与会话 HTTP 测试共享的内存项目仓储夹具。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from domain.project import Project, ProjectHasSessionsError, ProjectPage


class InMemoryProjectRepository:
    """为项目与会话 HTTP 测试保存并按 owner 过滤项目实体。"""

    def __init__(self, projects: tuple[Project, ...] = ()) -> None:
        self.items: dict[str, Project] = {project.id: project for project in projects}
        # 可选挂载 InMemorySessionRepository，用于联动校验/解绑绑定会话。
        self.session_repository: Any | None = None

    async def create(self, project: Project) -> Project:
        self.items[project.id] = project
        return project

    async def get(self, owner_id: str, project_id: str) -> Project | None:
        project = self.items.get(project_id)
        if project is None or project.owner_id != owner_id or project.deleted_time is not None:
            return None
        return project

    async def list_active(
        self,
        owner_id: str,
        *,
        offset: int,
        limit: int,
    ) -> ProjectPage:
        active = sorted(
            (
                project
                for project in self.items.values()
                if project.owner_id == owner_id and project.deleted_time is None
            ),
            key=lambda project: (project.updated_at, project.id),
            reverse=True,
        )
        return ProjectPage(items=tuple(active[offset : offset + limit]), total=len(active))

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
        project = await self.get(owner_id, project_id)
        if project is None:
            return None
        updated = replace(
            project,
            name=name if update_name and name is not None else project.name,
            description=description if update_description else project.description,
            instructions=instructions if update_instructions else project.instructions,
            default_skill_ids=(
                default_skill_ids or () if update_default_skills else project.default_skill_ids
            ),
            updated_at=updated_at,
        )
        self.items[project_id] = updated
        return updated

    async def soft_delete(
        self,
        owner_id: str,
        project_id: str,
        deleted_at: datetime,
        *,
        detach_sessions: bool = False,
    ) -> bool:
        project = self.items.get(project_id)
        if project is None or project.owner_id != owner_id:
            return False
        if project.deleted_time is None:
            if self.session_repository is not None:
                bound_ids = [
                    session.id
                    for session in self.session_repository.items.values()
                    if session.owner_id == owner_id
                    and session.project_id == project_id
                    and session.deleted_time is None
                ]
                if bound_ids:
                    if not detach_sessions:
                        raise ProjectHasSessionsError("project_has_sessions")
                    for session_id in bound_ids:
                        existing = self.session_repository.items[session_id]
                        self.session_repository.items[session_id] = replace(
                            existing,
                            project_id=None,
                        )
            self.items[project_id] = replace(
                project,
                updated_at=deleted_at,
                deleted_time=deleted_at,
            )
        return True


__all__ = ["InMemoryProjectRepository"]
