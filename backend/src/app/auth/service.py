"""企业级本地应急认证、飞书原生 SSO、非生产 Mock 登录与会话管理用例。"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Protocol, TypeVar

from email_validator import EmailNotValidError, validate_email

from app.auth.oauth_state import (
    InMemoryOAuthStateStore,
    OAuthStateStore,
    validate_safe_return_to,
)
from app.auth.rate_limit import FixedWindowRateLimiter, RateLimiter
from common.date import as_utc, utc_now
from common.ids import new_id
from domain.auth import (
    AdminPromotionError,
    AuthContext,
    AuthProvider,
    AuthProviderDisabledError,
    AuthRepository,
    AuthSession,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidOAuthStateError,
    InvalidPasswordError,
    InvalidSessionError,
    MockUserNotFoundError,
    PasswordChangeNotSupportedError,
    PasswordHasher,
    RateLimitExceededError,
    RateLimitUnavailableError,
    SsoAuthenticationError,
    User,
    UserRole,
    UserStatus,
)
from domain.org import OrgMembershipReader, OrgUserDetail, UserOrgMembership
from infrastructure.feishu import FeishuError, FeishuUserIdentity

logger = logging.getLogger(__name__)

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


class FeishuSsoClient(Protocol):
    """认证服务所需的飞书 SSO 最小能力端口。"""

    def build_authorize_url(self, state: str) -> str:
        """构造飞书 OAuth2 授权跳转地址。"""

    async def exchange_code_for_user(self, code: str) -> FeishuUserIdentity:
        """用一次性 OAuth code 换取飞书用户身份信息。"""


@dataclass(frozen=True, slots=True)
class IssuedAuthResult:
    """成功登录后返回的用户领域实体与一次性会话 token 信息。"""

    user: User
    token: str
    expires_at: datetime
    max_age_seconds: int


def _default_user_id() -> str:
    """生成不可枚举的用户主键。"""
    return new_id("usr")


def _default_auth_session_id() -> str:
    """生成不可枚举的认证会话主键。"""
    return new_id("ses")


def _default_token() -> str:
    """生成 256 位随机 URL 安全 token。"""
    return token_urlsafe(32)


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


def _normalize_optional_email(email: str | None) -> str | None:
    """规范化第三方 SSO 返回的可选邮箱，格式非法时安全降级为 None。"""
    if email is None:
        return None
    try:
        return _normalize_email(email)
    except InvalidEmailError:
        return None


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


def _normalize_optional_display_name(display_name: str | None) -> str | None:
    """容错清洗第三方 SSO 姓名：剔除 Unicode Cc 控制字符、去首尾空白并截断至列上限。"""
    if display_name is None:
        return None
    sanitized = "".join(
        character for character in display_name if unicodedata.category(character) != "Cc"
    ).strip()
    if not sanitized:
        return None
    return sanitized[:MAX_DISPLAY_NAME_LENGTH]


def _normalize_client_key(client_key: str) -> str:
    """限制入口 key 长度并保证非空。"""
    normalized = client_key.strip()
    return normalized[:MAX_CLIENT_KEY_LENGTH] or "unknown"


def _hash_token(token: str) -> str:
    """对高熵 token 做快速 SHA-256 摘要。"""
    return sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    """管理本地应急凭据、飞书原生 SSO、非生产 Mock 登录与服务端可撤销会话。"""

    def __init__(
        self,
        repository: AuthRepository,
        password_hasher: PasswordHasher,
        *,
        org_reader: OrgMembershipReader | None = None,
        feishu_client: FeishuSsoClient | None = None,
        oauth_state_store: OAuthStateStore | None = None,
        mock_enabled: bool = False,
        user_id_factory: Callable[[], str] = _default_user_id,
        auth_session_id_factory: Callable[[], str] = _default_auth_session_id,
        token_factory: Callable[[], str] = _default_token,
        clock: Callable[[], datetime] = utc_now,
        login_account_limiter: RateLimiter | None = None,
        login_ip_limiter: RateLimiter | None = None,
        change_password_limiter: RateLimiter | None = None,
    ) -> None:
        """保存仓储、密码哈希器、SSO/组织依赖、限流器及可替换时钟与工厂。"""
        self._repository = repository
        self._password_hasher = password_hasher
        self._org_reader = org_reader
        self._feishu_client = feishu_client
        self._oauth_state_store = (
            oauth_state_store
            if oauth_state_store is not None
            else (InMemoryOAuthStateStore() if feishu_client is not None else None)
        )
        self._mock_enabled = mock_enabled
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
        self._change_password_limiter = change_password_limiter or FixedWindowRateLimiter(
            limit=5,
            window_seconds=15 * 60,
        )

    @property
    def feishu_enabled(self) -> bool:
        """指示当前实例是否启用了飞书原生 SSO。"""
        return self._feishu_client is not None

    @property
    def mock_enabled(self) -> bool:
        """指示当前实例是否启用了非生产本地 Mock 快捷登录。"""
        return self._mock_enabled

    async def provision_local_user(
        self,
        email: str,
        password: str,
        *,
        display_name: str | None = None,
        role: UserRole = UserRole.MEMBER,
    ) -> User:
        """内部受控置备本地密码账号（不对外开放自助注册 HTTP 接口）。"""
        normalized_email = _normalize_email(email)
        normalized_password = _validate_password(password)
        normalized_display_name = _normalize_display_name(display_name)
        current = as_utc(self._clock())
        user = User(
            id=self._user_id_factory(),
            email=normalized_email,
            password_hash=await self._run_password_operation(
                self._password_hasher.hash_password,
                normalized_password,
            ),
            display_name=normalized_display_name,
            status=UserStatus.ACTIVE,
            role=role,
            auth_provider=AuthProvider.LOCAL,
            created_at=current,
            updated_at=current,
        )
        return await self._repository.create_user(user)

    async def bootstrap_super_admin(
        self,
        email: str,
        password: str,
        *,
        display_name: str | None = None,
    ) -> tuple[User, bool]:
        """创建或重置本地应急 super_admin 账号（供运维 CLI 调用）。"""
        normalized_email = _normalize_email(email)
        normalized_password = _validate_password(password)
        normalized_display_name = _normalize_display_name(display_name)
        password_hash = await self._run_password_operation(
            self._password_hasher.hash_password,
            normalized_password,
        )
        current = as_utc(self._clock())
        return await self._repository.upsert_local_super_admin(
            user_id=self._user_id_factory(),
            email=normalized_email,
            password_hash=password_hash,
            display_name=normalized_display_name,
            now=current,
        )

    async def login(
        self,
        email: str,
        password: str,
        *,
        remember_me: bool = False,
        client_key: str = "unknown",
    ) -> IssuedAuthResult:
        """仅允许活跃本地账号校验邮箱密码并签发固定期限会话。"""
        normalized_client = _normalize_client_key(client_key)
        email_key = _login_email_key(email)
        ip_key = f"login:ip:{normalized_client}"
        account_key = f"login:account:{sha256(email_key.encode()).hexdigest()}"
        if not await self._login_ip_limiter.consume(ip_key):
            raise RateLimitExceededError("rate_limited")
        if not await self._login_account_limiter.consume(account_key):
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
        # 未知账号、非 local 账号（飞书/Mock）或无密码哈希时一律执行 dummy_hash 校验，
        # 统一返回 InvalidCredentialsError 防止通过计时或错误差异枚举账号来源。
        if (
            user is None
            or user.auth_provider is not AuthProvider.LOCAL
            or user.password_hash is None
        ):
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

        await self._login_account_limiter.reset(account_key)
        current = as_utc(self._clock())
        ttl_seconds = REMEMBER_SESSION_SECONDS if remember_me else DEFAULT_SESSION_SECONDS
        auth_session, token, max_age_seconds = self._new_session(
            user.id,
            current,
            ttl_seconds,
        )
        await self._repository.create_session(auth_session)
        return IssuedAuthResult(
            user=user,
            token=token,
            expires_at=auth_session.expires_at,
            max_age_seconds=max_age_seconds,
        )

    async def start_feishu_authorize(self, *, return_to: str | None = None) -> str:
        """校验回跳路径、签发一次性防重放 state 并生成飞书授权页跳转地址。"""
        if self._feishu_client is None or self._oauth_state_store is None:
            raise AuthProviderDisabledError("feishu_not_enabled")
        safe_return_to = validate_safe_return_to(return_to)
        state = await self._oauth_state_store.issue(
            provider="feishu",
            return_to=safe_return_to,
        )
        return self._feishu_client.build_authorize_url(state)

    async def complete_feishu_callback(
        self,
        *,
        code: str,
        state: str,
    ) -> tuple[IssuedAuthResult, str]:
        """原子消费一次性 state，向飞书换取身份并签发同源会话 Cookie。"""
        if self._feishu_client is None or self._oauth_state_store is None:
            raise AuthProviderDisabledError("feishu_not_enabled")

        # 必须先原子消费 state，确保即便后续上游换码失败也不存在重放窗口。
        state_payload = await self._oauth_state_store.consume(state, provider="feishu")
        if state_payload is None:
            raise InvalidOAuthStateError("invalid_oauth_state")
        safe_return_to = validate_safe_return_to(state_payload.return_to)

        try:
            identity = await self._feishu_client.exchange_code_for_user(code)
        except FeishuError:
            # 截断底层异常链，防止任何 code/app_secret/access_token 逸出到上层。
            raise SsoAuthenticationError("feishu_auth_failed") from None

        current = as_utc(self._clock())
        token = self._token_factory()
        auth_session_id = self._auth_session_id_factory()
        expires_at = current + timedelta(seconds=DEFAULT_SESSION_SECONDS)
        user = await self._repository.upsert_feishu_user_and_create_session(
            user_id=self._user_id_factory(),
            union_id=identity.union_id,
            open_id=identity.open_id,
            display_name=_normalize_optional_display_name(identity.name),
            email=_normalize_optional_email(identity.email),
            avatar_url=identity.avatar_url,
            employee_no=identity.employee_no,
            job_title=identity.job_title,
            auth_session_id=auth_session_id,
            token_hash=_hash_token(token),
            expires_at=expires_at,
            now=current,
        )
        issued = IssuedAuthResult(
            user=user,
            token=token,
            expires_at=expires_at,
            max_age_seconds=DEFAULT_SESSION_SECONDS,
        )
        return issued, safe_return_to

    async def list_mock_users(self) -> tuple[OrgUserDetail, ...]:
        """返回非生产本地 Mock 模式下全部可用测试账号及其组织信息。"""
        if not self._mock_enabled or self._org_reader is None:
            raise AuthProviderDisabledError("auth_mock_not_enabled")
        return await self._org_reader.list_active_mock_users()

    async def login_mock_user(self, user_id: str) -> IssuedAuthResult:
        """非生产 Mock 快捷登录：校验目标为活跃 mock 账号后签发真实服务端会话。"""
        if not self._mock_enabled:
            raise AuthProviderDisabledError("auth_mock_not_enabled")
        cleaned_id = user_id.strip()
        if not cleaned_id:
            raise MockUserNotFoundError("mock_user_not_found")
        user = await self._repository.get_user_by_id(cleaned_id)
        if (
            user is None
            or user.auth_provider is not AuthProvider.MOCK
            or user.deleted_time is not None
        ):
            raise MockUserNotFoundError("mock_user_not_found")
        if user.status is not UserStatus.ACTIVE:
            raise InvalidCredentialsError("invalid_credentials")
        current = as_utc(self._clock())
        auth_session, token, max_age_seconds = self._new_session(
            user.id,
            current,
            DEFAULT_SESSION_SECONDS,
        )
        await self._repository.create_session(auth_session)
        return IssuedAuthResult(
            user=user,
            token=token,
            expires_at=auth_session.expires_at,
            max_age_seconds=max_age_seconds,
        )

    async def get_user_membership(self, user_id: str) -> UserOrgMembership:
        """按需读取用户所属活跃部门及已授权管辖部门 ID（供 HTTP 响应拼装使用）。"""
        if self._org_reader is None:
            return UserOrgMembership(departments=(), managed_department_ids=())
        return await self._org_reader.get_user_org_membership(user_id)

    async def authenticate(self, token: str) -> AuthContext | None:
        """验证原始 token 并返回有效登录上下文。"""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        return await self._repository.get_active_session(
            _hash_token(token),
            as_utc(self._clock()),
        )

    async def promote_admin_by_email(self, email: str) -> User:
        """将已有 active 且未删除账号提升为超级管理员。"""
        try:
            normalized_email = _normalize_email(email)
        except InvalidEmailError as error:
            raise AdminPromotionError("admin_promotion_unavailable") from error
        user = await self._repository.promote_active_user_by_email(
            normalized_email,
            as_utc(self._clock()),
        )
        if user is None:
            raise AdminPromotionError("admin_promotion_unavailable")
        return user

    async def logout(self, token: str) -> None:
        """撤销当前 token；未知或已失效 token 保持幂等。"""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return
        await self._repository.revoke_session(
            _hash_token(token),
            as_utc(self._clock()),
        )

    async def change_password(
        self,
        context: AuthContext,
        current_password: str,
        new_password: str,
    ) -> None:
        """仅允许本地密码账号修改密码，并撤销其他设备会话。"""
        if (
            context.user.auth_provider is not AuthProvider.LOCAL
            or context.user.password_hash is None
        ):
            raise PasswordChangeNotSupportedError("password_change_not_supported")
        rate_limit_key = f"change_password:user:{context.user.id}"
        # 按 user_id 限制改密失败次数，在进入 Argon2 校验前拦截暴力猜测。
        if not await self._change_password_limiter.consume(rate_limit_key):
            raise RateLimitExceededError("rate_limited")
        if not current_password or len(current_password) > MAX_PASSWORD_LENGTH:
            raise InvalidCurrentPasswordError("current_password_invalid")
        if not await self._run_password_operation(
            self._password_hasher.verify_password,
            current_password,
            context.user.password_hash,
        ):
            raise InvalidCurrentPasswordError("current_password_invalid")
        validated_password = _validate_password(new_password)
        password_hash = await self._run_password_operation(
            self._password_hasher.hash_password,
            validated_password,
        )
        current = as_utc(self._clock())
        changed = await self._repository.change_password_and_revoke_sessions(
            context.user.id,
            password_hash,
            current,
            keep_session_id=context.session_id,
            revoked_at=current,
        )
        if not changed:
            raise InvalidSessionError("invalid_session")
        # 改密与会话吊销已落库提交；限流窗口重置为非关键清理，
        # Redis 短暂异常不应掩盖已成功的密码修改。
        try:
            await self._change_password_limiter.reset(rate_limit_key)
        except RateLimitUnavailableError:
            logger.warning("auth_change_password_rate_limit_reset_skipped")

    async def update_display_name(
        self,
        context: AuthContext,
        display_name: str | None,
    ) -> User:
        """更新当前用户显示名，并在持久化边界复核会话。"""
        normalized_display_name = _normalize_display_name(display_name)
        updated = await self._repository.update_display_name(
            context.user.id,
            context.session_id,
            normalized_display_name,
            as_utc(self._clock()),
        )
        if updated is None:
            raise InvalidSessionError("invalid_session")
        return updated

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
        """在线程中执行密码运算，并限制同时运行的任务数量。"""
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
        """在线程任务结束后释放名额并读取异常，避免取消后的警告。"""
        self._password_operation_slots.release()
        if not task.cancelled():
            task.exception()


__all__ = [
    "AuthService",
    "DEFAULT_SESSION_SECONDS",
    "FeishuSsoClient",
    "IssuedAuthResult",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "REMEMBER_SESSION_SECONDS",
]
