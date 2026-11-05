"""认证与用户身份领域公共接口。"""

from domain.auth.model import (
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
    UserStatus,
)
from domain.auth.ports import (
    Authenticator,
    AuthRepository,
    PasswordHasher,
)

__all__ = [
    "AuthContext",
    "AuthRepository",
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
    "UserStatus",
]
