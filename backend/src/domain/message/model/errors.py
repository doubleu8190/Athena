"""消息领域异常。"""


class InvalidMessageError(ValueError):
    """消息内容或角色不满足约束。"""


class InvalidMessageReferenceError(InvalidMessageError):
    """消息引用不存在、跨 Session 或指向错误角色。"""


class MessageConflictError(ValueError):
    """同一幂等键对应了不同消息内容。"""


class SessionNotFoundError(LookupError):
    """会话不存在、已删除或不属于当前用户。"""


__all__ = [
    "InvalidMessageError",
    "InvalidMessageReferenceError",
    "MessageConflictError",
    "SessionNotFoundError",
]
