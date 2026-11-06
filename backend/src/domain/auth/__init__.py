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
    User,
    UserRole,
    UserStatus,
)
from domain.auth.ports import (
    Authenticator,
    AuthRepository,
    AuthTransactionGuard,
    PasswordHasher,
)

__all__ = [
    "AdminPromotionError",
    "AuthContext",
    "AuthRepository",
    "AuthTransactionGuard",
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
    "User",
    "UserRole",
    "UserStatus",
]
