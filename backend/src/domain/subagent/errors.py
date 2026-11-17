"""Subagent 配置管理领域异常定义。"""

from __future__ import annotations


class SubagentError(ValueError):
    """Subagent 领域错误基类。"""

    code: str = "subagent_invalid"

    def __init__(self, code: str | None = None) -> None:
        """使用稳定安全错误码初始化异常。"""
        if code is not None:
            self.code = code
        super().__init__(self.code)


class SubagentAdminRequiredError(SubagentError):
    """操作者不具备管理员权限。"""

    code = "admin_required"


class SubagentInvalidSessionError(SubagentError):
    """操作者会话已失效。"""

    code = "subagent_session_invalid"


class SubagentNotFoundError(SubagentError):
    """目标 Subagent 不存在或已被删除。"""

    code = "subagent_not_found"


class SubagentSlugConflictError(SubagentError):
    """活跃 Subagent 中已存在相同 slug。"""

    code = "subagent_slug_conflict"


class SubagentSlugReservedError(SubagentError):
    """请求使用的 slug 属于系统保留字。"""

    code = "subagent_slug_reserved"


class SubagentVersionConflictError(SubagentError):
    """乐观锁版本不匹配，配置已被其他管理员修改。"""

    code = "subagent_version_conflict"


class SubagentToolForbiddenError(SubagentError):
    """工具白名单包含禁止嵌套调度的工具。"""

    code = "subagent_tool_forbidden"


class SubagentModelUnavailableError(SubagentError):
    """指定的模型配置不存在或未启用。"""

    code = "subagent_model_unavailable"


class SubagentValidationError(SubagentError):
    """Subagent 字段格式或取值范围不合法。"""

    code = "subagent_validation_error"


__all__ = [
    "SubagentAdminRequiredError",
    "SubagentError",
    "SubagentInvalidSessionError",
    "SubagentModelUnavailableError",
    "SubagentNotFoundError",
    "SubagentSlugConflictError",
    "SubagentSlugReservedError",
    "SubagentToolForbiddenError",
    "SubagentValidationError",
    "SubagentVersionConflictError",
]
