"""为 MCP 凭据与 LLM API Key 等敏感配置提供通用 AES-GCM 秘密封装。"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from common.secrets import MAX_SECRET_BYTES, SecretCipherError, SecretPurpose

_KEY_ENV = "ATHENA_MCP_SECRET_KEY"
_VERSION_ENV = "ATHENA_MCP_SECRET_KEY_VERSION"
_MAX_NONCE_BYTES = 12


@dataclass(frozen=True, slots=True)
class SealedSecrets:
    """仅含持久化所需的认证密文材料。"""

    ciphertext: bytes
    nonce: bytes
    key_version: int


class SecretCipher:
    """使用固定版本应用主密钥对不同用途的凭据字典执行 AES-GCM 认证加密。"""

    def __init__(self, key: bytes, key_version: int = 1) -> None:
        """校验 256-bit AES 密钥及正数版本。

        参数：key 是 32 字节应用主密钥；key_version 是密钥版本正整数。
        返回：无。
        异常：密钥长度或版本不合法时抛 SecretCipherError。
        """
        if len(key) != 32 or isinstance(key_version, bool) or key_version < 1:
            raise SecretCipherError("mcp_secret_key_invalid")
        self._key = key
        self._key_version = key_version

    @classmethod
    def from_environment(cls) -> SecretCipher:
        """从部署环境严格载入 base64 32-byte 主密钥和版本。

        参数：无。
        返回：已验证的 AES-GCM 封装器。
        异常：密钥缺失、base64 无效、密钥长度错误或版本无效时抛 SecretCipherError。
        """
        encoded_key = os.getenv(_KEY_ENV)
        raw_version = os.getenv(_VERSION_ENV, "1")
        if not encoded_key:
            raise SecretCipherError("mcp_secret_key_missing")
        try:
            key = base64.b64decode(encoded_key, validate=True)
            if not raw_version.isdecimal() or int(raw_version) < 1:
                raise ValueError
            return cls(key, int(raw_version))
        except (ValueError, TypeError) as error:
            raise SecretCipherError("mcp_secret_key_invalid") from error

    @property
    def key_version(self) -> int:
        """返回当前密钥版本。"""
        return self._key_version

    def seal(
        self,
        owner_id: str,
        purpose: SecretPurpose,
        values: Mapping[str, str],
    ) -> SealedSecrets:
        """使用随机 nonce、用途绑定 AAD 加密 secrets 映射。

        参数：owner_id 是密文所属实体（如 Server ID 或模型版本 ID）；purpose 区分用途；
        values 是秘密名和值的映射；名称和值会作为一个整体加密。
        返回：密文、随机 nonce 和 key version。
        异常：输入过大/类型错误时抛 SecretCipherError。
        """
        try:
            normalized_values = dict(values)
            if any(
                not isinstance(name, str)
                or not isinstance(value, str)
                or "\x00" in name
                or "\x00" in value
                for name, value in normalized_values.items()
            ):
                raise ValueError
            plaintext = json.dumps(
                normalized_values,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(plaintext) > MAX_SECRET_BYTES:
                raise ValueError
        except (TypeError, ValueError, UnicodeError) as error:
            raise SecretCipherError("mcp_secret_payload_invalid") from error

        nonce = os.urandom(_MAX_NONCE_BYTES)
        associated_data = self._associated_data(owner_id, purpose)
        encrypted = AESGCM(self._key).encrypt(nonce, plaintext, associated_data)
        return SealedSecrets(encrypted, nonce, self._key_version)

    def open(
        self,
        owner_id: str,
        purpose: SecretPurpose,
        ciphertext: bytes,
        nonce: bytes,
        key_version: int,
    ) -> Mapping[str, str]:
        """验证 key version、AAD 和 GCM tag 后解封 secrets。

        参数：owner_id、purpose、nonce 和 key_version 必须与封装时一致；ciphertext 为完整信封。
        返回：恢复的秘密名和值映射。
        异常：密钥版本、信封、AAD 或认证标签不匹配时抛 SecretCipherError。
        """
        try:
            if key_version != self._key_version or len(nonce) != _MAX_NONCE_BYTES:
                raise ValueError
            plaintext = AESGCM(self._key).decrypt(
                nonce,
                ciphertext,
                self._associated_data(owner_id, purpose),
            )
            values = json.loads(plaintext)
            if not isinstance(values, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in values.items()
            ):
                raise ValueError
            if len(plaintext) > MAX_SECRET_BYTES:
                raise ValueError
            return values
        except (InvalidTag, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise SecretCipherError("mcp_secret_decrypt_failed") from error

    def _associated_data(
        self,
        owner_id: str,
        purpose: SecretPurpose,
    ) -> bytes:
        # 严格保持历史 AAD 格式不变，确保既有数据库密文 100% 向后兼容可解密。
        return f"athena:mcp-secret:{owner_id}:{purpose.value}:{self._key_version}".encode()


__all__ = [
    "MAX_SECRET_BYTES",
    "SealedSecrets",
    "SecretCipher",
    "SecretCipherError",
    "SecretPurpose",
]
