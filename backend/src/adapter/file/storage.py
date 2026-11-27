"""Skill 包、会话文件与执行日志共用的私有 MinIO 对象存储领域包装层。"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

from common.date import as_utc, utc_now
from domain.file import FileObjectScanEntry, FileObjectStore, FileStorageError
from infrastructure.storage import minio_client as _mc
from infrastructure.storage.config import ObjectStorageConfig, ObjectStorageConfigError
from infrastructure.storage.minio_client import (
    MinioObjectClient,
    MinioStoreAdapterBase,
    StorageErrorKind,
)

logger = logging.getLogger(__name__)
# 文件与技能域仅允许操作下列业务前缀，与同桶内 knowledge/ 严格隔离。
FILE_OBJECT_PREFIXES = (
    "session-files/",
    "session-files-extracted/",
    "session-artifacts/",
    "skill-logs/",
    "skills/",
)
_OBJECT_KEY = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,1022}$")
_ORPHAN_KEY = re.compile(
    r"^(?:session-files/[0-9a-f]{2}/[A-Za-z0-9_-]{1,128}|"
    r"session-files-extracted/[0-9a-f]{2}/[A-Za-z0-9_-]{1,128}\.txt|"
    r"session-artifacts/[0-9a-f]{2}/[0-9a-f]{64}/[A-Za-z0-9_-]{1,128}|"
    r"skill-logs/[0-9a-f]{2}/[0-9a-f]{64}/[A-Za-z0-9_-]{1,128}\.log|"
    r"skills/[0-9a-f]{2}/[0-9a-f]{64}/[A-Za-z0-9_-]{1,128}\.zip)$"
)
_LIST_PAGE_SIZE, MAX_CONCURRENT_RANGE_STREAMS = 1000, 8
_EXECUTOR_MAX_WORKERS, _HTTP_POOL_MAXSIZE = (
    _mc.SHARED_EXECUTOR_MAX_WORKERS,
    _mc.SHARED_HTTP_POOL_MAXSIZE,
)
_FILE_ERROR_FACTORIES: dict[StorageErrorKind, Callable[[], Exception]] = {
    StorageErrorKind.NOT_FOUND: partial(FileStorageError, "file_storage_not_found"),
    StorageErrorKind.ACCESS_DENIED: partial(FileStorageError, "file_storage_access_denied"),
    StorageErrorKind.BUCKET_MISSING: partial(FileStorageError, "file_storage_bucket_missing"),
    StorageErrorKind.SDK_MISSING: partial(FileStorageError, "file_storage_sdk_missing"),
    StorageErrorKind.CONFIGURATION_INVALID: partial(
        FileStorageError, "file_storage_configuration_invalid"
    ),
    StorageErrorKind.NOT_STARTED: partial(FileStorageError, "file_storage_not_started"),
}


@dataclass(frozen=True, slots=True)
class MinioFileStorageConfig(ObjectStorageConfig):
    """基于统一 ATHENA_STORAGE_* 的文件/技能对象存储连接配置。"""

    def __post_init__(self) -> None:
        try:
            # 显式调用父类校验，兼容 Python 3.12 @dataclass(slots=True) 类重建场景。
            ObjectStorageConfig.__post_init__(self)
        except ObjectStorageConfigError:
            raise FileStorageError("file_storage_configuration_invalid") from None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> MinioFileStorageConfig:
        try:
            b = ObjectStorageConfig.from_environment(environ)
        except ObjectStorageConfigError:
            raise FileStorageError("file_storage_configuration_invalid") from None
        return cls(b.endpoint, b.access_key, b.secret_key, b.bucket, b.secure)


class MinioFileObjectStore(MinioStoreAdapterBase, FileObjectStore):
    """不可变对象写入、范围读取和幂等孤立对象删除；共享生命周期骨架由基类统一管理。"""

    _domain_error_type = FileStorageError
    _error_factories = _FILE_ERROR_FACTORIES
    _default_error_type = FileStorageError
    _logger = logger
    _error_prefix = "file_storage"
    _client_name = "athena-file-minio"
    _auto_start_injected_client = True

    def __init__(
        self,
        config: ObjectStorageConfig | None = None,
        *,
        client: _mc._MinioClient | None = None,
        object_client: MinioObjectClient | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(
            config,
            client=client,
            object_client=object_client,
            environ=environ,
            clock=clock,
        )
        self._range_stream_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RANGE_STREAMS)

    @classmethod
    def from_environment(
        cls, *, deferred: bool = False, object_client: MinioObjectClient | None = None
    ) -> MinioFileObjectStore:
        """按部署环境构建对象存储；复用共享底层客户端时不传递冗余配置。"""
        if object_client is not None:
            return cls(object_client=object_client)
        return cls(
            None if deferred else MinioFileStorageConfig.from_environment(),
            environ=os.environ if deferred else None,
        )

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        _validate_key(key)
        if (size := path.stat().st_size) < 0:
            raise FileStorageError("file_storage_size_invalid")

        def store() -> None:
            with path.open("rb") as content:
                self.object_client.put_sync(key, content, size, content_type)

        await self._call(store)

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        _validate_key(key)
        await self._call(
            lambda: self.object_client.put_sync(
                key, io.BytesIO(content), len(content), content_type
            )
        )

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        _validate_key(key)
        if maximum_bytes < 1:
            raise ValueError("file_storage_read_limit_invalid")

        def read() -> bytes:
            data = self.object_client.read_sync(key, max_read=maximum_bytes + 1)
            if len(data) > maximum_bytes:
                raise FileStorageError("file_storage_object_too_large")
            return data

        return await self._call(read)

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        _validate_key(key)
        if offset < 0 or length < 1:
            raise ValueError("file_storage_range_invalid")
        return await self._call(
            lambda: self.object_client.read_sync(key, max_read=length, offset=offset, length=length)
        )

    async def iter_range(
        self, key: str, *, offset: int = 0, chunk_size: int = 64 * 1024
    ) -> AsyncIterator[bytes]:
        """打开一次对象响应并分块交付到 EOF；信号量限制并发长流避免 AB-BA 死锁。"""
        _validate_key(key)
        if offset < 0 or not 1 <= chunk_size <= 1024 * 1024:
            raise ValueError("file_storage_range_invalid")
        await self._range_stream_semaphore.acquire()
        response: _mc._HTTPResponse | None = None
        try:

            def open_stream() -> _mc._HTTPResponse:
                sdk, bucket = self.object_client.target()
                return sdk.get_object(bucket, key, offset=offset)

            response = await self._call(open_stream)
            active = response
            while chunk := await self._call(lambda: active.read(chunk_size)):
                yield chunk
        finally:
            try:
                if response is not None:
                    await asyncio.shield(self._close_stream_response(response))
            finally:
                self._range_stream_semaphore.release()

    async def _close_stream_response(self, response: _mc._HTTPResponse) -> None:
        """在受保护协程内关闭流式响应，防止取消或停机竞态下连接泄漏。"""
        closed_invoked = False

        def _close() -> None:
            nonlocal closed_invoked
            closed_invoked = True
            _mc.close_response(response)

        try:
            if self._started and self._client is not None:
                try:
                    await self._call(_close)
                    return
                except FileStorageError:
                    # 停机竞态下执行器已关闭但 _started 仍为 True；未执行回调则回退关闭。
                    if closed_invoked:
                        raise
            await asyncio.to_thread(_mc.close_response, response)
        except Exception:
            logger.warning("file_storage_stream_close_failed")

    async def delete(self, key: str) -> None:
        _validate_key(key)
        await self._call(lambda: self.object_client.delete_sync(key))

    async def iter_maintenance_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
        after_key: str | None = None,
        maximum_items: int = _LIST_PAGE_SIZE,
        prefix: str | None = None,
    ) -> AsyncIterator[FileObjectScanEntry]:
        """从离线扫描位置按有界页返回对象及其候选判定；已关闭包装层立即拒绝继续拉取。"""
        if older_than_seconds < 86400:
            raise ValueError("file_maintenance_grace_period_invalid")
        if not 1 <= maximum_items <= _LIST_PAGE_SIZE:
            raise ValueError("file_maintenance_page_size_invalid")
        if prefix is not None and prefix not in FILE_OBJECT_PREFIXES:
            raise ValueError("file_maintenance_prefix_invalid")
        cutoff, remaining = as_utc(self._clock()).timestamp() - older_than_seconds, maximum_items
        try:
            # 仅在显式指定非空业务前缀时透传 prefix，保持与无前缀扫描桩的 100% 兼容。
            stream = (
                self.object_client.iter_objects(
                    start_after=after_key,
                    is_closed=lambda: self._closed,
                )
                if prefix is None
                else self.object_client.iter_objects(
                    prefix=prefix,
                    start_after=after_key,
                    is_closed=lambda: self._closed,
                )
            )
            async with aclosing(stream) as items:
                async for item in items:
                    key = getattr(item, "object_name", None)
                    if not isinstance(key, str) or not key:
                        raise FileStorageError("file_storage_listing_invalid")
                    modified = getattr(item, "last_modified", None)
                    yield FileObjectScanEntry(
                        object_key=key,
                        is_candidate=(
                            isinstance(modified, datetime)
                            and as_utc(modified).timestamp() < cutoff
                            and _ORPHAN_KEY.fullmatch(key) is not None
                        ),
                    )
                    if (remaining := remaining - 1) == 0:
                        return
        except FileStorageError:
            raise
        except Exception as error:
            self._raise_mapped(error)


def _validate_key(key: str) -> None:
    if (
        _OBJECT_KEY.fullmatch(key) is None
        or "//" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or not key.startswith(FILE_OBJECT_PREFIXES)
    ):
        raise ValueError("file_storage_key_invalid")


__all__ = [
    "FILE_OBJECT_PREFIXES",
    "MAX_CONCURRENT_RANGE_STREAMS",
    "FileStorageError",
    "MinioFileObjectStore",
    "MinioFileStorageConfig",
]
