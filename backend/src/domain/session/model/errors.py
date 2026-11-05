"""会话管理业务异常。"""


class SessionNotFoundError(LookupError):
    """会话不存在、已经删除或不属于当前用户。"""


class SessionProjectLockedError(ValueError):
    """会话已经产生消息，不能再修改项目绑定。"""


class InvalidSessionTitleError(ValueError):
    """会话标题不满足展示名称约束。"""


class InvalidSessionUpdateError(ValueError):
    """会话更新请求没有可修改字段。"""


class InvalidSessionFilterError(ValueError):
    """会话列表筛选参数组合无效。"""


__all__ = [
    "InvalidSessionFilterError",
    "InvalidSessionTitleError",
    "InvalidSessionUpdateError",
    "SessionNotFoundError",
    "SessionProjectLockedError",
]
