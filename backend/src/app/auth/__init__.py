"""认证应用层。"""

from app.auth.model.result import IssuedAuthResult, UserResult
from app.auth.service import (
    DEFAULT_SESSION_SECONDS,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    REMEMBER_SESSION_SECONDS,
    AuthService,
)
from domain.auth import (
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
    "AuthService",
    "DEFAULT_SESSION_SECONDS",
    "EmailAlreadyRegisteredError",
    "InvalidCredentialsError",
    "InvalidCurrentPasswordError",
    "InvalidDisplayNameError",
    "InvalidEmailError",
    "InvalidPasswordError",
    "InvalidSessionError",
    "IssuedAuthResult",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "REMEMBER_SESSION_SECONDS",
    "RateLimitExceededError",
    "UserResult",
]
