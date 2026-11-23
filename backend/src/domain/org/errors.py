"""组织架构与通讯录同步领域异常。"""

from __future__ import annotations


class OrgError(RuntimeError):
    """组织领域稳定错误码基类。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OrgAuthorizationError(OrgError):
    """当前会话不再具备有效超级管理员权限。"""

    def __init__(self, code: str = "admin_required") -> None:
        super().__init__(code)


class OrgUserNotFoundError(OrgError):
    """目标用户不存在或已被软删除。"""

    def __init__(self, code: str = "user_not_found") -> None:
        super().__init__(code)


class DepartmentNotFoundError(OrgError):
    """指定的部门不存在或已归档。"""

    def __init__(self, code: str = "department_not_found") -> None:
        super().__init__(code)


class InvalidDeptAdminScopeError(OrgError):
    """部门管理员管辖范围配置不合法（如为空或重复）。"""

    def __init__(self, code: str = "dept_admin_departments_required") -> None:
        super().__init__(code)


class LastSuperAdminProtectedError(OrgError):
    """禁止降权或停用系统内最后一个活跃超级管理员。"""

    def __init__(self, code: str = "last_super_admin_protected") -> None:
        super().__init__(code)


class DepartmentCycleError(OrgError):
    """远端部门树存在循环引用，拒绝提交同步以保护现有组织树。"""

    def __init__(self, code: str = "department_cycle_detected") -> None:
        super().__init__(code)


class OrgSyncValidationError(OrgError):
    """远端通讯录数据不满足同步校验规则（如重复部门 ID 或路径超长）。"""

    def __init__(self, code: str = "org_sync_validation_failed") -> None:
        super().__init__(code)


class FeishuNotEnabledError(OrgError):
    """当前部署未启用飞书集成。"""

    def __init__(self, code: str = "feishu_not_enabled") -> None:
        super().__init__(code)


__all__ = [
    "DepartmentCycleError",
    "DepartmentNotFoundError",
    "FeishuNotEnabledError",
    "InvalidDeptAdminScopeError",
    "LastSuperAdminProtectedError",
    "OrgAuthorizationError",
    "OrgError",
    "OrgSyncValidationError",
    "OrgUserNotFoundError",
]
