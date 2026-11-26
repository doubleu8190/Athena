"""统一权限引擎持久化读取与授权维护端口。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from domain.access.model import (
    ResourceAccessTarget,
    ResourceType,
    UserAccessSnapshot,
    VisibilityGrant,
    VisibilityGrantInput,
)


class AccessControlRepository(Protocol):
    """实时读取用户组织归属、分权管辖范围与资源可见性授权的仓储端口。"""

    async def get_user_access_snapshot(self, user_id: str) -> UserAccessSnapshot | None:
        """实时读取用户的角色、启用状态、活跃祖先部门与管辖子树快照。"""

    async def list_active_department_ids(self, department_ids: Sequence[str]) -> frozenset[str]:
        """过滤并返回指定 ID 集合中当前仍处于 active 状态的部门 ID。"""

    async def get_resource_target(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> ResourceAccessTarget | None:
        """读取单个未删除资源的权限判定元数据。"""

    async def list_resource_targets(
        self,
        resource_type: ResourceType,
        resource_ids: Sequence[str] | None = None,
    ) -> tuple[ResourceAccessTarget, ...]:
        """批量读取指定类型（或限定 ID 集合）未删除资源的权限判定元数据。"""

    async def list_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> tuple[VisibilityGrant, ...]:
        """读取单个资源的全部可见性授权。"""

    async def list_grants_for_resources(
        self,
        resource_type: ResourceType,
        resource_ids: Sequence[str],
    ) -> dict[str, tuple[VisibilityGrant, ...]]:
        """批量读取多个资源的可见性授权，返回按 resource_id 分组的映射。"""

    async def replace_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
        grants: Sequence[VisibilityGrantInput],
        *,
        now: datetime,
    ) -> tuple[VisibilityGrant, ...]:
        """原子替换指定资源的可见性授权记录。"""

    async def delete_grants(
        self,
        resource_type: ResourceType,
        resource_id: str,
    ) -> None:
        """删除指定资源关联的全部可见性授权记录。"""

    async def filter_readable_targets(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_ids: Sequence[str] | None = None,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> tuple[ResourceAccessTarget, ...]:
        """单会话内加载或复用权限快照，下推过滤指定类型下的可读资源元数据。"""

    async def can_read_resource(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> bool:
        """单会话内实时判定用户对单个未删除资源是否拥有读权限。"""

    async def can_read_target(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> bool:
        """已知资源归属元数据时实时判定读权限（命中快速路径时零查库）。"""

    async def evaluate_target_access(
        self,
        user_id: str,
        target: ResourceAccessTarget,
        *,
        snapshot: UserAccessSnapshot | None = None,
    ) -> tuple[bool, bool]:
        """单次快照内联合返回目标资源的 (可读, 可管) 双判定结果。"""


__all__ = ["AccessControlRepository"]
