"""个人工作偏好与显式记忆领域异常定义。"""

from __future__ import annotations


class PreferenceError(ValueError):
    """个人工作偏好与显式记忆领域错误基类。"""

    code: str = "preference_invalid"

    def __init__(self, code: str | None = None) -> None:
        """使用稳定安全错误码初始化异常。"""
        if code is not None:
            self.code = code
        super().__init__(self.code)


class InvalidCustomInstructionsError(PreferenceError):
    """个人工作偏好指令超出长度限制或包含非法控制字符。"""

    code = "custom_instructions_invalid"


class InvalidUserMemoryContentError(PreferenceError):
    """显式记忆正文为空、超出长度限制或包含非法控制字符。"""

    code = "user_memory_content_invalid"


class UserMemoryNotFoundError(PreferenceError):
    """目标显式记忆不存在或不属于当前用户。"""

    code = "user_memory_not_found"


class UserMemoryLimitReachedError(PreferenceError):
    """当前用户的显式记忆数量已达到硬性配额上限（50 条）。"""

    code = "user_memory_limit_reached"


class UserMemoryDisabledError(PreferenceError):
    """用户已关闭显式记忆功能，拒绝通过对话工具写入新记忆。"""

    code = "user_memory_disabled"


__all__ = [
    "InvalidCustomInstructionsError",
    "InvalidUserMemoryContentError",
    "PreferenceError",
    "UserMemoryDisabledError",
    "UserMemoryLimitReachedError",
    "UserMemoryNotFoundError",
]
