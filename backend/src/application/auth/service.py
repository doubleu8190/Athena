"""配置账号认证用例和签名 Cookie 能力。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Protocol

from application.common.responses import AuthSessionResponse, LoginResponse


@dataclass(frozen=True, slots=True)
class AuthenticationConfig:
    """单用户认证所需的配置。"""

    enabled: bool = False
    username: str = "athena"
    password: str = "change-me"
    session_secret: str = ""
    session_ttl_seconds: int = 24 * 3600


class TokenSigner(Protocol):
    """签发和验证会话令牌的端口。"""

    def issue(self, username: str) -> str: ...

    def verify(self, token: str) -> str | None: ...


class InvalidCredentialsError(ValueError):
    """账号或密码不匹配。"""


class HmacTokenSigner:
    """使用 HMAC-SHA256 签名的无状态 Cookie 令牌。"""

    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    def issue(self, username: str) -> str:
        """签发带过期时间的用户名令牌。"""
        payload = base64.urlsafe_b64encode(
            json.dumps({"u": username, "exp": int(time.time()) + self._ttl_seconds}, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        signature = hmac.new(self._secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify(self, token: str) -> str | None:
        """验证签名和过期时间，失败时返回 None。"""
        if "." not in token or not self._secret:
            return None
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(self._secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            padded = payload + "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded))
            if float(data.get("exp", 0)) <= time.time():
                return None
            username = data.get("u")
            return str(username) if username else None
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error):
            return None


class AuthenticationService:
    """执行登录、登出前的令牌验证和会话状态查询。"""

    def __init__(self, config: AuthenticationConfig, signer: TokenSigner | None = None) -> None:
        self.config = config
        self._signer = signer or HmacTokenSigner(config.session_secret, config.session_ttl_seconds)

    async def login(self, username: str, password: str) -> LoginResponse:
        """校验配置账号并返回待写入 Cookie 的登录结果。"""
        if not hmac.compare_digest(username, self.config.username) or not hmac.compare_digest(password, self.config.password):
            raise InvalidCredentialsError("invalid_credentials")
        if self.config.enabled and len(self.config.session_secret) < 32:
            raise RuntimeError("auth_secret_not_configured")
        return LoginResponse(authenticated=True, username=username)

    def issue_token(self, username: str) -> str:
        """为已通过认证的用户名签发令牌。"""
        return self._signer.issue(username)

    def authenticate_token(self, token: str | None) -> str | None:
        """验证 Cookie 令牌并返回用户名。"""
        if not self.config.enabled:
            return self.config.username
        if len(self.config.session_secret) < 32:
            return None
        return self._signer.verify(token or "")

    def session(self, token: str | None) -> AuthSessionResponse:
        """返回当前请求的认证状态。"""
        username = self.authenticate_token(token)
        return AuthSessionResponse(authenticated=username is not None, username=username)


__all__ = ["AuthenticationConfig", "AuthenticationService", "HmacTokenSigner", "InvalidCredentialsError", "TokenSigner"]
