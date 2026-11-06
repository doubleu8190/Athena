"""认证领域模型与异常。"""

from domain.auth.model.entity import AuthContext, AuthSession, User, UserRole, UserStatus
from domain.auth.model.errors import (
    AdminPromotionError,
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
    "AdminPromotionError",
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
    "UserRole",
    "UserStatus",
]
