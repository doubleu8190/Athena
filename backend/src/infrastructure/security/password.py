"""基于 Argon2id 的密码哈希实现。"""

from __future__ import annotations

from pwdlib import PasswordHash

_DUMMY_PASSWORD_HASH = PasswordHash.recommended().hash(
    "athena-login-timing-equalizer-not-a-real-password"
)


class Argon2PasswordHasher:
    """使用 pwdlib 推荐的 Argon2id 参数。"""

    def __init__(self) -> None:
        """初始化密码哈希器。"""
        self._password_hash = PasswordHash.recommended()

    @property
    def dummy_hash(self) -> str:
        """返回未知账号校验使用的固定 Argon2 哈希。"""
        return _DUMMY_PASSWORD_HASH

    def hash_password(self, password: str) -> str:
        """生成 Argon2id 哈希。"""
        return self._password_hash.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        """校验密码；损坏的哈希不会被当作匹配。"""
        try:
            return self._password_hash.verify(password, password_hash)
        except Exception:
            return False


__all__ = ["Argon2PasswordHasher"]
