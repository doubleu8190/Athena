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


class PasswordChangeNotSupportedError(ValueError):
    """非本地密码账号（如飞书 SSO 或 Mock）不支持修改密码。"""


class InvalidSessionError(LookupError):
    """登录会话不存在、已撤销、已过期或用户不可用。"""


class AdminPromotionError(ValueError):
    """目标账号不存在或当前不可提升为超级管理员。"""


class RateLimitExceededError(ValueError):
    """认证请求超过允许频率。"""


class RateLimitUnavailableError(RuntimeError):
    """认证限流基础设施（如 Redis）不可用，按 fail-safe 策略拒绝继续放行。"""


class AuthProviderDisabledError(ValueError):
    """请求的认证方式在当前部署未启用。"""


class InvalidReturnToError(ValueError):
    """登录回跳地址不是安全的站内相对路径。"""


class InvalidOAuthStateError(ValueError):
    """OAuth 回调 state 缺失、已消费、已过期或来源不匹配。"""


class OAuthStateUnavailableError(RuntimeError):
    """OAuth state 存储基础设施（如 Redis）不可用。"""


class UserDisabledError(PermissionError):
    """目标用户已停用、离职或已软删除，拒绝签发会话。"""


class MockUserNotFoundError(LookupError):
    """请求的 Mock 快捷登录用户不存在或不是 Mock 账号。"""


class SsoAuthenticationError(RuntimeError):
    """第三方 SSO 认证交换失败（不包含任何敏感凭据）。"""


__all__ = [
    "AdminPromotionError",
    "AuthProviderDisabledError",
    "EmailAlreadyRegisteredError",
    "InvalidCredentialsError",
    "InvalidCurrentPasswordError",
    "InvalidDisplayNameError",
    "InvalidEmailError",
    "InvalidOAuthStateError",
    "InvalidPasswordError",
    "InvalidReturnToError",
    "InvalidSessionError",
    "MockUserNotFoundError",
    "OAuthStateUnavailableError",
    "PasswordChangeNotSupportedError",
    "RateLimitExceededError",
    "RateLimitUnavailableError",
    "SsoAuthenticationError",
    "UserDisabledError",
]
