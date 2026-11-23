"""统一对象存储基础设施配置。"""

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
