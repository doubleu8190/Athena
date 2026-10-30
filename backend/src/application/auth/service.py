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

    def issue(self, username: str) -> str:
        """为指定用户名签发认证令牌。

        参数：
        - `username`（str）：登录账号或令牌中绑定的用户名；约束：无额外运行时限制。

        返回：
        - str：带有签名和过期时间的认证令牌。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    def verify(self, token: str) -> str | None:
        """验证认证令牌并返回其绑定用户名；令牌无效或过期时返回 None。

        参数：
        - `token`（str）：待签发、读取或验证的认证令牌；约束：无额外运行时限制。

        返回：
        - str | None：令牌绑定的用户名；验证失败、令牌过期或内容无效时为 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class InvalidCredentialsError(ValueError):
    """账号或密码不匹配。"""


class HmacTokenSigner:
    """使用 HMAC-SHA256 签名的无状态 Cookie 令牌。"""

    def __init__(self, secret: str, ttl_seconds: int) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `secret`（str）：用于生成和验证 HMAC 签名的密钥；约束：无额外运行时限制。
        - `ttl_seconds`（int）：认证令牌从签发时刻起允许有效的秒数；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    def issue(self, username: str) -> str:
        """将用户名和过期时间编码后生成 HMAC-SHA256 签名令牌。

        参数：
        - `username`（str）：登录账号或令牌中绑定的用户名；约束：无额外运行时限制。

        返回：
        - str：带有签名和过期时间的认证令牌。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        payload = base64.urlsafe_b64encode(
            json.dumps({"u": username, "exp": int(time.time()) + self._ttl_seconds}, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        signature = hmac.new(self._secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify(self, token: str) -> str | None:
        """验证 HMAC-SHA256 签名及令牌过期时间，失败时返回 None。

        参数：
        - `token`（str）：待签发、读取或验证的认证令牌；约束：无额外运行时限制。

        返回：
        - str | None：令牌绑定的用户名；验证失败、令牌过期或内容无效时为 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `config`（AuthenticationConfig）：当前操作使用的已解析配置；约束：无额外运行时限制。
        - `signer`（TokenSigner | None）：签发和验证认证令牌的依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self.config = config
        self._signer = signer or HmacTokenSigner(config.session_secret, config.session_ttl_seconds)

    async def login(self, username: str, password: str) -> LoginResponse:
        """校验登录凭据和认证配置，并返回登录结果。

        参数：
        - `username`（str）：登录账号或令牌中绑定的用户名；约束：须通过方法内校验；失败条件见下方异常说明。
        - `password`（str）：用户提交的登录密码，仅用于与认证配置进行安全比较；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - LoginResponse：认证成功状态及已验证用户名组成的登录响应。

        异常：
        - InvalidCredentialsError：校验条件 `not hmac.compare_digest(username, self.config.username) or not hmac.compare_digest(password, self.config.password)` 成立时（异常说明：invalid_credentials）。
        - RuntimeError：校验条件 `self.config.enabled and len(self.config.session_secret) < 32` 成立时（异常说明：auth_secret_not_configured）。
        - 未列出的依赖异常按当前实现传播。
        """
        if not hmac.compare_digest(username, self.config.username) or not hmac.compare_digest(password, self.config.password):
            raise InvalidCredentialsError("invalid_credentials")
        if self.config.enabled and len(self.config.session_secret) < 32:
            raise RuntimeError("auth_secret_not_configured")
        return LoginResponse(authenticated=True, username=username)

    def issue_token(self, username: str) -> str:
        """使用已配置的签名器为用户名签发认证令牌。

        参数：
        - `username`（str）：登录账号或令牌中绑定的用户名；约束：无额外运行时限制。

        返回：
        - str：可写入会话 Cookie 的签名令牌。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return self._signer.issue(username)

    def authenticate_token(self, token: str | None) -> str | None:
        """验证 Cookie 令牌并返回用户名。

        参数：
        - `token`（str | None）：待签发、读取或验证的认证令牌；约束：允许为 None，按未提供值的分支处理。

        返回：
        - str | None：令牌绑定的用户名；令牌缺失、过期或校验失败时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if not self.config.enabled:
            return self.config.username
        if len(self.config.session_secret) < 32:
            return None
        return self._signer.verify(token or "")

    def session(self, token: str | None) -> AuthSessionResponse:
        """返回当前请求的认证状态。

        参数：
        - `token`（str | None）：待签发、读取或验证的认证令牌；约束：允许为 None，按未提供值的分支处理。

        返回：
        - AuthSessionResponse：会话标识、项目归属和会话元数据组成的响应。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        username = self.authenticate_token(token)
        return AuthSessionResponse(authenticated=username is not None, username=username)


__all__ = ["AuthenticationConfig", "AuthenticationService", "HmacTokenSigner", "InvalidCredentialsError", "TokenSigner"]
