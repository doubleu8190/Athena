"""统一三层权限引擎领域公共导出。"""

from domain.access.errors import (
    AccessControlError,
    AccessPermissionDeniedError,
    InvalidVisibilityConfigError,
)
from domain.access.model import (
    DEPT_ADMIN_MANAGEABLE_RESOURCE_TYPES,
    PERSONAL_CAPABLE_RESOURCE_TYPES,
    GrantTargetType,
    ResourceAccessTarget,
    ResourceType,
    UserAccessSnapshot,
    VisibilityGrant,
    VisibilityGrantInput,
    VisibilityScope,
)
from domain.access.policy import (
    evaluate_can_manage,
    evaluate_can_read,
    parse_department_path_ids,
)
from domain.access.ports import AccessControlRepository

__all__ = [
    "AccessControlError",
    "AccessControlRepository",
    "AccessPermissionDeniedError",
    "DEPT_ADMIN_MANAGEABLE_RESOURCE_TYPES",
    "GrantTargetType",
    "InvalidVisibilityConfigError",
    "PERSONAL_CAPABLE_RESOURCE_TYPES",
    "ResourceAccessTarget",
    "ResourceType",
    "UserAccessSnapshot",
    "VisibilityGrant",
    "VisibilityGrantInput",
    "VisibilityScope",
    "evaluate_can_manage",
    "evaluate_can_read",
    "parse_department_path_ids",
]
