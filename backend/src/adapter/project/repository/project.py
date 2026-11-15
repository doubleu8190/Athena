"""项目目录的 async SQLAlchemy 持久化适配器。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.knowledge.model import ProjectKnowledgeBaseModel
from adapter.memory import disable_project_memory_scope, lock_user_memory_setting
from adapter.project.converter.project import project_to_domain
from adapter.project.model.project import ProjectModel
from adapter.session.model import SessionModel
from domain.project import (
    Project,
    ProjectHasSessionsError,
    ProjectPage,
    ProjectRepository,
)


class SqlAlchemyProjectRepository(ProjectRepository):
    """使用 PostgreSQL 兼容 SQL 持久化项目目录。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """保存会话工厂。"""
        self._session_factory = session_factory

    async def create(self, project: Project) -> Project:
        """插入新项目并返回同一领域对象。"""
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    ProjectModel(
                        id=project.id,
                        owner_id=project.owner_id,
                        name=project.name,
                        created_at=project.created_at,
                        updated_at=project.updated_at,
                        deleted_time=project.deleted_time,
                    )
                )
        return project

    async def get(self, owner_id: str, project_id: str) -> Project | None:
        """读取未删除项目。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ProjectModel).where(
                    ProjectModel.id == project_id,
                    ProjectModel.owner_id == owner_id,
                    ProjectModel.deleted_time.is_(None),
                )
            )
        return None if row is None else project_to_domain(row)

    async def list_active(
        self,
        owner_id: str,
        *,
        offset: int,
        limit: int,
    ) -> ProjectPage:
        """按最近修改时间倒序读取未删除项目。"""
        active = (
            ProjectModel.owner_id == owner_id,
            ProjectModel.deleted_time.is_(None),
        )
        async with self._session_factory() as session:
            total = int(
                (
                    await session.scalar(
                        select(func.count()).select_from(ProjectModel).where(*active)
                    )
                )
                or 0
            )
            rows = (
                await session.scalars(
                    select(ProjectModel)
                    .where(*active)
                    .order_by(ProjectModel.updated_at.desc(), ProjectModel.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return ProjectPage(items=tuple(project_to_domain(row) for row in rows), total=total)

    async def rename(
        self,
        owner_id: str,
        project_id: str,
        name: str,
        updated_at: datetime,
    ) -> Project | None:
        """原子修改未删除项目名称。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        update(ProjectModel)
                        .where(
                            ProjectModel.id == project_id,
                            ProjectModel.owner_id == owner_id,
                            ProjectModel.deleted_time.is_(None),
                        )
                        .values(name=name, updated_at=updated_at)
                        .returning(ProjectModel)
                    )
                ).scalar_one_or_none()
        return None if row is None else project_to_domain(row)

    async def soft_delete(
        self,
        owner_id: str,
        project_id: str,
        deleted_at: datetime,
    ) -> bool:
        """锁定项目并拒绝删除仍有有效会话的项目。"""
        async with self._session_factory() as session:
            async with session.begin():
                # 记忆 scope 失效必须先锁用户设置，避免与 control/extract 反锁。
                await lock_user_memory_setting(session, owner_id)
                row = await session.scalar(
                    select(ProjectModel)
                    .where(
                        ProjectModel.id == project_id,
                        ProjectModel.owner_id == owner_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                if row.deleted_time is not None:
                    await disable_project_memory_scope(
                        session,
                        owner_id=owner_id,
                        project_id=project_id,
                        disabled_at=row.deleted_time,
                    )
                    return True
                active_session_id = await session.scalar(
                    select(SessionModel.id)
                    .where(
                        SessionModel.owner_id == owner_id,
                        SessionModel.project_id == project_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .limit(1)
                )
                if active_session_id is not None:
                    raise ProjectHasSessionsError("project_has_sessions")
                await session.execute(
                    delete(ProjectKnowledgeBaseModel).where(
                        ProjectKnowledgeBaseModel.project_id == project_id,
                        ProjectKnowledgeBaseModel.owner_id == owner_id,
                    )
                )
                row.deleted_time = deleted_at
                row.updated_at = deleted_at
                # 项目可见性、知识关联、记忆 scope 禁用与清理任务必须在同一事务提交。
                await disable_project_memory_scope(
                    session,
                    owner_id=owner_id,
                    project_id=project_id,
                    disabled_at=deleted_at,
                )
                await session.flush()
        return True


__all__ = ["SqlAlchemyProjectRepository"]
