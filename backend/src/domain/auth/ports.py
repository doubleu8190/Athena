"""认证持久化和密码校验端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.auth.model import AuthContext, AuthSession, FeishuUserIdentity, User


class PasswordHasher(Protocol):
    """密码哈希与校验能力。"""

    @property
    def dummy_hash(self) -> str:
        """返回用于未知账号或非本地账号的固定哈希。"""

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

    async def create_user(self, user: User) -> User:
        """受控创建单个用户记录（邮箱冲突时抛 EmailAlreadyRegisteredError）。"""

    async def upsert_local_super_admin(
        self,
        *,
        user_id: str,
        email: str,
        password_hash: str,
        display_name: str | None,
        now: datetime,
    ) -> tuple[User, bool]:
        """创建或重置本地应急 super_admin 账号，并在重置密码时撤销其既有全部会话。"""

    async def get_user_by_id(self, user_id: str) -> User | None:
        """按主键读取用户（包含停用/已删除状态供调用方判别）。"""

    async def get_user_by_email(self, email: str) -> User | None:
        """按规范化邮箱读取未删除用户（包含停用账号）。"""

    async def create_session(self, auth_session: AuthSession) -> AuthSession:
        """为已有用户创建新的登录会话。"""

    async def upsert_feishu_user_and_create_session(
        self,
        *,
        user_id: str,
        union_id: str | None,
        open_id: str | None,
        display_name: str | None,
        email: str | None,
        avatar_url: str | None,
        employee_no: str | None,
        job_title: str | None,
        auth_session_id: str,
        token_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> User:
        """按 union_id/open_id 同步飞书用户并签发会话；停用或已删除用户吊销全部会话后拒绝。"""

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
        """锁定账号行，将现存 active 且未删除账号设为超级管理员。"""

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
        """原子修改本地账号密码并撤销当前会话之外的其他会话。"""

    async def update_display_name(
        self,
        user_id: str,
        session_id: str,
        display_name: str | None,
        updated_at: datetime,
    ) -> User | None:
        """在事务内复核当前会话后，仅更新显示名和修改时间。"""


class Authenticator(Protocol):
    """HTTP 层所需的 token 认证能力。"""

    async def authenticate(self, token: str) -> AuthContext | None:
        """验证原始 token 并返回登录上下文。"""


class FeishuSsoClient(Protocol):
    """认证服务所需的飞书 SSO 最小能力端口。"""

    def build_authorize_url(self, state: str) -> str:
        """构造飞书 OAuth2 授权跳转地址。"""

    async def exchange_code_for_user(self, code: str) -> FeishuUserIdentity:
        """用一次性 OAuth code 换取飞书用户身份信息。"""


__all__ = [
    "AuthRepository",
    "Authenticator",
    "CurrentSessionReader",
    "FeishuSsoClient",
    "PasswordHasher",
]
