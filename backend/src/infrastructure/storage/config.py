"""统一对象存储（S3 / MinIO）单套环境变量配置与严格校验。"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

STORAGE_ENDPOINT_ENV = "ATHENA_STORAGE_ENDPOINT"
STORAGE_ACCESS_KEY_ENV = "ATHENA_STORAGE_ACCESS_KEY"
STORAGE_SECRET_KEY_ENV = "ATHENA_STORAGE_SECRET_KEY"
STORAGE_BUCKET_ENV = "ATHENA_STORAGE_BUCKET"
STORAGE_SECURE_ENV = "ATHENA_STORAGE_SECURE"

STORAGE_REQUIRED_ENV_NAMES = (
    STORAGE_ENDPOINT_ENV,
    STORAGE_ACCESS_KEY_ENV,
    STORAGE_SECRET_KEY_ENV,
    STORAGE_BUCKET_ENV,
)

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class ObjectStorageConfigError(ValueError):
    """统一对象存储配置缺失或格式非法。"""

    def __init__(self, code: str = "storage_config_invalid") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ObjectStorageConfig:
    """跨知识库与文件/技能业务共用的单套对象存储连接配置。

    业务隔离通过同桶内独立对象键前缀实现（knowledge/、session-files/、
    session-artifacts/、skill-logs/、skills/），凭据字段不进入 repr。
    """

    endpoint: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    bucket: str
    secure: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str):
            raise ObjectStorageConfigError()
        validate_storage_endpoint(self.endpoint)
        if (
            not isinstance(self.access_key, str)
            or not self.access_key.strip()
            or not isinstance(self.secret_key, str)
            or not self.secret_key.strip()
        ):
            raise ObjectStorageConfigError()
        if not isinstance(self.bucket, str) or not valid_storage_bucket(self.bucket):
            raise ObjectStorageConfigError()
        if not isinstance(self.secure, bool):
            raise ObjectStorageConfigError()

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> ObjectStorageConfig:
        """从统一 ATHENA_STORAGE_* 环境变量解析并校验对象存储配置。"""
        source = os.environ if environ is None else environ
        endpoint = _required_setting(source, STORAGE_ENDPOINT_ENV)
        access_key = _required_setting(source, STORAGE_ACCESS_KEY_ENV)
        secret_key = _required_setting(source, STORAGE_SECRET_KEY_ENV)
        bucket = _required_setting(source, STORAGE_BUCKET_ENV)
        secure = _strict_flag(source, STORAGE_SECURE_ENV, default=True)
        return cls(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            secure=secure,
        )


def has_object_storage_env(environ: Mapping[str, str] | None = None) -> bool:
    """判断部署环境是否已配置任一 ATHENA_STORAGE_* 连接项。"""
    source = os.environ if environ is None else environ
    return any((source.get(name) or "").strip() for name in STORAGE_REQUIRED_ENV_NAMES)


def validate_storage_endpoint(endpoint: str) -> None:
    """严格校验 host[:port]，支持域名、IPv4 与方括号 IPv6，拒绝协议/路径/凭据。"""
    if (
        not endpoint
        or any(character.isspace() or ord(character) < 32 for character in endpoint)
        or any(character in "/\\?#@%" for character in endpoint)
    ):
        raise ObjectStorageConfigError() from None
    try:
        parsed = urlsplit(f"//{endpoint}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ObjectStorageConfigError() from None
    if (
        not host
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ObjectStorageConfigError()
    trailing = endpoint.rsplit("]", 1)[-1] if endpoint.startswith("[") else endpoint
    if (":" in trailing and port is None) or (port is not None and not 1 <= port <= 65535):
        raise ObjectStorageConfigError()
    if endpoint.startswith("["):
        # 方括号形式仅允许合法 IPv6 字面量。
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ObjectStorageConfigError() from None
        return
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        if host.replace(".", "").isdigit() and "." in host:
            raise ObjectStorageConfigError() from None
    if len(host) > 253 or any(_HOST_LABEL.fullmatch(label) is None for label in host.split(".")):
        raise ObjectStorageConfigError()


def valid_storage_bucket(bucket: str) -> bool:
    """校验符合 S3/MinIO 命名规范的存储桶名称。"""
    return _BUCKET.fullmatch(bucket) is not None and ".." not in bucket


def _required_setting(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise ObjectStorageConfigError()
    return value


def _strict_flag(environ: Mapping[str, str], name: str, *, default: bool) -> bool:
    value = environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ObjectStorageConfigError()


__all__ = [
    "ObjectStorageConfig",
    "ObjectStorageConfigError",
    "STORAGE_ACCESS_KEY_ENV",
    "STORAGE_BUCKET_ENV",
    "STORAGE_ENDPOINT_ENV",
    "STORAGE_REQUIRED_ENV_NAMES",
    "STORAGE_SECRET_KEY_ENV",
    "STORAGE_SECURE_ENV",
    "has_object_storage_env",
    "valid_storage_bucket",
    "validate_storage_endpoint",
]
