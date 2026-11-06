"""认证持久化和密码校验端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.auth.model.entity import AuthContext, AuthSession, User


class PasswordHasher(Protocol):
    """密码哈希与校验能力。"""

    @property
    def dummy_hash(self) -> str:
        """返回用于未知账号的固定哈希。"""

    def hash_password(self, password: str) -> str:
        """生成不可逆密码哈希。"""

    def verify_password(self, password: str, password_hash: str) -> bool:
        """校验明文密码与哈希是否匹配。"""


class CurrentSessionReader(Protocol):
    """按可信用户与 session ID 重新读取有效身份及当前角色。"""

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        """独立只读预检查；不为后续写事务提供并发授权保证。"""


class AuthRepository(CurrentSessionReader, Protocol):
    """用户与登录会话的一致性持久化端口。"""

    async def create_user_with_session(
        self,
        user: User,
        auth_session: AuthSession,
    ) -> User:
        """在同一事务中创建用户与首个登录会话。"""

    async def get_user_by_email(self, email: str) -> User | None:
        """按规范化邮箱读取用户，包括不可用账号。"""

    async def create_session(self, auth_session: AuthSession) -> AuthSession:
        """为已有用户创建新的登录会话。"""

    async def get_active_session(
        self,
        token_hash: str,
        now: datetime,
    ) -> AuthContext | None:
        """读取未撤销、未过期且用户可用的登录上下文。"""

    async def promote_active_user_by_email(
        self,
        email: str,
        updated_at: datetime,
    ) -> User | None:
        """锁定账号行，将现存 active 且未删除账号设为管理员。"""

    async def revoke_session(
        self,
        token_hash: str,
        revoked_at: datetime,
    ) -> bool:
        """撤销指定 token 对应的登录会话。"""

    async def change_password_and_revoke_sessions(
        self,
        user_id: str,
        password_hash: str,
        updated_at: datetime,
        *,
        keep_session_id: str,
        revoked_at: datetime,
    ) -> bool:
        """原子修改密码并撤销当前会话之外的其他会话。"""


class Authenticator(Protocol):
    """HTTP 层所需的 token 认证能力。"""

    async def authenticate(self, token: str) -> AuthContext | None:
        """验证原始 token 并返回登录上下文。"""


class AuthTransactionGuard(Protocol):
    """绑定到调用方事务、为最终管理操作复核身份的窄能力。"""

    async def lock_current_session_user(
        self,
        user_id: str,
        session_id: str,
    ) -> User | None:
        """在调用方事务中按 user 后 session 的顺序锁行并检查当前有效身份。"""


__all__ = [
    "AuthRepository",
    "AuthTransactionGuard",
    "Authenticator",
    "CurrentSessionReader",
    "PasswordHasher",
]
