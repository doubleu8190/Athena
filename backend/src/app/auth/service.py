"""用户注册、登录、登出与密码管理用例。"""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import TypeVar

from email_validator import EmailNotValidError, validate_email

from app.auth.converter.result import user_to_result
from app.auth.model.result import IssuedAuthResult
from app.auth.rate_limit import FixedWindowRateLimiter, RateLimiter
from common.ids import new_id, now_utc
from domain.auth import (
    AuthContext,
    AuthRepository,
    AuthSession,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidPasswordError,
    InvalidSessionError,
    PasswordHasher,
    RateLimitExceededError,
    User,
    UserStatus,
)

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
MAX_EMAIL_LENGTH = 320
MAX_DISPLAY_NAME_LENGTH = 80
MAX_CLIENT_KEY_LENGTH = 128
MAX_TOKEN_LENGTH = 512
PASSWORD_OPERATION_LIMIT = 4
DEFAULT_SESSION_SECONDS = 7 * 24 * 60 * 60
REMEMBER_SESSION_SECONDS = 30 * 24 * 60 * 60
_PasswordResult = TypeVar("_PasswordResult")


def _default_user_id() -> str:
    """生成不可枚举的用户主键。"""
    return new_id("usr")


def _default_auth_session_id() -> str:
    """生成不可枚举的认证会话主键。"""
    return new_id("ses")


def _default_token() -> str:
    """生成 256 位随机 URL 安全 token。"""
    return token_urlsafe(32)


def _utc_value(value: datetime) -> datetime:
    """把时钟输出规范化为 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_email(email: str) -> str:
    """验证邮箱并生成大小写不敏感的规范形式。"""
    candidate = email.strip()
    if not candidate or len(candidate) > MAX_EMAIL_LENGTH:
        raise InvalidEmailError("invalid_email")
    try:
        validated = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError as error:
        raise InvalidEmailError("invalid_email") from error
    normalized = validated.normalized.lower()
    if len(normalized) > MAX_EMAIL_LENGTH:
        raise InvalidEmailError("invalid_email")
    return normalized


def _login_email_key(email: str) -> str:
    """为无效邮箱也生成稳定的限流键。"""
    candidate = email.strip().lower()
    return candidate[:MAX_EMAIL_LENGTH] or "empty"


def _validate_password(password: str) -> str:
    """校验新密码长度。"""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise InvalidPasswordError("password_invalid")
    return password


def _normalize_display_name(display_name: str | None) -> str | None:
    """清理可选显示名并拒绝控制字符。"""
    if display_name is None:
        return None
    normalized = display_name.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_DISPLAY_NAME_LENGTH or any(
        unicodedata.category(character) == "Cc" for character in normalized
    ):
        raise InvalidDisplayNameError("display_name_invalid")
    return normalized


def _normalize_client_key(client_key: str) -> str:
    """限制入口 key 长度并保证非空。"""
    normalized = client_key.strip()
    return normalized[:MAX_CLIENT_KEY_LENGTH] or "unknown"


def _hash_token(token: str) -> str:
    """对高熵 token 做快速 SHA-256 摘要。"""
    return sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    """管理用户凭据与服务端可撤销登录会话。"""

    def __init__(
        self,
        repository: AuthRepository,
        password_hasher: PasswordHasher,
        *,
        user_id_factory: Callable[[], str] = _default_user_id,
        auth_session_id_factory: Callable[[], str] = _default_auth_session_id,
        token_factory: Callable[[], str] = _default_token,
        clock: Callable[[], datetime] = now_utc,
        login_account_limiter: RateLimiter | None = None,
        login_ip_limiter: RateLimiter | None = None,
        register_limiter: RateLimiter | None = None,
    ) -> None:
        """保存仓储、密码哈希器、限流器及可替换来源。"""
        self._repository = repository
        self._password_hasher = password_hasher
        self._password_operation_slots = asyncio.Semaphore(PASSWORD_OPERATION_LIMIT)
        self._user_id_factory = user_id_factory
        self._auth_session_id_factory = auth_session_id_factory
        self._token_factory = token_factory
        self._clock = clock
        self._login_account_limiter = login_account_limiter or FixedWindowRateLimiter(
            limit=5,
            window_seconds=15 * 60,
        )
        self._login_ip_limiter = login_ip_limiter or FixedWindowRateLimiter(
            limit=30,
            window_seconds=15 * 60,
        )
        self._register_limiter = register_limiter or FixedWindowRateLimiter(
            limit=10,
            window_seconds=60 * 60,
        )

    async def register(
        self,
        email: str,
        password: str,
        *,
        display_name: str | None = None,
        client_key: str = "unknown",
    ) -> IssuedAuthResult:
        """开放注册并直接签发登录会话。"""
        normalized_email = _normalize_email(email)
        normalized_password = _validate_password(password)
        normalized_display_name = _normalize_display_name(display_name)
        normalized_client = _normalize_client_key(client_key)
        if not self._register_limiter.consume(f"register:{normalized_client}"):
            raise RateLimitExceededError("rate_limited")

        current = _utc_value(self._clock())
        user = User(
            id=self._user_id_factory(),
            email=normalized_email,
            password_hash=await self._run_password_operation(
                self._password_hasher.hash_password,
                normalized_password,
            ),
            display_name=normalized_display_name,
            status=UserStatus.ACTIVE,
            created_at=current,
            updated_at=current,
        )
        auth_session, token, max_age_seconds = self._new_session(
            user.id,
            current,
            DEFAULT_SESSION_SECONDS,
        )
        created = await self._repository.create_user_with_session(user, auth_session)
        return IssuedAuthResult(
            user=user_to_result(created),
            token=token,
            expires_at=auth_session.expires_at,
            max_age_seconds=max_age_seconds,
        )

    async def login(
        self,
        email: str,
        password: str,
        *,
        remember_me: bool = False,
        client_key: str = "unknown",
    ) -> IssuedAuthResult:
        """校验邮箱密码并签发固定期限会话。"""
        normalized_client = _normalize_client_key(client_key)
        email_key = _login_email_key(email)
        ip_key = f"login:ip:{normalized_client}"
        account_key = f"login:account:{sha256(email_key.encode()).hexdigest()}"
        if not self._login_ip_limiter.consume(ip_key):
            raise RateLimitExceededError("rate_limited")
        if not self._login_account_limiter.consume(account_key):
            raise RateLimitExceededError("rate_limited")
        if not password or len(password) > MAX_PASSWORD_LENGTH:
            raise InvalidCredentialsError("invalid_credentials")

        try:
            normalized_email = _normalize_email(email)
        except InvalidEmailError as error:
            await self._run_password_operation(
                self._password_hasher.verify_password,
                password,
                self._password_hasher.dummy_hash,
            )
            raise InvalidCredentialsError("invalid_credentials") from error

        user = await self._repository.get_user_by_email(normalized_email)
        if user is None:
            await self._run_password_operation(
                self._password_hasher.verify_password,
                password,
                self._password_hasher.dummy_hash,
            )
            raise InvalidCredentialsError("invalid_credentials")
        password_matches = await self._run_password_operation(
            self._password_hasher.verify_password,
            password,
            user.password_hash,
        )
        if (
            not password_matches
            or user.status is not UserStatus.ACTIVE
            or user.deleted_time is not None
        ):
            raise InvalidCredentialsError("invalid_credentials")

        self._login_account_limiter.reset(account_key)
        current = _utc_value(self._clock())
        ttl_seconds = REMEMBER_SESSION_SECONDS if remember_me else DEFAULT_SESSION_SECONDS
        auth_session, token, max_age_seconds = self._new_session(
            user.id,
            current,
            ttl_seconds,
        )
        await self._repository.create_session(auth_session)
        return IssuedAuthResult(
            user=user_to_result(user),
            token=token,
            expires_at=auth_session.expires_at,
            max_age_seconds=max_age_seconds,
        )

    async def authenticate(self, token: str) -> AuthContext | None:
        """验证原始 token 并返回有效登录上下文。"""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        return await self._repository.get_active_session(
            _hash_token(token),
            _utc_value(self._clock()),
        )

    async def logout(self, token: str) -> None:
        """撤销当前 token；未知或已失效 token 保持幂等。"""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return
        await self._repository.revoke_session(
            _hash_token(token),
            _utc_value(self._clock()),
        )

    async def change_password(
        self,
        context: AuthContext,
        current_password: str,
        new_password: str,
    ) -> None:
        """校验当前密码并撤销其他设备会话。"""
        if not current_password or len(current_password) > MAX_PASSWORD_LENGTH:
            raise InvalidCurrentPasswordError("current_password_invalid")
        if not await self._run_password_operation(
            self._password_hasher.verify_password,
            current_password,
            context.user.password_hash,
        ):
            raise InvalidCurrentPasswordError("current_password_invalid")
        validated_password = _validate_password(new_password)
        current = _utc_value(self._clock())
        changed = await self._repository.change_password_and_revoke_sessions(
            context.user.id,
            await self._run_password_operation(
                self._password_hasher.hash_password,
                validated_password,
            ),
            current,
            keep_session_id=context.session_id,
            revoked_at=current,
        )
        if not changed:
            raise InvalidSessionError("invalid_session")

    def _new_session(
        self,
        user_id: str,
        now: datetime,
        ttl_seconds: int,
    ) -> tuple[AuthSession, str, int]:
        """生成原始 token 和只保存摘要的认证会话。"""
        token = self._token_factory()
        auth_session = AuthSession(
            id=self._auth_session_id_factory(),
            user_id=user_id,
            token_hash=_hash_token(token),
            expires_at=now + timedelta(seconds=ttl_seconds),
            revoked_time=None,
            created_at=now,
        )
        return auth_session, token, ttl_seconds

    async def _run_password_operation(
        self,
        operation: Callable[..., _PasswordResult],
        *arguments: str,
    ) -> _PasswordResult:
        """在线程中执行密码运算，并限制同时运行的任务数量。

        参数：operation 是同步哈希或验证函数；arguments 是传给该函数的字符串参数。
        返回：operation 在线程中的计算结果。
        异常：传播 operation 抛出的异常；调用任务取消时传播 CancelledError。
        """
        await self._password_operation_slots.acquire()
        task = asyncio.create_task(asyncio.to_thread(operation, *arguments))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # 线程无法随协程取消而停止，必须等它结束后再释放名额。
            if task.done():
                self._password_operation_slots.release()
            else:
                task.add_done_callback(self._release_password_operation_slot)
            raise
        except BaseException:
            self._password_operation_slots.release()
            raise
        self._password_operation_slots.release()
        return result

    def _release_password_operation_slot(self, task: asyncio.Task[object]) -> None:
        """在线程任务结束后释放名额并读取异常，避免取消后的警告。

        参数：task 是已完成的后台线程任务。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._password_operation_slots.release()
        if not task.cancelled():
            task.exception()


__all__ = [
    "AuthService",
    "DEFAULT_SESSION_SECONDS",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "REMEMBER_SESSION_SECONDS",
]
