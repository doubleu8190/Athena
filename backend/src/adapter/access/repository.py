"""统一三层权限引擎 SQLAlchemy 仓储与同事务共享辅助函数。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access.model import ResourceVisibilityGrantModel
from adapter.auth.model import UserModel
from adapter.org.model import DepartmentModel, DeptAdminScopeModel, UserDepartmentModel
from common.ids import new_id
from domain.access import (
    AccessControlRepository,
    GrantTargetType,
    ResourceAccessTarget,
    ResourceType,
    UserAccessSnapshot,
    VisibilityGrant,
    VisibilityGrantInput,
    VisibilityScope,
    evaluate_can_read,
    parse_department_path_ids,
)
from domain.auth import UserRole, UserStatus
from domain.org import DepartmentStatus


def _utc(value: datetime) -> datetime:
    """补齐 SQLite 驱动省略的 UTC 时区信息。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def grant_to_domain(row: ResourceVisibilityGrantModel) -> VisibilityGrant:
    """将授权表 ORM 行转换为不可变领域对象。"""
    return VisibilityGrant(
        id=row.id,
        resource_type=ResourceType(row.resource_type),
        resource_id=row.resource_id,
        target_type=GrantTargetType(row.target_type),
        target_id=row.target_id,
        include_subdepartments=bool(row.include_subdepartments),
        created_at=_utc(row.created_at),
    )


async def load_user_access_snapshot_in_session(
    session: AsyncSession,
    user_id: str,
) -> UserAccessSnapshot | None:
    """在指定事务会话内实时加载用户的角色、启用状态、活跃祖先部门与管辖子树。"""
    user = await session.scalar(
        select(UserModel).where(
            UserModel.id == user_id,
            UserModel.deleted_time.is_(None),
        )
    )
    if user is None:
        return None

    role = UserRole(user.role)
    is_active = user.status == UserStatus.ACTIVE.value
    if not is_active:
        # 停用账号直接返回非活跃快照，跳过后续组织树查询。
        return UserAccessSnapshot(
            user_id=user.id,
            role=role,
            is_active=False,
            direct_department_ids=frozenset(),
            ancestor_department_ids=frozenset(),
            managed_department_ids=frozenset(),
            subtree_managed_department_ids=frozenset(),
        )

    # 仅加载关联 active 部门的用户直属归属，并从物化路径解析全部祖先部门候选。
    dept_rows = (
        await session.execute(
            select(DepartmentModel.id, DepartmentModel.path)
            .join(
                UserDepartmentModel,
                UserDepartmentModel.department_id == DepartmentModel.id,
            )
            .where(
                UserDepartmentModel.user_id == user.id,
                DepartmentModel.status == DepartmentStatus.ACTIVE.value,
            )
        )
    ).all()

    direct_ids = frozenset(row[0] for row in dept_rows)
    candidate_ancestor_ids: set[str] = set(direct_ids)
    for _, path in dept_rows:
        candidate_ancestor_ids.update(parse_department_path_ids(path))

    # 过滤掉路径上已被归档（archived）的上级部门，确保归档部门授权不再误匹配。
    ancestor_ids: frozenset[str] = direct_ids
    extra_ancestors = candidate_ancestor_ids - direct_ids
    if extra_ancestors:
        active_ancestors = set(
            (
                await session.scalars(
                    select(DepartmentModel.id).where(
                        DepartmentModel.id.in_(tuple(extra_ancestors)),
                        DepartmentModel.status == DepartmentStatus.ACTIVE.value,
                    )
                )
            ).all()
        )
        ancestor_ids = direct_ids | active_ancestors

    # 部门管理员按 dept_admin_scopes 实时展开活跃管辖子树；其他角色为空集。
    managed_ids: frozenset[str] = frozenset()
    subtree_managed_ids: frozenset[str] = frozenset()
    if role is UserRole.DEPT_ADMIN:
        scope_rows = (
            await session.execute(
                select(
                    DepartmentModel.id,
                    DepartmentModel.path,
                    DeptAdminScopeModel.include_subdepartments,
                )
                .join(
                    DeptAdminScopeModel,
                    DeptAdminScopeModel.department_id == DepartmentModel.id,
                )
                .where(
                    DeptAdminScopeModel.user_id == user.id,
                    DepartmentModel.status == DepartmentStatus.ACTIVE.value,
                )
            )
        ).all()
        collected_managed: set[str] = set()
        collected_subtree_managed: set[str] = set()
        subtree_paths: list[str] = []
        for dept_id, dept_path, include_sub in scope_rows:
            collected_managed.add(dept_id)
            if include_sub:
                collected_subtree_managed.add(dept_id)
                subtree_paths.append(dept_path)
        if subtree_paths:
            # 将多条子树前缀匹配合并为单条 SQL，防止 N+1 查库。
            prefix_conditions = [
                DepartmentModel.path.startswith(path, autoescape=True) for path in subtree_paths
            ]
            descendant_ids = (
                await session.scalars(
                    select(DepartmentModel.id).where(
                        DepartmentModel.status == DepartmentStatus.ACTIVE.value,
                        or_(*prefix_conditions),
                    )
                )
            ).all()
            collected_managed.update(descendant_ids)
            collected_subtree_managed.update(descendant_ids)
        managed_ids = frozenset(collected_managed)
        subtree_managed_ids = frozenset(collected_subtree_managed)

    return UserAccessSnapshot(
        user_id=user.id,
        role=role,
        is_active=True,
        direct_department_ids=direct_ids,
        ancestor_department_ids=ancestor_ids,
        managed_department_ids=managed_ids,
        subtree_managed_department_ids=subtree_managed_ids,
    )


async def list_active_department_ids_in_session(
    session: AsyncSession,
    department_ids: Sequence[str],
) -> frozenset[str]:
    """在指定事务会话内批量筛选仍为 active 状态的部门 ID。"""
    unique_ids = tuple(dict.fromkeys(department_ids))
    if not unique_ids:
        return frozenset()
    rows = (
        await session.scalars(
            select(DepartmentModel.id).where(
                DepartmentModel.id.in_(unique_ids),
                DepartmentModel.status == DepartmentStatus.ACTIVE.value,
            )
        )
    ).all()
    return frozenset(rows)


async def load_grants_map_in_session(
    session: AsyncSession,
    resource_type: ResourceType,
    resource_ids: Sequence[str],
) -> dict[str, tuple[VisibilityGrant, ...]]:
    """在指定事务会话内批量读取一组资源的可见性授权映射。"""
    unique_ids = tuple(dict.fromkeys(resource_ids))
    if not unique_ids:
        return {}
    rows = (
        await session.scalars(
            select(ResourceVisibilityGrantModel)
            .where(
                ResourceVisibilityGrantModel.resource_type == resource_type.value,
                ResourceVisibilityGrantModel.resource_id.in_(unique_ids),
            )
            .order_by(
                ResourceVisibilityGrantModel.resource_id.asc(),
                ResourceVisibilityGrantModel.target_type.asc(),
                ResourceVisibilityGrantModel.target_id.asc(),
            )
        )
    ).all()
    grouped: dict[str, list[VisibilityGrant]] = {}
    for row in rows:
        grouped.setdefault(row.resource_id, []).append(grant_to_domain(row))
    return {key: tuple(items) for key, items in grouped.items()}


async def sync_resource_grants_in_session(
    session: AsyncSession,
    resource_type: ResourceType,
    resource_id: str,
    grants: Sequence[VisibilityGrantInput],
    *,
    now: datetime,
    id_factory: Callable[[str], str] = new_id,
) -> tuple[VisibilityGrant, ...]:
    """在当前资源写事务内原子清空并重建指定资源的可见性授权。"""
    await session.execute(
        delete(ResourceVisibilityGrantModel).where(
            ResourceVisibilityGrantModel.resource_type == resource_type.value,
            ResourceVisibilityGrantModel.resource_id == resource_id,
        )
    )
    created: list[ResourceVisibilityGrantModel] = []
    seen: set[tuple[str, str]] = set()
    for item in grants:
        key = (item.target_type.value, item.target_id)
        if key in seen:
            continue
        seen.add(key)
        model = ResourceVisibilityGrantModel(
            id=id_factory("rvg"),
            resource_type=resource_type.value,
            resource_id=resource_id,
            target_type=item.target_type.value,
            target_id=item.target_id,
            include_subdepartments=(
                bool(item.include_subdepartments)
                if item.target_type is GrantTargetType.DEPARTMENT
                else False
            ),
            created_at=now,
        )
        session.add(model)
        created.append(model)
    await session.flush()
    created.sort(key=lambda row: (row.target_type, row.target_id))
    return tuple(grant_to_domain(row) for row in created)


async def delete_resource_grants_in_session(
    session: AsyncSession,
    resource_type: ResourceType,
    resource_id: str,
) -> None:
    """在资源删除事务内同步清除所有关联的可见性授权，防止残留孤儿授权。"""
    await session.execute(
        delete(ResourceVisibilityGrantModel).where(
            ResourceVisibilityGrantModel.resource_type == resource_type.value,
            ResourceVisibilityGrantModel.resource_id == resource_id,
        )
    )


async def filter_readable_targets_in_session(
    session: AsyncSession,
    user_id: str,
    targets: Sequence[ResourceAccessTarget],
) -> set[str]:
    """在同一数据库事务快照内批量过滤当前用户可读的资源 ID（供 Run 快照等原子路径复用）。"""
    if not targets:
        return set()
    snapshot = await load_user_access_snapshot_in_session(session, user_id)
    if snapshot is None or not snapshot.is_active:
        return set()

    resource_type = targets[0].resource_type
    dept_resource_ids = [
        target.resource_id
        for target in targets
        if target.visibility_scope is VisibilityScope.DEPARTMENT
    ]
    grants_by_id: dict[str, tuple[VisibilityGrant, ...]] = {}
    active_grant_depts: frozenset[str] = frozenset()
    if dept_resource_ids and snapshot.role is not UserRole.SUPER_ADMIN:
        grants_by_id = await load_grants_map_in_session(session, resource_type, dept_resource_ids)
        dept_target_ids = tuple(
            dict.fromkeys(
                grant.target_id
                for grants in grants_by_id.values()
                for grant in grants
                if grant.target_type is GrantTargetType.DEPARTMENT
            )
        )
        if dept_target_ids:
            active_grant_depts = await list_active_department_ids_in_session(
                session, dept_target_ids
            )

    return {
        target.resource_id
        for target in targets
        if evaluate_can_read(
            snapshot,
            target,
            grants_by_id.get(target.resource_id, ()),
            active_grant_department_ids=active_grant_depts,
        )
    }


class SqlAlchemyAccessControlRepository(AccessControlRepository):
    """基于 async SQLAlchemy 实时读取组织权限树与资源授权表的仓储实现。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        """保存异步数据库会话工厂与授权 ID 生成器。"""
        self._session_factory = session_factory
        self._id_factory = id_factory

    async def get_user_access_snapshot(self, user_id: str) -> UserAccessSnapshot | None:
        """实时读取用户的角色、状态、活跃祖先部门及管辖子树。"""
        async with self._session_factory() as session:
            return await load_user_access_snapshot_in_session(session, user_id)

    async def list_active_department_ids(self, department_ids: Sequence[str]) -> frozenset[str]:
        """批量检查给定部门 ID 中当前为 active 状态的集合。"""
        async with self._session_factory() as session:
            return await list_active_department_ids_in_session(session, department_ids)

    async def get_resource_target(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> ResourceAccessTarget | None:
        """读取单个未删除资源的权限判定元数据。"""
        targets = await self.list_resource_targets(resource_type, (resource_id,))
        return targets[0] if targets else None

    async def list_resource_targets(
        self,
        resource_type: ResourceType,
        resource_ids: Sequence[str] | None = None,
    ) -> tuple[ResourceAccessTarget, ...]:
        """批量读取指定类型的未删除资源归属元数据。"""
        unique_ids: tuple[str, ...] | None = None
        if resource_ids is not None:
            unique_ids = tuple(dict.fromkeys(resource_ids))
            if not unique_ids:
                return ()

        # 按需延迟导入四条业务线 ORM 模型，避免与各业务仓储复用本模块同事务辅助函数时产生循环导入。
        async with self._session_factory() as session:
            if resource_type is ResourceType.KNOWLEDGE_BASE:
                from adapter.knowledge.model import KnowledgeBaseModel

                kb_stmt = select(
                    KnowledgeBaseModel.id,
                    KnowledgeBaseModel.visibility_scope,
                    KnowledgeBaseModel.owner_id,
                    KnowledgeBaseModel.owner_department_id,
                ).where(KnowledgeBaseModel.deleted_time.is_(None))
                if unique_ids is not None:
                    kb_stmt = kb_stmt.where(KnowledgeBaseModel.id.in_(unique_ids))
                kb_rows = (await session.execute(kb_stmt)).all()
                return tuple(
                    ResourceAccessTarget(
                        resource_type=ResourceType.KNOWLEDGE_BASE,
                        resource_id=row[0],
                        visibility_scope=VisibilityScope(row[1]),
                        owner_id=row[2],
                        owner_department_id=row[3],
                    )
                    for row in kb_rows
                )

            if resource_type is ResourceType.SKILL:
                from adapter.skill.model import SkillModel

                skill_stmt = select(
                    SkillModel.id,
                    SkillModel.scope,
                    SkillModel.owner_id,
                    SkillModel.owner_department_id,
                ).where(SkillModel.deleted_at.is_(None))
                if unique_ids is not None:
                    skill_stmt = skill_stmt.where(SkillModel.id.in_(unique_ids))
                skill_rows = (await session.execute(skill_stmt)).all()
                return tuple(
                    ResourceAccessTarget(
                        resource_type=ResourceType.SKILL,
                        resource_id=row[0],
                        visibility_scope=VisibilityScope(row[1]),
                        owner_id=row[2],
                        owner_department_id=row[3],
                    )
                    for row in skill_rows
                )

            if resource_type is ResourceType.MCP_SERVER:
                from adapter.mcp.model import McpServerModel

                mcp_stmt = select(
                    McpServerModel.id,
                    McpServerModel.visibility_scope,
                    McpServerModel.owner_department_id,
                )
                if unique_ids is not None:
                    mcp_stmt = mcp_stmt.where(McpServerModel.id.in_(unique_ids))
                mcp_rows = (await session.execute(mcp_stmt)).all()
                return tuple(
                    ResourceAccessTarget(
                        resource_type=ResourceType.MCP_SERVER,
                        resource_id=row[0],
                        visibility_scope=VisibilityScope(row[1]),
                        owner_id=None,
                        owner_department_id=row[2],
                    )
                    for row in mcp_rows
                )

            if resource_type is ResourceType.SUBAGENT:
                from adapter.subagent.model import SubagentModel

                subagent_stmt = select(
                    SubagentModel.id,
                    SubagentModel.visibility_scope,
                    SubagentModel.owner_department_id,
                ).where(SubagentModel.deleted_at.is_(None))
                if unique_ids is not None:
                    subagent_stmt = subagent_stmt.where(SubagentModel.id.in_(unique_ids))
                subagent_rows = (await session.execute(subagent_stmt)).all()
                return tuple(
                    ResourceAccessTarget(
                        resource_type=ResourceType.SUBAGENT,
                        resource_id=row[0],
                        visibility_scope=VisibilityScope(row[1]),
                        owner_id=None,
                        owner_department_id=row[2],
                    )
                    for row in subagent_rows
                )

        return ()

    async def list_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> tuple[VisibilityGrant, ...]:
        """读取单个资源的全部可见性授权。"""
        async with self._session_factory() as session:
            grouped = await load_grants_map_in_session(session, resource_type, (resource_id,))
        return grouped.get(resource_id, ())

    async def list_grants_for_resources(
        self,
        resource_type: ResourceType,
        resource_ids: Sequence[str],
    ) -> dict[str, tuple[VisibilityGrant, ...]]:
        """批量读取多个资源的可见性授权。"""
        async with self._session_factory() as session:
            return await load_grants_map_in_session(session, resource_type, resource_ids)

    async def replace_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
        grants: Sequence[VisibilityGrantInput],
        *,
        now: datetime,
    ) -> tuple[VisibilityGrant, ...]:
        """独立短事务内原子替换资源可见性授权。"""
        async with self._session_factory() as session, session.begin():
            return await sync_resource_grants_in_session(
                session,
                resource_type,
                resource_id,
                grants,
                now=now,
                id_factory=self._id_factory,
            )

    async def delete_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> None:
        """独立短事务内删除指定资源的可见性授权。"""
        async with self._session_factory() as session, session.begin():
            await delete_resource_grants_in_session(session, resource_type, resource_id)


__all__ = [
    "SqlAlchemyAccessControlRepository",
    "delete_resource_grants_in_session",
    "filter_readable_targets_in_session",
    "grant_to_domain",
    "list_active_department_ids_in_session",
    "load_grants_map_in_session",
    "load_user_access_snapshot_in_session",
    "sync_resource_grants_in_session",
]
