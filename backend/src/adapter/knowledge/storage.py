"""基于统一 MinIO 底层引擎的知识库原件不可变对象存储领域包装层。"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Self

from common.date import as_utc, utc_now
from domain.knowledge import model as _km
from domain.knowledge.ports import OriginalStore
from infrastructure.storage import minio_client as _mc
from infrastructure.storage.config import ObjectStorageConfig, ObjectStorageConfigError
from infrastructure.storage.minio_client import (
    MinioObjectClient,
    MinioStoreAdapterBase,
    StorageErrorKind,
)

logger = logging.getLogger(__name__)
# 同桶多业务隔离：知识库原件在底层 S3 统一落入 knowledge/ 前缀，上层维持无前缀逻辑键。
KNOWLEDGE_OBJECT_PREFIX = "knowledge/"
# 仅接受四段式内容寻址逻辑键：{owner_scope}/{source_id}/{sha256[:2]}/{sha256}。
_KEY = re.compile(r"^[a-f0-9]{64}/[a-z0-9_-]{1,64}/[a-f0-9]{2}/[a-f0-9]{64}$")
_MAX_WORKERS = 4
_KNOWLEDGE_ERRORS: dict[StorageErrorKind, Callable[[], Exception]] = {
    StorageErrorKind.NOT_FOUND: _km.OriginalStorageNotFoundError,
    StorageErrorKind.ACCESS_DENIED: _km.OriginalStorageAccessDeniedError,
    StorageErrorKind.INTEGRITY: _km.OriginalStorageIntegrityError,
    StorageErrorKind.BUCKET_MISSING: partial(
        _km.OriginalStorageConfigurationError, "knowledge_storage_bucket_missing"
    ),
    StorageErrorKind.SDK_MISSING: partial(
        _km.OriginalStorageConfigurationError, "knowledge_storage_sdk_missing"
    ),
    StorageErrorKind.CONFIGURATION_INVALID: _km.OriginalStorageConfigurationError,
}


@dataclass(frozen=True, slots=True)
class MinioStorageConfig(ObjectStorageConfig):
    """基于统一 ATHENA_STORAGE_* 的知识库对象存储配置。"""

    def __post_init__(self) -> None:
        try:
            # 显式调用父类校验，兼容 Python 3.12 @dataclass(slots=True) 类重建场景。
            ObjectStorageConfig.__post_init__(self)
        except ObjectStorageConfigError:
            raise _km.OriginalStorageConfigurationError() from None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> MinioStorageConfig:
        try:
            b = ObjectStorageConfig.from_environment(environ)
        except ObjectStorageConfigError:
            raise _km.OriginalStorageConfigurationError() from None
        return cls(b.endpoint, b.access_key, b.secret_key, b.bucket, b.secure)


class MinioOriginalStore(MinioStoreAdapterBase, OriginalStore):
    """在统一私有 S3/MinIO 存储桶的 knowledge/ 前缀下存储内容寻址知识库原件。"""

    _domain_error_type = _km.OriginalStorageError
    _error_factories = _KNOWLEDGE_ERRORS
    _default_error_type = _km.OriginalStorageUnavailableError
    _logger = logger
    _error_prefix = "knowledge_storage"
    _client_name = "knowledge-minio-store"
    _log_domain_errors = True
    _log_operation = True
    _require_config = True
    _max_workers = _MAX_WORKERS
    _http_pool_maxsize = _MAX_WORKERS
    _http_pool_block = True

    def __init__(
        self,
        config: ObjectStorageConfig | None = None,
        *,
        client: _mc._MinioClient | None = None,
        object_client: MinioObjectClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        max_object_bytes: int = _km.MAX_ORIGINAL_BYTES,
    ) -> None:
        if max_object_bytes < 1:
            raise ValueError("knowledge_storage_max_bytes_invalid")
        super().__init__(config, client=client, object_client=object_client, clock=clock)
        self._max_object_bytes = max_object_bytes

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        client: _mc._MinioClient | None = None,
        object_client: MinioObjectClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        max_object_bytes: int = _km.MAX_ORIGINAL_BYTES,
    ) -> Self:
        """从统一 ATHENA_STORAGE_* 构建原件存储；复用共享底层客户端时不传递冗余配置。"""
        if object_client is not None:
            return cls(object_client=object_client, clock=clock, max_object_bytes=max_object_bytes)
        return cls(
            MinioStorageConfig.from_environment(environ),
            client=client,
            clock=clock,
            max_object_bytes=max_object_bytes,
        )

    async def put(self, key: str, content: bytes) -> None:
        """在 knowledge/<key> 下写入不可变原件；已存在同键且内容不一致时拒绝覆盖。"""
        expected_hash = _validate_key_and_content(key, content)
        if len(content) > self._max_object_bytes:
            raise _km.OriginalStorageIntegrityError("knowledge_storage_object_too_large")
        physical_key = f"{KNOWLEDGE_OBJECT_PREFIX}{key}"

        def store() -> None:
            sdk, bucket = self.object_client.target()
            try:
                info = sdk.stat_object(bucket, physical_key)
            except Exception as error:
                if _mc.classify_storage_error(error) is not StorageErrorKind.NOT_FOUND:
                    raise
                info = None
            if info is not None:
                if self._read_verified(physical_key, expected_hash) != content:
                    raise _km.OriginalStorageIntegrityError("knowledge_storage_key_conflict")
                return
            self.object_client.put_sync(
                physical_key, io.BytesIO(content), len(content), "application/octet-stream"
            )

        await self._call(store, operation="put")

    async def get(self, key: str) -> bytes:
        expected_hash = _validate_key(key)
        return await self._call(
            lambda: self._read_verified(f"{KNOWLEDGE_OBJECT_PREFIX}{key}", expected_hash),
            operation="get",
        )

    async def delete(self, key: str) -> None:
        _validate_key(key)
        await self._call(
            lambda: self.object_client.delete_sync(f"{KNOWLEDGE_OBJECT_PREFIX}{key}"),
            operation="delete",
        )

    async def iter_orphan_candidates(
        self, *, older_than_seconds: int = 86400
    ) -> AsyncIterator[str]:
        """仅扫描 knowledge/ 前缀下的老对象，剥离前缀后返回合法逻辑键。"""
        if older_than_seconds < 86400:
            raise ValueError("knowledge_orphan_grace_period_invalid")
        cutoff = as_utc(self._clock()) - timedelta(seconds=older_than_seconds)
        try:
            stream = self.object_client.iter_objects(
                prefix=KNOWLEDGE_OBJECT_PREFIX, is_closed=lambda: self._closed
            )
            async with aclosing(stream) as items:
                async for item in items:
                    modified, name = (
                        getattr(item, "last_modified", None),
                        getattr(item, "object_name", ""),
                    )
                    logical = (
                        name[len(KNOWLEDGE_OBJECT_PREFIX) :]
                        if isinstance(name, str) and name.startswith(KNOWLEDGE_OBJECT_PREFIX)
                        else None
                    )
                    if (
                        isinstance(modified, datetime)
                        and as_utc(modified) < cutoff
                        and logical
                        and _KEY.fullmatch(logical)
                    ):
                        yield logical
        except _km.OriginalStorageError:
            raise
        except Exception as error:
            self._raise_mapped(error, operation="list")

    def _read_verified(self, key: str, expected_hash: str) -> bytes:
        data = self.object_client.read_sync(key, max_read=self._max_object_bytes + 1)
        if len(data) > self._max_object_bytes:
            raise _km.OriginalStorageIntegrityError("knowledge_storage_object_too_large")
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise _km.OriginalStorageIntegrityError("knowledge_storage_hash_mismatch")
        return data

    def _map_build_client_error(self, kind: StorageErrorKind) -> _km.OriginalStorageError:
        if kind is StorageErrorKind.SDK_MISSING:
            return _km.OriginalStorageConfigurationError("knowledge_storage_sdk_missing")
        return _km.OriginalStorageConfigurationError()


def _validate_key(key: str) -> str:
    if _KEY.fullmatch(key) is None:
        raise _km.OriginalStorageIntegrityError("knowledge_storage_key_invalid")
    return key.rsplit("/", 1)[-1]


def _validate_key_and_content(key: str, content: bytes) -> str:
    expected = _validate_key(key)
    if not isinstance(content, bytes):
        raise _km.OriginalStorageIntegrityError("knowledge_storage_content_invalid")
    if hashlib.sha256(content).hexdigest() != expected:
        raise _km.OriginalStorageIntegrityError("knowledge_storage_hash_mismatch")
    return expected


__all__ = ["KNOWLEDGE_OBJECT_PREFIX", "MinioOriginalStore", "MinioStorageConfig"]
