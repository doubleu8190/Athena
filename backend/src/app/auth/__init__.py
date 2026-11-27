"""认证应用层。"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from app.auth.guard import require_session_roles

if TYPE_CHECKING:
    from app.auth.oauth_state import (
        InMemoryOAuthStateStore,
        OAuthStatePayload,
        OAuthStateStore,
        RedisOAuthStateStore,
        validate_safe_return_to,
    )
    from app.auth.service import (
        DEFAULT_SESSION_SECONDS,
        MAX_PASSWORD_LENGTH,
        MIN_PASSWORD_LENGTH,
        REMEMBER_SESSION_SECONDS,
        AuthService,
        FeishuSsoClient,
        IssuedAuthResult,
    )
    from domain.auth import (
        AdminPromotionError,
        AuthProviderDisabledError,
        EmailAlreadyRegisteredError,
        InvalidCredentialsError,
        InvalidCurrentPasswordError,
        InvalidDisplayNameError,
        InvalidEmailError,
        InvalidOAuthStateError,
        InvalidPasswordError,
        InvalidReturnToError,
        InvalidSessionError,
        MockUserNotFoundError,
        OAuthStateUnavailableError,
        PasswordChangeNotSupportedError,
        RateLimitExceededError,
        RateLimitUnavailableError,
        SsoAuthenticationError,
        UserDisabledError,
    )

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "InMemoryOAuthStateStore": ("app.auth.oauth_state", "InMemoryOAuthStateStore"),
    "OAuthStatePayload": ("app.auth.oauth_state", "OAuthStatePayload"),
    "OAuthStateStore": ("app.auth.oauth_state", "OAuthStateStore"),
    "RedisOAuthStateStore": ("app.auth.oauth_state", "RedisOAuthStateStore"),
    "validate_safe_return_to": ("app.auth.oauth_state", "validate_safe_return_to"),
    "DEFAULT_SESSION_SECONDS": ("app.auth.service", "DEFAULT_SESSION_SECONDS"),
    "MAX_PASSWORD_LENGTH": ("app.auth.service", "MAX_PASSWORD_LENGTH"),
    "MIN_PASSWORD_LENGTH": ("app.auth.service", "MIN_PASSWORD_LENGTH"),
    "REMEMBER_SESSION_SECONDS": ("app.auth.service", "REMEMBER_SESSION_SECONDS"),
    "AuthService": ("app.auth.service", "AuthService"),
    "FeishuSsoClient": ("app.auth.service", "FeishuSsoClient"),
    "IssuedAuthResult": ("app.auth.service", "IssuedAuthResult"),
    "AdminPromotionError": ("domain.auth", "AdminPromotionError"),
    "AuthProviderDisabledError": ("domain.auth", "AuthProviderDisabledError"),
    "EmailAlreadyRegisteredError": ("domain.auth", "EmailAlreadyRegisteredError"),
    "InvalidCredentialsError": ("domain.auth", "InvalidCredentialsError"),
    "InvalidCurrentPasswordError": ("domain.auth", "InvalidCurrentPasswordError"),
    "InvalidDisplayNameError": ("domain.auth", "InvalidDisplayNameError"),
    "InvalidEmailError": ("domain.auth", "InvalidEmailError"),
    "InvalidOAuthStateError": ("domain.auth", "InvalidOAuthStateError"),
    "InvalidPasswordError": ("domain.auth", "InvalidPasswordError"),
    "InvalidReturnToError": ("domain.auth", "InvalidReturnToError"),
    "InvalidSessionError": ("domain.auth", "InvalidSessionError"),
    "MockUserNotFoundError": ("domain.auth", "MockUserNotFoundError"),
    "OAuthStateUnavailableError": ("domain.auth", "OAuthStateUnavailableError"),
    "PasswordChangeNotSupportedError": ("domain.auth", "PasswordChangeNotSupportedError"),
    "RateLimitExceededError": ("domain.auth", "RateLimitExceededError"),
    "RateLimitUnavailableError": ("domain.auth", "RateLimitUnavailableError"),
    "SsoAuthenticationError": ("domain.auth", "SsoAuthenticationError"),
    "UserDisabledError": ("domain.auth", "UserDisabledError"),
}


def __getattr__(name: str) -> object:
    """按需延迟加载认证服务与飞书适配依赖，避免导入 app.auth.guard 时连带加载 infrastructure。"""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


__all__ = [
    "AdminPromotionError",
    "AuthProviderDisabledError",
    "AuthService",
    "DEFAULT_SESSION_SECONDS",
    "EmailAlreadyRegisteredError",
    "FeishuSsoClient",
    "InMemoryOAuthStateStore",
    "InvalidCredentialsError",
    "InvalidCurrentPasswordError",
    "InvalidDisplayNameError",
    "InvalidEmailError",
    "InvalidOAuthStateError",
    "InvalidPasswordError",
    "InvalidReturnToError",
    "InvalidSessionError",
    "IssuedAuthResult",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "MockUserNotFoundError",
    "OAuthStatePayload",
    "OAuthStateStore",
    "OAuthStateUnavailableError",
    "PasswordChangeNotSupportedError",
    "REMEMBER_SESSION_SECONDS",
    "RateLimitExceededError",
    "RateLimitUnavailableError",
    "RedisOAuthStateStore",
    "SsoAuthenticationError",
    "UserDisabledError",
    "require_session_roles",
    "validate_safe_return_to",
]
