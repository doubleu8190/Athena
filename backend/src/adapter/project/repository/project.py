"""项目目录的 async SQLAlchemy 持久化适配器。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.knowledge.model import ProjectKnowledgeBaseModel
from adapter.project.converter.project import project_to_domain
from adapter.project.model.project import ProjectModel
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from domain.project import (
    Project,
    ProjectHasSessionsError,
    ProjectPage,
    ProjectRepository,
)
from domain.run import RunStatus, SessionHasActiveRunError

_ACTIVE_RUN_STATUSES = (
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_FOR_INPUT.value,
    RunStatus.WAITING_FOR_APPROVAL.value,
    RunStatus.WAITING_FOR_SUBAGENTS.value,
    RunStatus.CANCELLING.value,
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
                        description=project.description,
                        instructions=project.instructions,
                        default_skill_ids=list(project.default_skill_ids),
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
        """原子局部更新未删除项目的目录与工作台配置字段。"""
        values: dict[str, object] = {"updated_at": updated_at}
        if update_name:
            assert name is not None
            values["name"] = name
        if update_description:
            values["description"] = description
        if update_instructions:
            values["instructions"] = instructions
        if update_default_skills:
            values["default_skill_ids"] = list(default_skill_ids or ())
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
                        .values(**values)
                        .returning(ProjectModel)
                    )
                ).scalar_one_or_none()
        return None if row is None else project_to_domain(row)

    async def soft_delete(
        self,
        owner_id: str,
        project_id: str,
        deleted_at: datetime,
        *,
        detach_sessions: bool = False,
    ) -> bool:
        """锁定项目行执行软删除；按 detach_sessions 决定拒绝还是先解绑无活跃 Run 的会话。"""
        async with self._session_factory() as session:
            async with session.begin():
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
                    return True
                if detach_sessions:
                    # 锁定该项目下全部未删除会话，确保检查活跃 Run 与解绑在同一事务内原子完成。
                    bound_sessions = (
                        await session.scalars(
                            select(SessionModel)
                            .where(
                                SessionModel.owner_id == owner_id,
                                SessionModel.project_id == project_id,
                                SessionModel.deleted_time.is_(None),
                            )
                            .order_by(SessionModel.id)
                            .with_for_update()
                        )
                    ).all()
                    if bound_sessions:
                        bound_session_ids = [item.id for item in bound_sessions]
                        active_run_id = await session.scalar(
                            select(RunModel.id)
                            .where(
                                RunModel.session_id.in_(bound_session_ids),
                                RunModel.status.in_(_ACTIVE_RUN_STATUSES),
                            )
                            .limit(1)
                        )
                        if active_run_id is not None:
                            raise SessionHasActiveRunError("session_has_active_run")
                        await session.execute(
                            update(SessionModel)
                            .where(
                                SessionModel.owner_id == owner_id,
                                SessionModel.project_id == project_id,
                                SessionModel.deleted_time.is_(None),
                            )
                            .values(project_id=None)
                        )
                else:
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
                await session.flush()
        return True


__all__ = ["SqlAlchemyProjectRepository"]
