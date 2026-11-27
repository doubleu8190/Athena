"""统一三层权限引擎 SQLAlchemy 仓储与同事务共享辅助函数。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from sqlalchemy import ColumnElement, and_, delete, exists, false, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import SQLColumnExpression

from adapter.access.model import ResourceVisibilityGrantModel
from adapter.auth.model import UserModel
from adapter.org.model import DepartmentModel, DeptAdminScopeModel, UserDepartmentModel
from common.date import as_utc
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
    evaluate_can_manage,
    parse_department_path_ids,
)
from domain.auth import UserRole, UserStatus
from domain.org import DepartmentStatus


@dataclass(frozen=True, slots=True)
class _ResourceTableDescriptor:
    """映射 ResourceType 到四类业务资源表的权限列，消除多处 3×4 分支复制。"""

    id_col: SQLColumnExpression[str]
    deleted_col: SQLColumnExpression[datetime | None]
    scope_col: SQLColumnExpression[str]
    owner_id_col: SQLColumnExpression[str] | SQLColumnExpression[str | None] | None
    owner_department_id_col: SQLColumnExpression[str] | SQLColumnExpression[str | None]


@lru_cache(maxsize=8)
def _get_resource_descriptor(resource_type: ResourceType) -> _ResourceTableDescriptor | None:
    """按需延迟导入资源 ORM 模型并返回只读列描述符，避免业务仓储反向导入产生循环依赖。"""
    if resource_type is ResourceType.KNOWLEDGE_BASE:
        from adapter.knowledge.model import KnowledgeBaseModel

        return _ResourceTableDescriptor(
            id_col=KnowledgeBaseModel.id,
            deleted_col=KnowledgeBaseModel.deleted_time,
            scope_col=KnowledgeBaseModel.visibility_scope,
            owner_id_col=KnowledgeBaseModel.owner_id,
            owner_department_id_col=KnowledgeBaseModel.owner_department_id,
        )
    if resource_type is ResourceType.SKILL:
        from adapter.skill.model import SkillModel

        return _ResourceTableDescriptor(
            id_col=SkillModel.id,
            deleted_col=SkillModel.deleted_at,
            scope_col=SkillModel.scope,
            owner_id_col=SkillModel.owner_id,
            owner_department_id_col=SkillModel.owner_department_id,
        )
    if resource_type is ResourceType.MCP_SERVER:
        from adapter.mcp.model import McpServerModel

        return _ResourceTableDescriptor(
            id_col=McpServerModel.id,
            deleted_col=McpServerModel.deleted_at,
            scope_col=McpServerModel.visibility_scope,
            owner_id_col=None,
            owner_department_id_col=McpServerModel.owner_department_id,
        )
    if resource_type is ResourceType.SUBAGENT:
        from adapter.subagent.model import SubagentModel

        return _ResourceTableDescriptor(
            id_col=SubagentModel.id,
            deleted_col=SubagentModel.deleted_at,
            scope_col=SubagentModel.visibility_scope,
            owner_id_col=None,
            owner_department_id_col=SubagentModel.owner_department_id,
        )
    return None


def grant_to_domain(row: ResourceVisibilityGrantModel) -> VisibilityGrant:
    """将授权表 ORM 行转换为不可变领域对象。"""
    return VisibilityGrant(
        id=row.id,
        resource_type=ResourceType(row.resource_type),
        resource_id=row.resource_id,
        target_type=GrantTargetType(row.target_type),
        target_id=row.target_id,
        include_subdepartments=bool(row.include_subdepartments),
        created_at=as_utc(row.created_at),
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


def _build_grant_match_clause(snapshot: UserAccessSnapshot) -> ColumnElement[bool]:
    """构造单条 resource_visibility_grants 命中活跃用户快照的 SQL 匹配条件。

    权限等价性说明：
    - GrantTargetType 仅有 department 与 role 两类；
    - load_user_access_snapshot_in_session 在构造 direct_department_ids 与
      ancestor_department_ids 时已强制过滤 departments.status == 'active'，
      故此处无需二次 JOIN departments，与 evaluate_can_read 规则 100% 等价；
    - 空集合严禁生成 .in_(())，仅对非空部门集追加对应分支。
    """
    clauses: list[ColumnElement[bool]] = [
        and_(
            ResourceVisibilityGrantModel.target_type == GrantTargetType.ROLE.value,
            ResourceVisibilityGrantModel.target_id == snapshot.role.value,
        )
    ]
    if snapshot.ancestor_department_ids:
        clauses.append(
            and_(
                ResourceVisibilityGrantModel.target_type == GrantTargetType.DEPARTMENT.value,
                ResourceVisibilityGrantModel.include_subdepartments.is_(True),
                ResourceVisibilityGrantModel.target_id.in_(
                    tuple(sorted(snapshot.ancestor_department_ids))
                ),
            )
        )
    if snapshot.direct_department_ids:
        clauses.append(
            and_(
                ResourceVisibilityGrantModel.target_type == GrantTargetType.DEPARTMENT.value,
                ResourceVisibilityGrantModel.include_subdepartments.is_(False),
                ResourceVisibilityGrantModel.target_id.in_(
                    tuple(sorted(snapshot.direct_department_ids))
                ),
            )
        )
    return or_(*clauses)


def build_readable_resource_clause(
    snapshot: UserAccessSnapshot | None,
    resource_type: ResourceType,
    *,
    id_col: SQLColumnExpression[str],
    scope_col: SQLColumnExpression[str],
    owner_id_col: SQLColumnExpression[str] | SQLColumnExpression[str | None] | None = None,
    owner_department_id_col: SQLColumnExpression[str] | SQLColumnExpression[str | None],
) -> ColumnElement[bool]:
    """生成与 domain.access.evaluate_can_read 规则 100% 等价的 PostgreSQL WHERE 可读谓词。

    覆盖四类资产列名差异：
    - KnowledgeBaseModel：visibility_scope / owner_id / owner_department_id；
    - SkillModel：scope / owner_id / owner_department_id；
    - McpServerModel / SubagentModel：visibility_scope / 无 owner_id / owner_department_id。
    """
    # 停用或不存在用户一律保守拒绝。
    if snapshot is None or not snapshot.is_active:
        return false()

    branches: list[ColumnElement[bool]] = [
        scope_col == VisibilityScope.COMPANY.value,
    ]
    # 仅支持个人私有层级的资源表追加 personal 匹配，超管同样不得越权读他人私有资产。
    if owner_id_col is not None:
        branches.append(
            and_(
                scope_col == VisibilityScope.PERSONAL.value,
                owner_id_col == snapshot.user_id,
            )
        )

    # department 层级：超管直接放行无 EXISTS；非超管按管辖部门或授权表 EXISTS 放行。
    if snapshot.role is UserRole.SUPER_ADMIN:
        branches.append(scope_col == VisibilityScope.DEPARTMENT.value)
    else:
        dept_conditions: list[ColumnElement[bool]] = []
        if snapshot.role is UserRole.DEPT_ADMIN and snapshot.managed_department_ids:
            dept_conditions.append(
                owner_department_id_col.in_(tuple(sorted(snapshot.managed_department_ids)))
            )
        # 命中 uq_resource_visibility_grant(resource_type, resource_id, ...) 最左前缀索引。
        dept_conditions.append(
            exists(
                select(1).where(
                    ResourceVisibilityGrantModel.resource_type == resource_type.value,
                    ResourceVisibilityGrantModel.resource_id == id_col,
                    _build_grant_match_clause(snapshot),
                )
            )
        )
        branches.append(
            and_(
                scope_col == VisibilityScope.DEPARTMENT.value,
                or_(*dept_conditions),
            )
        )
    return or_(*branches)


def _fast_can_read_target(
    snapshot: UserAccessSnapshot,
    target: ResourceAccessTarget,
) -> bool | None:
    """在不查询授权表的前提下做快速路径判定；仅 department 需查 grants 时返回 None。"""
    if not snapshot.is_active:
        return False
    if target.visibility_scope is VisibilityScope.PERSONAL:
        return target.owner_id is not None and target.owner_id == snapshot.user_id
    if target.visibility_scope is VisibilityScope.COMPANY:
        return True
    if target.visibility_scope is not VisibilityScope.DEPARTMENT:
        return False
    if snapshot.role is UserRole.SUPER_ADMIN:
        return True
    if (
        snapshot.role is UserRole.DEPT_ADMIN
        and target.owner_department_id is not None
        and target.owner_department_id in snapshot.managed_department_ids
    ):
        return True
    return None


async def _has_matching_grant_in_session(
    session: AsyncSession,
    snapshot: UserAccessSnapshot,
    resource_type: ResourceType,
    resource_id: str,
) -> bool:
    """在当前只读会话内通过唯一索引最左前缀判定单资源是否命中可见性授权。"""
    matched = await session.scalar(
        select(ResourceVisibilityGrantModel.id)
        .where(
            ResourceVisibilityGrantModel.resource_type == resource_type.value,
            ResourceVisibilityGrantModel.resource_id == resource_id,
            _build_grant_match_clause(snapshot),
        )
        .limit(1)
    )
    return matched is not None


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
        async with self._session_factory() as session:
            return await self._select_resource_targets_in_session(
                session,
                resource_type,
                unique_ids,
            )

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

    async def filter_readable_targets(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_ids: Sequence[str] | None = None,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> tuple[ResourceAccessTarget, ...]:
        """单 Session 内加载/复用用户权限快照并通过 SQL EXISTS 下推过滤可读目标元数据。"""
        unique_ids: tuple[str, ...] | None = None
        if resource_ids is not None:
            unique_ids = tuple(dict.fromkeys(resource_ids))
            if not unique_ids:
                return ()
        normalized_user_id = user_id.strip()
        if snapshot is None and not normalized_user_id:
            return ()
        if snapshot is not None and not snapshot.is_active:
            return ()

        async with self._session_factory() as session:
            resolved = (
                snapshot
                if snapshot is not None
                else await load_user_access_snapshot_in_session(session, normalized_user_id)
            )
            if resolved is None or not resolved.is_active:
                return ()
            return await self._query_readable_targets_in_session(
                session,
                resolved,
                resource_type,
                unique_ids,
            )

    async def can_read_resource(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> bool:
        """单 Session 内加载/复用用户权限快照并通过 SQL EXISTS 实时判定单个资源读权限。"""
        normalized_user_id = user_id.strip()
        if snapshot is None and not normalized_user_id:
            return False
        if snapshot is not None and not snapshot.is_active:
            return False

        async with self._session_factory() as session:
            resolved = (
                snapshot
                if snapshot is not None
                else await load_user_access_snapshot_in_session(session, normalized_user_id)
            )
            if resolved is None or not resolved.is_active:
                return False
            return await self._exists_readable_resource_in_session(
                session,
                resolved,
                resource_type,
                resource_id,
            )

    async def can_read_target(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> bool:
        """已知资源归属元数据时单 Session（命中快速路径时零 Session）判定读权限。"""
        can_read, _ = await self._evaluate_target_in_session(
            user_id,
            target,
            snapshot=snapshot,
            compute_manage=False,
        )
        return can_read

    async def evaluate_target_access(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> tuple[bool, bool]:
        """在单次快照与单 Session 内联合返回目标资源的 (可读, 可管) 双判定结果。"""
        return await self._evaluate_target_in_session(
            user_id,
            target,
            snapshot=snapshot,
            compute_manage=True,
        )

    async def _evaluate_target_in_session(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None,
        compute_manage: bool,
    ) -> tuple[bool, bool]:
        """合并 snapshot 复用与单 Session 实时加载分支，保留零 Session 快速路径。"""
        normalized_user_id = user_id.strip()
        if snapshot is None and not normalized_user_id:
            return False, False

        def _evaluate_fast(resolved: UserAccessSnapshot) -> tuple[bool | None, bool]:
            can_manage = (
                evaluate_can_manage(
                    resolved,
                    target.resource_type,
                    target.visibility_scope,
                    owner_id=target.owner_id,
                    owner_department_id=target.owner_department_id,
                )
                if compute_manage
                else False
            )
            return _fast_can_read_target(resolved, target), can_manage

        if snapshot is not None:
            if not snapshot.is_active:
                return False, False
            fast, can_manage = _evaluate_fast(snapshot)
            if fast is not None:
                return fast, can_manage
            async with self._session_factory() as session:
                can_read = await _has_matching_grant_in_session(
                    session,
                    snapshot,
                    target.resource_type,
                    target.resource_id,
                )
            return can_read, can_manage

        async with self._session_factory() as session:
            resolved = await load_user_access_snapshot_in_session(session, normalized_user_id)
            if resolved is None or not resolved.is_active:
                return False, False
            fast, can_manage = _evaluate_fast(resolved)
            if fast is not None:
                return fast, can_manage
            can_read = await _has_matching_grant_in_session(
                session,
                resolved,
                target.resource_type,
                target.resource_id,
            )
            return can_read, can_manage

    @staticmethod
    async def _select_resource_targets_in_session(
        session: AsyncSession,
        resource_type: ResourceType,
        unique_ids: tuple[str, ...] | None,
        *,
        readable_snapshot: UserAccessSnapshot | None = None,
    ) -> tuple[ResourceAccessTarget, ...]:
        """按资源描述符表统一构造未删资源目标查询，可选叠加 SQL 可读谓词下推。"""
        descriptor = _get_resource_descriptor(resource_type)
        if descriptor is None:
            return ()
        owner_expr: SQLColumnExpression[str | None] = (
            descriptor.owner_id_col if descriptor.owner_id_col is not None else literal(None)
        )
        stmt = select(
            descriptor.id_col,
            descriptor.scope_col,
            owner_expr,
            descriptor.owner_department_id_col,
        ).where(descriptor.deleted_col.is_(None))
        if readable_snapshot is not None:
            stmt = stmt.where(
                build_readable_resource_clause(
                    readable_snapshot,
                    resource_type,
                    id_col=descriptor.id_col,
                    scope_col=descriptor.scope_col,
                    owner_id_col=descriptor.owner_id_col,
                    owner_department_id_col=descriptor.owner_department_id_col,
                )
            )
        if unique_ids is not None:
            stmt = stmt.where(descriptor.id_col.in_(unique_ids))
        rows = (await session.execute(stmt)).all()
        return tuple(
            ResourceAccessTarget(
                resource_type=resource_type,
                resource_id=row[0],
                visibility_scope=VisibilityScope(row[1]),
                owner_id=row[2],
                owner_department_id=row[3],
            )
            for row in rows
        )

    @classmethod
    async def _query_readable_targets_in_session(
        cls,
        session: AsyncSession,
        snapshot: UserAccessSnapshot,
        resource_type: ResourceType,
        unique_ids: tuple[str, ...] | None,
    ) -> tuple[ResourceAccessTarget, ...]:
        """在给定会话内按资源描述符执行 SQL 下推可读过滤。"""
        return await cls._select_resource_targets_in_session(
            session,
            resource_type,
            unique_ids,
            readable_snapshot=snapshot,
        )

    @staticmethod
    async def _exists_readable_resource_in_session(
        session: AsyncSession,
        snapshot: UserAccessSnapshot,
        resource_type: ResourceType,
        resource_id: str,
    ) -> bool:
        """单条 LIMIT 1 存在性查询判定指定资源未删除且对当前用户快照可读。"""
        descriptor = _get_resource_descriptor(resource_type)
        if descriptor is None:
            return False
        found = await session.scalar(
            select(descriptor.id_col)
            .where(
                descriptor.id_col == resource_id,
                descriptor.deleted_col.is_(None),
                build_readable_resource_clause(
                    snapshot,
                    resource_type,
                    id_col=descriptor.id_col,
                    scope_col=descriptor.scope_col,
                    owner_id_col=descriptor.owner_id_col,
                    owner_department_id_col=descriptor.owner_department_id_col,
                ),
            )
            .limit(1)
        )
        return found is not None


__all__ = [
    "SqlAlchemyAccessControlRepository",
    "build_readable_resource_clause",
    "delete_resource_grants_in_session",
    "grant_to_domain",
    "list_active_department_ids_in_session",
    "load_grants_map_in_session",
    "load_user_access_snapshot_in_session",
    "sync_resource_grants_in_session",
]
