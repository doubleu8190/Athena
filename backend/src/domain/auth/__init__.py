"""认证与用户身份领域公共接口。"""

from domain.auth.model import (
    AdminPromotionError,
    AuthContext,
    AuthSession,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidPasswordError,
    InvalidSessionError,
    RateLimitExceededError,
    RateLimitUnavailableError,
    User,
    UserRole,
    UserStatus,
)
from domain.auth.ports import (
    Authenticator,
    AuthRepository,
    AuthTransactionGuard,
    CurrentSessionReader,
    PasswordHasher,
)

__all__ = [
    "AdminPromotionError",
    "AuthContext",
    "AuthRepository",
    "AuthTransactionGuard",
    "CurrentSessionReader",
    "AuthSession",
    "Authenticator",
    "EmailAlreadyRegisteredError",
    "InvalidCredentialsError",
    "InvalidCurrentPasswordError",
    "InvalidDisplayNameError",
    "InvalidEmailError",
    "InvalidPasswordError",
    "InvalidSessionError",
    "PasswordHasher",
    "RateLimitExceededError",
    "RateLimitUnavailableError",
    "User",
    "UserRole",
    "UserStatus",
]
