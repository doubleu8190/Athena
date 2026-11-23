"""统一三层权限引擎纯领域判定规则（无 I/O 与框架依赖）。"""

from __future__ import annotations

from collections.abc import Sequence
from collections.abc import Set as AbstractSet

from domain.access.model import (
    DEPT_ADMIN_MANAGEABLE_RESOURCE_TYPES,
    PERSONAL_CAPABLE_RESOURCE_TYPES,
    GrantTargetType,
    ResourceAccessTarget,
    ResourceType,
    UserAccessSnapshot,
    VisibilityGrant,
    VisibilityScope,
)
from domain.auth import UserRole


def parse_department_path_ids(path: str) -> tuple[str, ...]:
    """从规范物化路径 /<root_id>/.../<self_id>/ 解析去重后的全部部门 ID。"""
    if not path:
        return ()
    return tuple(dict.fromkeys(segment for segment in path.split("/") if segment))


def evaluate_can_read(
    snapshot: UserAccessSnapshot | None,
    target: ResourceAccessTarget,
    grants: Sequence[VisibilityGrant],
    *,
    active_grant_department_ids: AbstractSet[str],
) -> bool:
    """判定指定用户实时快照是否对目标资源拥有读权限。

    权限边界：
    1. 停用或不存在用户一律拒绝；
    2. personal 仅 owner 本人可读，超级管理员亦不可越权查看他人私有资产；
    3. company 对全体活跃登录用户开放；
    4. department 按 super_admin、管辖该归属部门的 dept_admin、或命中有效 grants 放行。
    """
    if snapshot is None or not snapshot.is_active:
        return False

    if target.visibility_scope is VisibilityScope.PERSONAL:
        return target.owner_id is not None and target.owner_id == snapshot.user_id

    if target.visibility_scope is VisibilityScope.COMPANY:
        return True

    # department 级资源判定：超管全局可读；管辖该归属部门的部门管理员可读。
    if snapshot.role is UserRole.SUPER_ADMIN:
        return True
    if (
        snapshot.role is UserRole.DEPT_ADMIN
        and target.owner_department_id is not None
        and target.owner_department_id in snapshot.managed_department_ids
    ):
        return True

    # 其余用户按 resource_visibility_grants 实时匹配部门祖先链、直属部门或角色。
    for grant in grants:
        if grant.target_type is GrantTargetType.DEPARTMENT:
            # 被授权的目标部门已归档（非 active）时，该条授权自动失效。
            if grant.target_id not in active_grant_department_ids:
                continue
            if grant.include_subdepartments:
                if grant.target_id in snapshot.ancestor_department_ids:
                    return True
            else:
                if grant.target_id in snapshot.direct_department_ids:
                    return True
        elif grant.target_type is GrantTargetType.ROLE:
            if grant.target_id == snapshot.role.value:
                return True
    return False


def evaluate_can_manage(
    snapshot: UserAccessSnapshot | None,
    resource_type: ResourceType,
    visibility_scope: VisibilityScope,
    *,
    owner_id: str | None = None,
    owner_department_id: str | None = None,
) -> bool:
    """判定指定用户是否拥有目标资源的创建/写/管理权限。

    权限边界：
    - super_admin：可创建并管理全部 company/department 四类资源，以及本人的 personal 资产；
    - dept_admin：仅可管理管辖子树内 active 部门的 knowledge_base/skill，以及本人 personal 资产；
    - member：仅可管理本人 personal knowledge_base/skill。
    """
    if snapshot is None or not snapshot.is_active:
        return False

    if visibility_scope is VisibilityScope.PERSONAL:
        if resource_type not in PERSONAL_CAPABLE_RESOURCE_TYPES:
            return False
        # 保守失败：owner_id 缺失时直接拒绝，创建路径由调用方显式传入 owner_id=snapshot.user_id。
        return owner_id is not None and owner_id == snapshot.user_id

    if visibility_scope is VisibilityScope.COMPANY:
        return snapshot.role is UserRole.SUPER_ADMIN

    # department 级管理权限判定。
    if snapshot.role is UserRole.SUPER_ADMIN:
        return True
    if snapshot.role is UserRole.DEPT_ADMIN:
        return (
            resource_type in DEPT_ADMIN_MANAGEABLE_RESOURCE_TYPES
            and owner_department_id is not None
            and owner_department_id in snapshot.managed_department_ids
        )
    return False


__all__ = [
    "evaluate_can_manage",
    "evaluate_can_read",
    "parse_department_path_ids",
]
