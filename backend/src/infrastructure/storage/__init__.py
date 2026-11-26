"""统一对象存储基础设施配置、连接池工厂与共享 MinIO 客户端。"""

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
from infrastructure.storage.minio_client import (
    SHARED_EXECUTOR_MAX_WORKERS,
    SHARED_HTTP_POOL_MAXSIZE,
    MinioClientError,
    MinioObjectClient,
    StorageErrorKind,
    classify_storage_error,
    close_response,
)

__all__ = [
    "DEFAULT_MINIO_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MINIO_READ_TIMEOUT_SECONDS",
    "MinioClientError",
    "MinioObjectClient",
    "ObjectStorageConfig",
    "ObjectStorageConfigError",
    "SHARED_EXECUTOR_MAX_WORKERS",
    "SHARED_HTTP_POOL_MAXSIZE",
    "STORAGE_ACCESS_KEY_ENV",
    "STORAGE_BUCKET_ENV",
    "STORAGE_ENDPOINT_ENV",
    "STORAGE_REQUIRED_ENV_NAMES",
    "STORAGE_SECRET_KEY_ENV",
    "STORAGE_SECURE_ENV",
    "StorageErrorKind",
    "build_minio_http_pool",
    "classify_storage_error",
    "close_response",
    "has_object_storage_env",
    "valid_storage_bucket",
    "validate_storage_endpoint",
]
