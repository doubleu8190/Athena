"""统一三层权限引擎应用服务：实时鉴权、可读资源过滤与可见性配置校验。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from common.ids import now_utc
from domain.access import (
    PERSONAL_CAPABLE_RESOURCE_TYPES,
    AccessControlRepository,
    AccessPermissionDeniedError,
    GrantTargetType,
    InvalidVisibilityConfigError,
    ResourceAccessTarget,
    ResourceType,
    UserAccessSnapshot,
    VisibilityGrant,
    VisibilityGrantInput,
    VisibilityScope,
    evaluate_can_manage,
)
from domain.auth import UserRole

_VALID_ROLE_VALUES: frozenset[str] = frozenset(role.value for role in UserRole)


class AccessPolicyService:
    """基于数据库实时组织树与资源授权表提供统一读写鉴权与配置校验。"""

    def __init__(
        self,
        repository: AccessControlRepository,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存权限引擎仓储端口与时钟来源。"""
        self._repository = repository
        self._clock = clock

    async def get_user_snapshot(self, user_id: str) -> UserAccessSnapshot | None:
        """实时读取用户的角色、启用状态、活跃祖先部门与管辖子树。"""
        normalized = user_id.strip()
        if not normalized:
            return None
        return await self._repository.get_user_access_snapshot(normalized)

    async def can_read_resource(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> bool:
        """实时判定用户是否对指定 ID 的未删除资源拥有读权限（支持复用已有权限快照）。"""
        normalized = user_id.strip()
        if snapshot is None and not normalized:
            return False
        if snapshot is not None and not snapshot.is_active:
            return False
        return await self._repository.can_read_resource(
            normalized or (snapshot.user_id if snapshot is not None else ""),
            resource_type,
            resource_id,
            snapshot=snapshot,
        )

    async def can_read_target(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> bool:
        """已知资源归属元数据时实时判定读权限（支持复用已有权限快照）。"""
        normalized = user_id.strip()
        if snapshot is None and not normalized:
            return False
        if snapshot is not None and not snapshot.is_active:
            return False
        return await self._repository.can_read_target(
            normalized or (snapshot.user_id if snapshot is not None else ""),
            target,
            snapshot=snapshot,
        )

    async def filter_readable_targets(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_ids: Sequence[str] | None = None,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> tuple[ResourceAccessTarget, ...]:
        """批量实时过滤并返回当前用户拥有读权限的资源目标元数据（含可见层级）。"""
        if resource_ids is not None and not resource_ids:
            return ()
        normalized = user_id.strip()
        if snapshot is None and not normalized:
            return ()
        if snapshot is not None and not snapshot.is_active:
            return ()
        return await self._repository.filter_readable_targets(
            normalized or (snapshot.user_id if snapshot is not None else ""),
            resource_type,
            resource_ids,
            snapshot=snapshot,
        )

    async def filter_readable_ids(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_ids: Sequence[str] | None = None,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> set[str]:
        """批量实时过滤出当前用户拥有读权限的资源 ID 集合。"""
        targets = await self.filter_readable_targets(
            user_id,
            resource_type,
            resource_ids,
            snapshot=snapshot,
        )
        return {target.resource_id for target in targets}

    async def can_create_resource(
        self,
        user_id: str,
        resource_type: ResourceType,
        visibility_scope: VisibilityScope,
        *,
        owner_department_id: str | None = None,
    ) -> bool:
        """实时判定用户是否允许在指定可见层级与归属部门下创建资源。"""
        snapshot = await self.get_user_snapshot(user_id)
        return evaluate_can_manage(
            snapshot,
            resource_type,
            visibility_scope,
            owner_id=user_id,
            owner_department_id=owner_department_id,
        )

    async def can_manage_resource(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> bool:
        """实时判定用户是否拥有指定已有资源的写/管理权限（支持复用已有权限快照）。"""
        resolved = snapshot if snapshot is not None else await self.get_user_snapshot(user_id)
        return evaluate_can_manage(
            resolved,
            target.resource_type,
            target.visibility_scope,
            owner_id=target.owner_id,
            owner_department_id=target.owner_department_id,
        )

    async def evaluate_target_access(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> tuple[bool, bool]:
        """单次快照内联合评估目标资源的 (can_read, can_manage)，合并 404/403 双校验。"""
        normalized = user_id.strip()
        if snapshot is None and not normalized:
            return False, False
        if snapshot is not None and not snapshot.is_active:
            return False, False
        return await self._repository.evaluate_target_access(
            normalized or (snapshot.user_id if snapshot is not None else ""),
            target,
            snapshot=snapshot,
        )

    async def resolve_and_validate_visibility(
        self,
        *,
        user_id: str,
        resource_type: ResourceType,
        visibility_scope: VisibilityScope,
        owner_department_id: str | None,
        visibility_grants: Sequence[VisibilityGrantInput] | None,
        for_create: bool,
        existing_target: ResourceAccessTarget | None = None,
        preserve_existing_grants_when_none: bool = False,
    ) -> tuple[VisibilityScope, str | None, tuple[VisibilityGrantInput, ...] | None]:
        """集中校验可见范围、归属部门、管理权限与授权清单，并按需生成默认部门授权。

        返回值第三项：
        - 当为 ``tuple`` 时表示需要原子覆盖写入的完整授权列表（空元组表示清空旧授权）；
        - 当为 ``None`` 时表示更新操作未变更授权列表，保留库中原有 grants。
        """
        # 1. 校验资源类型与可见范围的静态合法性。
        if (
            visibility_scope is VisibilityScope.PERSONAL
            and resource_type not in PERSONAL_CAPABLE_RESOURCE_TYPES
        ):
            raise InvalidVisibilityConfigError("visibility_scope_unsupported")

        normalized_dept = (
            owner_department_id.strip() if isinstance(owner_department_id, str) else None
        )
        if normalized_dept == "":
            normalized_dept = None

        if visibility_scope is VisibilityScope.DEPARTMENT:
            if normalized_dept is None:
                raise InvalidVisibilityConfigError("owner_department_required")
        else:
            if normalized_dept is not None:
                raise InvalidVisibilityConfigError("owner_department_not_allowed")
            if visibility_grants:
                raise InvalidVisibilityConfigError("visibility_grants_not_allowed")

        # 2. 实时读取用户权限快照并核对原资源与目标配置的管理权限。
        snapshot = await self.get_user_snapshot(user_id)
        if snapshot is None or not snapshot.is_active:
            raise AccessPermissionDeniedError("permission_denied")

        if existing_target is not None and not evaluate_can_manage(
            snapshot,
            existing_target.resource_type,
            existing_target.visibility_scope,
            owner_id=existing_target.owner_id,
            owner_department_id=existing_target.owner_department_id,
        ):
            raise AccessPermissionDeniedError("permission_denied")

        target_owner_id = (
            user_id
            if for_create
            else (existing_target.owner_id if existing_target is not None else user_id)
        )
        # 归属部门必须先验证为真实存在的 active 部门；注意顺序：
        # 先在权限边界拒绝越权操作（包括超管试图将他人资产改为 personal 或 dept_admin 越权操作），
        # 避免无权用户通过 422 错误码探测部门 ID 是否存在；超管对 department 层级在
        # evaluate_can_manage 中天然返回 True，仍会进入下方的 422 活跃部门存在性校验。
        if not evaluate_can_manage(
            snapshot,
            resource_type,
            visibility_scope,
            owner_id=target_owner_id,
            owner_department_id=normalized_dept,
        ):
            raise AccessPermissionDeniedError("permission_denied")

        if visibility_scope is not VisibilityScope.DEPARTMENT:
            # company/personal 不允许残留任何 grants，统一返回空元组触发清理。
            return visibility_scope, None, ()

        assert normalized_dept is not None
        active_owner_depts = await self._repository.list_active_department_ids((normalized_dept,))
        if normalized_dept not in active_owner_depts:
            raise InvalidVisibilityConfigError("owner_department_not_found")

        # 3. 校验或自动生成 department 级可见性授权列表。
        if visibility_grants is None:
            if (
                not for_create
                and preserve_existing_grants_when_none
                and existing_target is not None
                and existing_target.visibility_scope is VisibilityScope.DEPARTMENT
                and existing_target.owner_department_id == normalized_dept
            ):
                return visibility_scope, normalized_dept, None
            # 创建 department 级资源或变更归属部门/从非部门切换为部门且未显式传 grants 时，
            # 自动授予归属部门可读；仅当超管或当前管理员拥有该部门子树管辖权时才包含子部门，
            # 避免 include_subdepartments=False 的部门管理员默认越权覆盖子部门。
            default_include_subdepartments = (
                snapshot.role is UserRole.SUPER_ADMIN
                or normalized_dept in snapshot.subtree_managed_department_ids
            )
            default_grant = VisibilityGrantInput(
                target_type=GrantTargetType.DEPARTMENT,
                target_id=normalized_dept,
                include_subdepartments=default_include_subdepartments,
            )
            return visibility_scope, normalized_dept, (default_grant,)

        validated_grants = await self._validate_explicit_grants(snapshot, visibility_grants)
        return visibility_scope, normalized_dept, validated_grants

    async def replace_resource_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
        grants: Sequence[VisibilityGrantInput],
    ) -> tuple[VisibilityGrant, ...]:
        """替换指定资源的可见性授权（独立写路径兜底使用）。"""
        return await self._repository.replace_grants(
            resource_type,
            resource_id,
            grants,
            now=self._clock(),
        )

    async def delete_resource_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> None:
        """清理已删除资源的全部可见性授权。"""
        await self._repository.delete_grants(resource_type, resource_id)

    async def list_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> tuple[VisibilityGrant, ...]:
        """读取指定资源的可见性授权列表。"""
        return await self._repository.list_grants(resource_type, resource_id)

    async def list_grants_for_resources(
        self,
        resource_type: ResourceType,
        resource_ids: Sequence[str],
    ) -> dict[str, tuple[VisibilityGrant, ...]]:
        """批量读取多个资源的可见性授权映射。"""
        return await self._repository.list_grants_for_resources(resource_type, resource_ids)

    async def _validate_explicit_grants(
        self,
        snapshot: UserAccessSnapshot,
        grants: Sequence[VisibilityGrantInput],
    ) -> tuple[VisibilityGrantInput, ...]:
        """校验显式传入的 visibility_grants 合法性及部门管理员防越权边界。"""
        seen: set[tuple[GrantTargetType, str]] = set()
        normalized: list[VisibilityGrantInput] = []
        dept_ids_to_check: list[str] = []

        for grant in grants:
            target_id = grant.target_id.strip() if isinstance(grant.target_id, str) else ""
            if not target_id or len(target_id) > 64:
                raise InvalidVisibilityConfigError("visibility_grant_target_invalid")
            if type(grant.include_subdepartments) is not bool:
                raise InvalidVisibilityConfigError("visibility_grant_invalid")

            key = (grant.target_type, target_id)
            if key in seen:
                raise InvalidVisibilityConfigError("visibility_grant_duplicate")
            seen.add(key)

            if grant.target_type is GrantTargetType.ROLE:
                # 严禁部门管理员借 role 授权将部门资源越权扩散给全公司成员或超管。
                if snapshot.role is not UserRole.SUPER_ADMIN:
                    raise AccessPermissionDeniedError("visibility_grant_forbidden")
                if target_id not in _VALID_ROLE_VALUES:
                    raise InvalidVisibilityConfigError("visibility_grant_role_invalid")
                normalized.append(
                    VisibilityGrantInput(
                        target_type=GrantTargetType.ROLE,
                        target_id=target_id,
                        include_subdepartments=False,
                    )
                )
            elif grant.target_type is GrantTargetType.DEPARTMENT:
                # 部门管理员仅可授权给自身管辖内的 active 部门，且仅当拥有该部门子树管辖权时
                # 才允许开启 include_subdepartments=True，防止直属管辖管理员越权扩散至下级子部门。
                if snapshot.role is not UserRole.SUPER_ADMIN:
                    if target_id not in snapshot.managed_department_ids:
                        raise AccessPermissionDeniedError("visibility_grant_forbidden")
                    if (
                        grant.include_subdepartments
                        and target_id not in snapshot.subtree_managed_department_ids
                    ):
                        raise AccessPermissionDeniedError("visibility_grant_forbidden")
                dept_ids_to_check.append(target_id)
                normalized.append(
                    VisibilityGrantInput(
                        target_type=GrantTargetType.DEPARTMENT,
                        target_id=target_id,
                        include_subdepartments=grant.include_subdepartments,
                    )
                )
            else:
                raise InvalidVisibilityConfigError("visibility_grant_target_type_invalid")

        if dept_ids_to_check:
            active_ids = await self._repository.list_active_department_ids(dept_ids_to_check)
            if any(dept_id not in active_ids for dept_id in dept_ids_to_check):
                raise InvalidVisibilityConfigError("visibility_grant_department_not_found")

        return tuple(normalized)


__all__ = ["AccessPolicyService"]
