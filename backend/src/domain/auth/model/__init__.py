"""认证领域模型与异常。"""

from domain.auth.model.entity import AuthContext, AuthSession, User, UserStatus
from domain.auth.model.errors import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidPasswordError,
    InvalidSessionError,
    RateLimitExceededError,
)

__all__ = [
    "AuthContext",
    "AuthSession",
    "EmailAlreadyRegisteredError",
    "InvalidCredentialsError",
    "InvalidCurrentPasswordError",
    "InvalidDisplayNameError",
    "InvalidEmailError",
    "InvalidPasswordError",
    "InvalidSessionError",
    "RateLimitExceededError",
    "User",
    "UserStatus",
]
