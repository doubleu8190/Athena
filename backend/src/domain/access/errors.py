"""统一权限引擎领域异常。"""

from __future__ import annotations


class AccessControlError(Exception):
    """统一权限引擎异常基类，携带对外安全错误码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AccessPermissionDeniedError(AccessControlError, PermissionError):
    """当前用户无权执行目标资源的创建、管理或越权授权操作。"""

    def __init__(self, code: str = "permission_denied") -> None:
        super().__init__(code)


class InvalidVisibilityConfigError(AccessControlError, ValueError):
    """资源的可见范围、归属部门或可见性授权配置不合法。"""

    def __init__(self, code: str = "visibility_config_invalid") -> None:
        super().__init__(code)


__all__ = [
    "AccessControlError",
    "AccessPermissionDeniedError",
    "InvalidVisibilityConfigError",
]
