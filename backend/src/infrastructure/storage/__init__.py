"""统一对象存储基础设施配置与连接池工厂。"""

from infrastructure.storage.config import (
    STORAGE_ACCESS_KEY_ENV,
    STORAGE_BUCKET_ENV,
    STORAGE_ENDPOINT_ENV,
    STORAGE_REQUIRED_ENV_NAMES,
    STORAGE_SECRET_KEY_ENV,
    STORAGE_SECURE_ENV,
    ObjectStorageConfig,
    ObjectStorageConfigError,
    has_object_storage_env,
    valid_storage_bucket,
    validate_storage_endpoint,
)
from infrastructure.storage.http import (
    DEFAULT_MINIO_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MINIO_READ_TIMEOUT_SECONDS,
    build_minio_http_pool,
)

__all__ = [
    "DEFAULT_MINIO_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MINIO_READ_TIMEOUT_SECONDS",
    "ObjectStorageConfig",
    "ObjectStorageConfigError",
    "STORAGE_ACCESS_KEY_ENV",
    "STORAGE_BUCKET_ENV",
    "STORAGE_ENDPOINT_ENV",
    "STORAGE_REQUIRED_ENV_NAMES",
    "STORAGE_SECRET_KEY_ENV",
    "STORAGE_SECURE_ENV",
    "build_minio_http_pool",
    "has_object_storage_env",
    "valid_storage_bucket",
    "validate_storage_endpoint",
]
