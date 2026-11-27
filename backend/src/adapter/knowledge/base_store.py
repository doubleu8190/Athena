"""知识库目录与项目绑定管理的 SQLAlchemy 子仓储。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access.repository import (
    build_readable_resource_clause,
    delete_resource_grants_in_session,
    load_grants_map_in_session,
    load_user_access_snapshot_in_session,
    sync_resource_grants_in_session,
)
from adapter.knowledge._shared import (
    _base,
    _load_base_grants,
    _lock_base,
    _new_queued_job,
)
from adapter.knowledge.model import (
    KnowledgeBaseModel,
    KnowledgeJobModel,
    ProjectKnowledgeBaseModel,
)
from adapter.project.model import ProjectModel
from common.ids import new_id
from domain.access import (
    ResourceType,
    UserAccessSnapshot,
    VisibilityGrantInput,
    VisibilityScope,
)
from domain.knowledge.model import (
    MAX_PROJECT_KNOWLEDGE_BASES,
    KnowledgeBase,
    ProjectKnowledgeLimitReachedError,
)


class KnowledgeBaseStore:
    """负责知识库本体、可见性授权与项目关联关系的事务持久化。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list_project_bases(self, owner_id: str, project_id: str) -> list[KnowledgeBase]:
        """读取用户项目关联且当前未删除的知识库（含可见性授权信息）。"""
        async with self._sessions() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeBaseModel)
                    .join(
                        ProjectKnowledgeBaseModel,
                        ProjectKnowledgeBaseModel.knowledge_base_id == KnowledgeBaseModel.id,
                    )
                    .where(
                        ProjectKnowledgeBaseModel.owner_id == owner_id,
                        ProjectKnowledgeBaseModel.project_id == project_id,
                        KnowledgeBaseModel.deleted_time.is_(None),
                    )
                    .order_by(KnowledgeBaseModel.name, KnowledgeBaseModel.id)
                )
            )
            grants_map = await load_grants_map_in_session(
                session, ResourceType.KNOWLEDGE_BASE, [row.id for row in rows]
            )
            return [_base(row, grants=grants_map.get(row.id, ())) for row in rows]

    async def bind_project_base(
        self, owner_id: str, project_id: str, base_id: str, at: datetime
    ) -> bool:
        """按项目→知识库锁顺序幂等关联用户项目与有权限的知识库。"""
        async with self._sessions() as session, session.begin():
            project = await session.scalar(
                select(ProjectModel)
                .where(
                    ProjectModel.id == project_id,
                    ProjectModel.owner_id == owner_id,
                    ProjectModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if project is None:
                return False
            # 复合外键已拆除：支持绑定 company/department 知识库及本人 personal 知识库。
            base = await _lock_base(session, base_id, owner_id=owner_id)
            if base is None:
                raise LookupError("knowledge_base_not_found")
            existing = await session.get(ProjectKnowledgeBaseModel, (project_id, base_id))
            if existing is None:
                active_count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ProjectKnowledgeBaseModel)
                        .join(
                            KnowledgeBaseModel,
                            KnowledgeBaseModel.id == ProjectKnowledgeBaseModel.knowledge_base_id,
                        )
                        .where(
                            ProjectKnowledgeBaseModel.project_id == project_id,
                            ProjectKnowledgeBaseModel.owner_id == owner_id,
                            KnowledgeBaseModel.deleted_time.is_(None),
                        )
                    )
                    or 0
                )
                if active_count >= MAX_PROJECT_KNOWLEDGE_BASES:
                    raise ProjectKnowledgeLimitReachedError("project_knowledge_limit_reached")
                session.add(
                    ProjectKnowledgeBaseModel(
                        project_id=project_id,
                        knowledge_base_id=base_id,
                        owner_id=owner_id,
                        created_at=at,
                    )
                )
            return True

    async def unbind_project_base(self, owner_id: str, project_id: str, base_id: str) -> bool:
        """按项目→知识库锁顺序幂等解除关联（即使调岗后仍允许解绑旧部门库）。"""
        async with self._sessions() as session, session.begin():
            project = await session.scalar(
                select(ProjectModel)
                .where(
                    ProjectModel.id == project_id,
                    ProjectModel.owner_id == owner_id,
                )
                .with_for_update()
            )
            if project is None:
                return False
            await _lock_base(session, base_id, active_only=False)
            link = await session.get(ProjectKnowledgeBaseModel, (project_id, base_id))
            if link is not None and link.owner_id == owner_id:
                await session.delete(link)
            return True

    async def create_base(
        self,
        item: KnowledgeBase,
        *,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> KnowledgeBase:
        """在单事务内原子创建知识库并写入关联的可见性授权。"""
        model = KnowledgeBaseModel(
            id=item.id,
            owner_id=item.owner_id,
            name=item.name,
            description=item.description,
            visibility_scope=item.visibility_scope.value,
            owner_department_id=item.owner_department_id,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
        async with self._sessions() as session, session.begin():
            session.add(model)
            await session.flush()
            grants = await sync_resource_grants_in_session(
                session,
                ResourceType.KNOWLEDGE_BASE,
                item.id,
                visibility_grants,
                now=item.created_at,
            )
            return _base(model, grants=grants)

    async def get_base_by_id(self, base_id: str) -> KnowledgeBase | None:
        """按主键读取未软删除的知识库（供权限引擎统一鉴权使用）。"""
        async with self._sessions() as session:
            model = await session.scalar(
                select(KnowledgeBaseModel).where(
                    KnowledgeBaseModel.id == base_id,
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
            )
            if model is None:
                return None
            return _base(model, grants=await _load_base_grants(session, model.id))

    async def list_readable_bases(
        self,
        user_id: str,
        *,
        offset: int,
        limit: int,
        visibility_scope: VisibilityScope | None = None,
        owner_department_id: str | None = None,
        snapshot: UserAccessSnapshot | None = None,
    ) -> list[KnowledgeBase]:
        """单 Session 加载/复用用户权限快照并以 SQL EXISTS 下推过滤、排序与分页知识库。"""
        normalized_user_id = user_id.strip()
        if snapshot is None and not normalized_user_id:
            return []
        if snapshot is not None and not snapshot.is_active:
            return []
        async with self._sessions() as session:
            resolved = (
                snapshot
                if snapshot is not None
                else await load_user_access_snapshot_in_session(session, normalized_user_id)
            )
            if resolved is None or not resolved.is_active:
                return []
            conditions: list[ColumnElement[bool]] = [
                KnowledgeBaseModel.deleted_time.is_(None),
                build_readable_resource_clause(
                    resolved,
                    ResourceType.KNOWLEDGE_BASE,
                    id_col=KnowledgeBaseModel.id,
                    scope_col=KnowledgeBaseModel.visibility_scope,
                    owner_id_col=KnowledgeBaseModel.owner_id,
                    owner_department_id_col=KnowledgeBaseModel.owner_department_id,
                ),
            ]
            if visibility_scope is not None:
                conditions.append(KnowledgeBaseModel.visibility_scope == visibility_scope.value)
            if owner_department_id is not None:
                conditions.append(KnowledgeBaseModel.owner_department_id == owner_department_id)
            rows = list(
                await session.scalars(
                    select(KnowledgeBaseModel)
                    .where(*conditions)
                    .order_by(KnowledgeBaseModel.updated_at.desc(), KnowledgeBaseModel.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            if not rows:
                return []
            # 仅对当前分页命中的知识库加载可见性授权，避免全表加载 grants。
            grants_map = await load_grants_map_in_session(
                session, ResourceType.KNOWLEDGE_BASE, [row.id for row in rows]
            )
            return [_base(item, grants=grants_map.get(item.id, ())) for item in rows]

    async def update_base(
        self,
        base_id: str,
        *,
        name: str,
        description: str | None,
        at: datetime,
        visibility_scope: VisibilityScope | None = None,
        owner_department_id: str | None = None,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
    ) -> KnowledgeBase | None:
        """在单事务行锁内更新知识库元数据，仅当提供 visibility_scope 时才变更可见性与授权。"""
        async with self._sessions() as session, session.begin():
            model = await _lock_base(session, base_id)
            if model is None:
                return None
            model.name = name
            model.description = description
            model.updated_at = at
            # 仅改名/改说明时 visibility_scope 为 None：在 FOR UPDATE 行锁内绝不覆写
            # visibility_scope、owner_department_id 与 resource_visibility_grants，
            # 防止旧读快照在并发下覆盖另一管理员刚提交的权限配置。
            if visibility_scope is not None:
                model.visibility_scope = visibility_scope.value
                model.owner_department_id = owner_department_id
            await session.flush()
            if visibility_scope is not None and visibility_grants is not None:
                grants = await sync_resource_grants_in_session(
                    session,
                    ResourceType.KNOWLEDGE_BASE,
                    model.id,
                    visibility_grants,
                    now=at,
                )
            else:
                grants = await _load_base_grants(session, model.id)
            return _base(model, grants=grants)

    async def soft_delete_base(self, owner_id: str, base_id: str, at: datetime) -> bool:
        """在单事务内软删除知识库、清理全部项目绑定及可见性授权并入队清理任务。"""
        async with self._sessions() as session, session.begin():
            item = await _lock_base(session, base_id, owner_id=owner_id, active_only=False)
            if item is None:
                return False
            item.deleted_time = item.deleted_time or at
            item.updated_at = at
            # 删除所有用户项目对该已删知识库的绑定，不再按 owner_id 限定。
            await session.execute(
                delete(ProjectKnowledgeBaseModel).where(
                    ProjectKnowledgeBaseModel.knowledge_base_id == base_id,
                )
            )
            # 同步清理该知识库的所有可见性授权，杜绝残留孤儿记录。
            await delete_resource_grants_in_session(session, ResourceType.KNOWLEDGE_BASE, base_id)
            cleanup = await session.scalar(
                select(KnowledgeJobModel)
                .where(
                    KnowledgeJobModel.kind == "cleanup",
                    KnowledgeJobModel.target_id == base_id,
                    KnowledgeJobModel.state.in_(("queued", "retry", "processing")),
                )
                .with_for_update()
            )
            if cleanup is None:
                session.add(
                    _new_queued_job(
                        job_id=new_id("kjob"),
                        owner_id=owner_id,
                        kind="cleanup",
                        target_id=base_id,
                        at=at,
                    )
                )
            return True
