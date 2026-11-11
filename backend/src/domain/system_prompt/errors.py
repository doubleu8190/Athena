"""系统提示词管理错误。"""


class SystemPromptError(ValueError):
    """系统提示词输入或配置错误基类。"""

    code = "system_prompt_invalid"

    def __init__(self, code: str | None = None) -> None:
        """使用稳定安全错误码初始化异常。"""
        super().__init__(code or self.code)


class SystemPromptAuthorizationError(SystemPromptError):
    """操作者已不再具有管理员权限。"""

    code = "admin_required"


class SystemPromptNotFoundError(SystemPromptError):
    """指定版本不存在或不属于请求类别。"""

    code = "system_prompt_not_found"


class SystemPromptConflictError(SystemPromptError):
    """当前版本已变化，不能覆盖其他管理员刚保存的配置。"""

    code = "system_prompt_version_conflict"


class SystemPromptUnavailableError(SystemPromptError):
    """初始化版本或当前指针缺失，不能安全提供提示词。"""

    code = "system_prompt_unavailable"


__all__ = [
    "SystemPromptAuthorizationError",
    "SystemPromptConflictError",
    "SystemPromptError",
    "SystemPromptNotFoundError",
    "SystemPromptUnavailableError",
]
