"""认证领域异常。"""


class InvalidEmailError(ValueError):
    """邮箱格式或长度不合法。"""


class InvalidPasswordError(ValueError):
    """新密码不满足长度约束。"""


class InvalidDisplayNameError(ValueError):
    """显示名不满足约束。"""


class EmailAlreadyRegisteredError(ValueError):
    """规范化邮箱已被注册。"""


class InvalidCredentialsError(ValueError):
    """邮箱或密码不能通过认证。"""


class InvalidCurrentPasswordError(ValueError):
    """修改密码时提供的当前密码错误。"""


class InvalidSessionError(LookupError):
    """登录会话不存在、已撤销、已过期或用户不可用。"""


class RateLimitExceededError(ValueError):
    """认证请求超过允许频率。"""


__all__ = [
    "EmailAlreadyRegisteredError",
    "InvalidCredentialsError",
    "InvalidCurrentPasswordError",
    "InvalidDisplayNameError",
    "InvalidEmailError",
    "InvalidPasswordError",
    "InvalidSessionError",
    "RateLimitExceededError",
]
