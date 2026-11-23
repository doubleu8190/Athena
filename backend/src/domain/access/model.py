"""统一三层权限（公司/部门/个人）核心领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from domain.auth import UserRole


class ResourceType(StrEnum):
    """接入统一三层可见性授权引擎的四类核心资产类型。"""

    KNOWLEDGE_BASE = "knowledge_base"
    SKILL = "skill"
    MCP_SERVER = "mcp_server"
    SUBAGENT = "subagent"


class VisibilityScope(StrEnum):
    """资产可见层级：公司全局、部门协作或个人私有。"""

    COMPANY = "company"
    DEPARTMENT = "department"
    PERSONAL = "personal"


class GrantTargetType(StrEnum):
    """resource_visibility_grants 支持的授权对象类型。"""

    DEPARTMENT = "department"
    ROLE = "role"


# 仅知识库与技能允许个人私有层级；MCP Server 与 Subagent 仅支持公司级与部门级。
PERSONAL_CAPABLE_RESOURCE_TYPES: frozenset[ResourceType] = frozenset(
    {ResourceType.KNOWLEDGE_BASE, ResourceType.SKILL}
)
# 部门管理员仅可维护管辖部门内的知识库与技能，不可触碰 MCP Server 或 Subagent。
DEPT_ADMIN_MANAGEABLE_RESOURCE_TYPES: frozenset[ResourceType] = frozenset(
    {ResourceType.KNOWLEDGE_BASE, ResourceType.SKILL}
)


@dataclass(frozen=True, slots=True)
class VisibilityGrantInput:
    """创建或更新资源可见性授权时的单条输入。"""

    target_type: GrantTargetType
    target_id: str
    include_subdepartments: bool = True


@dataclass(frozen=True, slots=True)
class VisibilityGrant:
    """已持久化的单条资源可见性授权记录。"""

    id: str
    resource_type: ResourceType
    resource_id: str
    target_type: GrantTargetType
    target_id: str
    include_subdepartments: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceAccessTarget:
    """权限判定所需的最小资源归属投影。"""

    resource_type: ResourceType
    resource_id: str
    visibility_scope: VisibilityScope
    owner_id: str | None = None
    owner_department_id: str | None = None


@dataclass(frozen=True, slots=True)
class UserAccessSnapshot:
    """实时从数据库加载的用户角色、状态、部门祖先链及部门管理员管辖子树快照。"""

    user_id: str
    role: UserRole
    is_active: bool
    direct_department_ids: frozenset[str]
    ancestor_department_ids: frozenset[str]
    managed_department_ids: frozenset[str]
    # 仅包含 dept_admin_scopes.include_subdepartments=True 展开的活跃部门及其子孙部门，
    # 用于限制非超管在配置授权时不得对仅直属管辖的部门越权开启 include_subdepartments=True。
    subtree_managed_department_ids: frozenset[str] = frozenset()


__all__ = [
    "DEPT_ADMIN_MANAGEABLE_RESOURCE_TYPES",
    "PERSONAL_CAPABLE_RESOURCE_TYPES",
    "GrantTargetType",
    "ResourceAccessTarget",
    "ResourceType",
    "UserAccessSnapshot",
    "VisibilityGrant",
    "VisibilityGrantInput",
    "VisibilityScope",
]
