"""纯内存单元测试共用的轻量权限引擎夹具（基于真实 AccessPolicyService）。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from app.access import AccessPolicyService
from domain.access import (
    AccessControlRepository,
    ResourceAccessTarget,
    ResourceType,
    UserAccessSnapshot,
    VisibilityGrant,
    VisibilityGrantInput,
    VisibilityScope,
    evaluate_can_manage,
    evaluate_can_read,
)
from domain.auth import UserRole


def active_user_snapshot(
    user_id: str,
    *,
    role: UserRole = UserRole.SUPER_ADMIN,
    direct_department_ids: frozenset[str] = frozenset(),
    ancestor_department_ids: frozenset[str] = frozenset(),
    managed_department_ids: frozenset[str] = frozenset(),
) -> UserAccessSnapshot:
    """构造一个活跃用户权限快照，供测试中调用下推读接口或 build_run_skill_snapshot。"""
    return UserAccessSnapshot(
        user_id=user_id,
        role=role,
        is_active=True,
        direct_department_ids=direct_department_ids,
        ancestor_department_ids=ancestor_department_ids,
        managed_department_ids=managed_department_ids,
    )


class InMemoryAccessControlRepository(AccessControlRepository):
    """供纯内存单元测试注入 AccessPolicyService 的最小权限仓储。

    - 若未显式配置 ``users``，默认将任意用户视为活跃 ``default_role``（默认 ``SUPER_ADMIN``），
      同时对已知 ``ResourceAccessTarget``（含 ``PERSONAL`` 归属隔离）严格执行真实领域判定；
    - ``permissive_unknown=True`` 时，对未在内存注册表登记的 ``resource_id``
      （如假仓储中的工具或知识库）放行读权限；传 ``targets_provider`` 时可动态反映真实资源列表。
    """

    def __init__(
        self,
        *,
        default_role: UserRole = UserRole.SUPER_ADMIN,
        users: Mapping[str, UserAccessSnapshot] | None = None,
        active_department_ids: frozenset[str] = frozenset(),
        targets: Mapping[tuple[ResourceType, str], ResourceAccessTarget] | None = None,
        targets_provider: Callable[[ResourceType], Sequence[ResourceAccessTarget]] | None = None,
        permissive_unknown: bool = True,
    ) -> None:
        self._default_role = default_role
        self._users = dict(users or {})
        self._active_department_ids = active_department_ids
        self._targets = dict(targets or {})
        self._targets_provider = targets_provider
        self._permissive_unknown = permissive_unknown
        self._grants: dict[tuple[ResourceType, str], tuple[VisibilityGrant, ...]] = {}

    def _snapshot(
        self,
        user_id: str,
        snapshot: UserAccessSnapshot | None = None,
    ) -> UserAccessSnapshot | None:
        if snapshot is not None:
            return snapshot
        if user_id in self._users:
            return self._users[user_id]
        return active_user_snapshot(user_id, role=self._default_role)

    def _all_targets_for(self, resource_type: ResourceType) -> dict[str, ResourceAccessTarget]:
        result: dict[str, ResourceAccessTarget] = {
            rid: target for (rtype, rid), target in self._targets.items() if rtype is resource_type
        }
        if self._targets_provider is not None:
            for target in self._targets_provider(resource_type):
                result[target.resource_id] = target
        return result

    async def get_user_access_snapshot(self, user_id: str) -> UserAccessSnapshot | None:
        return self._snapshot(user_id)

    async def list_active_department_ids(self, department_ids: Sequence[str]) -> frozenset[str]:
        if not self._active_department_ids:
            return frozenset(department_ids)
        return frozenset(item for item in department_ids if item in self._active_department_ids)

    async def get_resource_target(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> ResourceAccessTarget | None:
        return self._all_targets_for(resource_type).get(resource_id)

    async def list_resource_targets(
        self,
        resource_type: ResourceType,
        resource_ids: Sequence[str] | None = None,
    ) -> tuple[ResourceAccessTarget, ...]:
        all_targets = self._all_targets_for(resource_type)
        if resource_ids is None:
            return tuple(all_targets.values())
        return tuple(all_targets[rid] for rid in resource_ids if rid in all_targets)

    async def list_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> tuple[VisibilityGrant, ...]:
        return self._grants.get((resource_type, resource_id), ())

    async def list_grants_for_resources(
        self,
        resource_type: ResourceType,
        resource_ids: Sequence[str],
    ) -> dict[str, tuple[VisibilityGrant, ...]]:
        return {rid: self._grants.get((resource_type, rid), ()) for rid in resource_ids}

    async def replace_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
        grants: Sequence[VisibilityGrantInput],
        *,
        now: datetime,
    ) -> tuple[VisibilityGrant, ...]:
        items = tuple(
            VisibilityGrant(
                id=f"grant_{index}",
                resource_type=resource_type,
                resource_id=resource_id,
                target_type=item.target_type,
                target_id=item.target_id,
                include_subdepartments=item.include_subdepartments,
                created_at=now,
            )
            for index, item in enumerate(grants, start=1)
        )
        self._grants[(resource_type, resource_id)] = items
        return items

    async def delete_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> None:
        self._grants.pop((resource_type, resource_id), None)

    def _active_dept_ids_for(self, grants: Sequence[VisibilityGrant]) -> frozenset[str]:
        if self._active_department_ids:
            return self._active_department_ids
        return frozenset(grant.target_id for grant in grants)

    async def filter_readable_targets(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_ids: Sequence[str] | None = None,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> tuple[ResourceAccessTarget, ...]:
        resolved = self._snapshot(user_id, snapshot)
        if resolved is None or not resolved.is_active:
            return ()
        all_targets = self._all_targets_for(resource_type)
        if resource_ids is None:
            candidates = tuple(all_targets.values())
        else:
            candidate_list: list[ResourceAccessTarget] = []
            for rid in resource_ids:
                target = all_targets.get(rid)
                if target is not None:
                    candidate_list.append(target)
                elif self._permissive_unknown:
                    candidate_list.append(
                        ResourceAccessTarget(
                            resource_type=resource_type,
                            resource_id=rid,
                            visibility_scope=VisibilityScope.COMPANY,
                            owner_id=user_id,
                            owner_department_id=None,
                        )
                    )
            candidates = tuple(candidate_list)
        matched: list[ResourceAccessTarget] = []
        for target in candidates:
            grants = self._grants.get((resource_type, target.resource_id), ())
            if evaluate_can_read(
                resolved,
                target,
                grants,
                active_grant_department_ids=self._active_dept_ids_for(grants),
            ):
                matched.append(target)
        return tuple(matched)

    async def can_read_resource(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> bool:
        resolved = self._snapshot(user_id, snapshot)
        if resolved is None or not resolved.is_active:
            return False
        target = self._all_targets_for(resource_type).get(resource_id)
        if target is None:
            return self._permissive_unknown
        grants = self._grants.get((resource_type, resource_id), ())
        return evaluate_can_read(
            resolved,
            target,
            grants,
            active_grant_department_ids=self._active_dept_ids_for(grants),
        )

    async def can_read_target(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> bool:
        resolved = self._snapshot(user_id, snapshot)
        if resolved is None or not resolved.is_active:
            return False
        grants = self._grants.get((target.resource_type, target.resource_id), ())
        return evaluate_can_read(
            resolved,
            target,
            grants,
            active_grant_department_ids=self._active_dept_ids_for(grants),
        )

    async def evaluate_target_access(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> tuple[bool, bool]:
        resolved = self._snapshot(user_id, snapshot)
        if resolved is None or not resolved.is_active:
            return False, False
        grants = self._grants.get((target.resource_type, target.resource_id), ())
        return (
            evaluate_can_read(
                resolved,
                target,
                grants,
                active_grant_department_ids=self._active_dept_ids_for(grants),
            ),
            evaluate_can_manage(
                resolved,
                target.resource_type,
                target.visibility_scope,
                owner_id=target.owner_id,
                owner_department_id=target.owner_department_id,
            ),
        )


def make_test_access_policy(
    *,
    default_role: UserRole = UserRole.SUPER_ADMIN,
    users: Mapping[str, UserAccessSnapshot] | None = None,
    active_department_ids: frozenset[str] = frozenset(),
    targets: Mapping[tuple[ResourceType, str], ResourceAccessTarget] | None = None,
    targets_provider: Callable[[ResourceType], Sequence[ResourceAccessTarget]] | None = None,
    permissive_unknown: bool = True,
) -> AccessPolicyService:
    """构造基于内存权限仓储的真实 AccessPolicyService，供纯内存单元测试复用。"""
    return AccessPolicyService(
        InMemoryAccessControlRepository(
            default_role=default_role,
            users=users,
            active_department_ids=active_department_ids,
            targets=targets,
            targets_provider=targets_provider,
            permissive_unknown=permissive_unknown,
        )
    )


__all__ = [
    "InMemoryAccessControlRepository",
    "active_user_snapshot",
    "make_test_access_policy",
]
