"""Subagent 配置 SQLAlchemy 仓储与模型可用性检查实现。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access.repository import (
    build_readable_resource_clause,
    delete_resource_grants_in_session,
    load_grants_map_in_session,
    sync_resource_grants_in_session,
)
from adapter.auth.repository.transaction_guard import verify_active_session_user
from adapter.llm.model import ModelProfileVersionModel
from adapter.subagent.converter import subagent_to_domain
from adapter.subagent.model import SubagentModel
from domain.access import ResourceType, UserAccessSnapshot, VisibilityGrantInput, VisibilityScope
from domain.auth import AuthContext, UserRole
from domain.subagent import (
    SubagentAdminRequiredError,
    SubagentInvalidSessionError,
    SubagentModelChecker,
    SubagentNotFoundError,
    SubagentPage,
    SubagentProfile,
    SubagentRepository,
    SubagentSlugConflictError,
    SubagentUpdatePatch,
    SubagentVersionConflictError,
)


class SqlAlchemySubagentRepository(SubagentRepository):
    """在单事务内复核管理员身份并维护 Subagent 配置。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存异步数据库会话工厂。"""
        self._session_factory = session_factory

    async def create(
        self,
        actor: AuthContext,
        profile: SubagentProfile,
        *,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> SubagentProfile:
        """在事务内复核管理员身份后写入新 Subagent 与可见性授权，冲突时映射为领域异常。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    row = SubagentModel(
                        id=profile.id,
                        slug=profile.slug,
                        name=profile.name,
                        description=profile.description,
                        system_prompt=profile.system_prompt,
                        model_profile_id=profile.model_profile_id,
                        allowed_tool_ids=list(profile.allowed_tool_ids),
                        enabled=profile.enabled,
                        visibility_scope=profile.visibility_scope.value,
                        owner_department_id=profile.owner_department_id,
                        max_llm_requests=profile.max_llm_requests,
                        timeout_seconds=profile.timeout_seconds,
                        version=profile.version,
                        created_by=profile.created_by,
                        deleted_at=profile.deleted_at,
                        created_at=profile.created_at,
                        updated_at=profile.updated_at,
                    )
                    session.add(row)
                    await session.flush()
                    grants = await sync_resource_grants_in_session(
                        session,
                        ResourceType.SUBAGENT,
                        row.id,
                        (
                            visibility_grants
                            if profile.visibility_scope is VisibilityScope.DEPARTMENT
                            else ()
                        ),
                        now=profile.created_at,
                    )
                    return subagent_to_domain(row, grants=grants)
        except IntegrityError as error:
            if _is_slug_conflict(error):
                raise SubagentSlugConflictError() from error
            raise

    async def get(self, subagent_id: str) -> SubagentProfile | None:
        """按 ID 读取未软删除的 Subagent 配置（含可见性授权）。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SubagentModel).where(
                    SubagentModel.id == subagent_id,
                    SubagentModel.deleted_at.is_(None),
                )
            )
            if row is None:
                return None
            grants_map = await load_grants_map_in_session(session, ResourceType.SUBAGENT, (row.id,))
            return subagent_to_domain(row, grants=grants_map.get(row.id, ()))

    async def get_by_slug(self, slug: str) -> SubagentProfile | None:
        """按 slug 读取未软删除的 Subagent 配置（含可见性授权）。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SubagentModel).where(
                    SubagentModel.slug == slug,
                    SubagentModel.deleted_at.is_(None),
                )
            )
            if row is None:
                return None
            grants_map = await load_grants_map_in_session(session, ResourceType.SUBAGENT, (row.id,))
            return subagent_to_domain(row, grants=grants_map.get(row.id, ()))

    async def list_page(
        self,
        *,
        offset: int,
        limit: int,
        enabled: bool | None = None,
    ) -> SubagentPage:
        """分页读取未软删除的 Subagent 列表，支持按启停状态过滤。"""
        filters = [SubagentModel.deleted_at.is_(None)]
        if enabled is not None:
            filters.append(SubagentModel.enabled.is_(enabled))
        async with self._session_factory() as session:
            total = await session.scalar(select(func.count(SubagentModel.id)).where(*filters)) or 0
            rows = list(
                (
                    await session.scalars(
                        select(SubagentModel)
                        .where(*filters)
                        .order_by(SubagentModel.created_at.desc(), SubagentModel.id.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
            grants_map = await load_grants_map_in_session(
                session, ResourceType.SUBAGENT, [row.id for row in rows]
            )
        return SubagentPage(
            items=tuple(subagent_to_domain(row, grants=grants_map.get(row.id, ())) for row in rows),
            total=int(total),
            offset=offset,
            limit=limit,
        )

    async def list_readable_enabled(
        self,
        snapshot: UserAccessSnapshot | None,
    ) -> tuple[SubagentProfile, ...]:
        """单 Session 以 SQL EXISTS 下推读取当前用户快照可读且已启用的 Subagent 列表。"""
        if snapshot is None or not snapshot.is_active:
            return ()
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(SubagentModel)
                        .where(
                            SubagentModel.deleted_at.is_(None),
                            SubagentModel.enabled.is_(True),
                            build_readable_resource_clause(
                                snapshot,
                                ResourceType.SUBAGENT,
                                id_col=SubagentModel.id,
                                scope_col=SubagentModel.visibility_scope,
                                owner_id_col=None,
                                owner_department_id_col=SubagentModel.owner_department_id,
                            ),
                        )
                        .order_by(SubagentModel.slug.asc(), SubagentModel.id.asc())
                    )
                ).all()
            )
            if not rows:
                return ()
            grants_map = await load_grants_map_in_session(
                session, ResourceType.SUBAGENT, [row.id for row in rows]
            )
        return tuple(subagent_to_domain(row, grants=grants_map.get(row.id, ())) for row in rows)

    async def update(
        self,
        actor: AuthContext,
        subagent_id: str,
        *,
        expected_version: int,
        patch: SubagentUpdatePatch,
    ) -> SubagentProfile:
        """在事务内复核管理员身份，锁定目标行并校验乐观锁版本后更新。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_admin(session, actor)
                    # 先锁定未软删除的目标行，避免并发编辑出现覆盖或与软删除竞态。
                    row = await session.scalar(
                        select(SubagentModel)
                        .where(
                            SubagentModel.id == subagent_id,
                            SubagentModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if row is None:
                        raise SubagentNotFoundError()
                    if row.version != expected_version:
                        raise SubagentVersionConflictError()
                    row.slug = patch.slug
                    row.name = patch.name
                    row.description = patch.description
                    row.system_prompt = patch.system_prompt
                    row.model_profile_id = patch.model_profile_id
                    row.allowed_tool_ids = list(patch.allowed_tool_ids)
                    row.visibility_scope = patch.visibility_scope.value
                    row.owner_department_id = patch.owner_department_id
                    row.max_llm_requests = patch.max_llm_requests
                    row.timeout_seconds = patch.timeout_seconds
                    row.version = row.version + 1
                    row.updated_at = patch.updated_at
                    await session.flush()
                    if patch.visibility_grants is not None:
                        grants = await sync_resource_grants_in_session(
                            session,
                            ResourceType.SUBAGENT,
                            row.id,
                            (
                                patch.visibility_grants
                                if patch.visibility_scope is VisibilityScope.DEPARTMENT
                                else ()
                            ),
                            now=patch.updated_at,
                        )
                    else:
                        grants_map = await load_grants_map_in_session(
                            session, ResourceType.SUBAGENT, (row.id,)
                        )
                        grants = grants_map.get(row.id, ())
                    return subagent_to_domain(row, grants=grants)
        except IntegrityError as error:
            if _is_slug_conflict(error):
                raise SubagentSlugConflictError() from error
            raise

    async def set_enabled(
        self,
        actor: AuthContext,
        subagent_id: str,
        enabled: bool,
        *,
        updated_at: datetime,
    ) -> SubagentProfile:
        """在事务内复核管理员身份并切换启停状态。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                row = await session.scalar(
                    select(SubagentModel)
                    .where(
                        SubagentModel.id == subagent_id,
                        SubagentModel.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise SubagentNotFoundError()
                if row.enabled != enabled:
                    row.enabled = enabled
                    row.version = row.version + 1
                    row.updated_at = updated_at
                    await session.flush()
                grants_map = await load_grants_map_in_session(
                    session, ResourceType.SUBAGENT, (row.id,)
                )
                return subagent_to_domain(row, grants=grants_map.get(row.id, ()))

    async def soft_delete(
        self,
        actor: AuthContext,
        subagent_id: str,
        *,
        deleted_at: datetime,
    ) -> None:
        """在事务内复核管理员身份并执行幂等软删除，同时清理关联可见性授权。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                row = await session.scalar(
                    select(SubagentModel).where(SubagentModel.id == subagent_id).with_for_update()
                )
                if row is None or row.deleted_at is not None:
                    # 幂等语义：目标不存在或已软删除时直接成功返回。
                    return
                row.deleted_at = deleted_at
                row.updated_at = deleted_at
                await delete_resource_grants_in_session(session, ResourceType.SUBAGENT, row.id)
                await session.flush()

    @staticmethod
    async def _require_admin(session: AsyncSession, actor: AuthContext) -> None:
        """在资源写事务内以无锁单条 JOIN 复核会话有效性及管理员角色。"""
        user = await verify_active_session_user(session, actor.user.id, actor.session_id)
        if user is None:
            raise SubagentInvalidSessionError()
        if user.role is not UserRole.SUPER_ADMIN:
            raise SubagentAdminRequiredError()


class SqlAlchemySubagentModelChecker(SubagentModelChecker):
    """直接查询模型配置表校验指定模型版本是否存在且启用。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存异步数据库会话工厂。"""
        self._session_factory = session_factory

    async def is_model_available(self, model_profile_id: str) -> bool:
        """仅当模型配置存在且 enabled=True 时返回 True。"""
        async with self._session_factory() as session:
            found_id = await session.scalar(
                select(ModelProfileVersionModel.id).where(
                    ModelProfileVersionModel.id == model_profile_id,
                    ModelProfileVersionModel.enabled.is_(True),
                    ModelProfileVersionModel.deleted_at.is_(None),
                )
            )
        return found_id is not None


def _is_slug_conflict(error: IntegrityError) -> bool:
    """识别 PostgreSQL 下 subagents.slug 部分唯一索引（uq_subagents_slug_active）冲突。"""
    orig = error.orig
    # 优先读取 PostgreSQL 驱动原生的 constraint_name，未提供时回退匹配异常文本中的约束名。
    constraint_name = getattr(orig, "constraint_name", None) or getattr(
        getattr(orig, "__cause__", None),
        "constraint_name",
        None,
    )
    if isinstance(constraint_name, str) and constraint_name:
        return constraint_name.lower() == "uq_subagents_slug_active"
    return "uq_subagents_slug_active" in str(orig or error).lower()


__all__ = ["SqlAlchemySubagentModelChecker", "SqlAlchemySubagentRepository"]
