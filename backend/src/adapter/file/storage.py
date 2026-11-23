"""Skill 包、会话文件与执行日志共用的私有 MinIO 对象存储。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol, TypeVar, cast

from common.date import as_utc
from common.ids import now_utc
from domain.file import FileObjectScanEntry, FileObjectStore
from infrastructure.storage.config import (
    ObjectStorageConfig,
    ObjectStorageConfigError,
)

logger = logging.getLogger(__name__)

# 文件与技能域仅允许操作下列业务前缀，与同桶内 knowledge/ 严格隔离。
FILE_OBJECT_PREFIXES = (
    "session-files/",
    "session-artifacts/",
    "skill-logs/",
    "skills/",
)
_OBJECT_KEY = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,1022}$")
_ORPHAN_KEY = re.compile(
    r"^(?:"
    r"session-files/[0-9a-f]{2}/[A-Za-z0-9_-]{1,128}|"
    r"session-artifacts/[0-9a-f]{2}/[0-9a-f]{64}/[A-Za-z0-9_-]{1,128}|"
    r"skill-logs/[0-9a-f]{2}/[0-9a-f]{64}/[A-Za-z0-9_-]{1,128}\.log|"
    r"skills/[0-9a-f]{2}/[0-9a-f]{64}/[A-Za-z0-9_-]{1,128}\.zip"
    r")$"
)
_ResultT = TypeVar("_ResultT")
_LIST_PAGE_SIZE = 1000


class FileStorageError(RuntimeError):
    """对象存储失败只携带稳定的基础设施错误码。"""

    def __init__(self, code: str = "file_storage_unavailable") -> None:
        """保存安全错误码。"""
        super().__init__(code)
        self.code = code


class _HTTPResponse(Protocol):
    def read(self, amt: int | None = None) -> bytes: ...

    def close(self) -> None: ...

    def release_conn(self) -> None: ...


class _MinioClient(Protocol):
    def bucket_exists(self, bucket_name: str) -> bool: ...

    def get_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> _HTTPResponse: ...

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: BinaryIO,
        length: int,
        *,
        content_type: str,
        num_parallel_uploads: int,
    ) -> object: ...

    def remove_object(self, bucket_name: str, object_name: str) -> object: ...

    def list_objects(
        self,
        bucket_name: str,
        *,
        recursive: bool,
        start_after: str | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class MinioFileStorageConfig(ObjectStorageConfig):
    """基于统一 ATHENA_STORAGE_* 的文件/技能对象存储连接配置。"""

    def __post_init__(self) -> None:
        try:
            super().__post_init__()
        except ObjectStorageConfigError:
            raise FileStorageError("file_storage_configuration_invalid") from None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> MinioFileStorageConfig:
        """从统一 ATHENA_STORAGE_* 读取配置，并将非法配置映射为 FileStorageError。"""
        try:
            base = ObjectStorageConfig.from_environment(environ)
        except ObjectStorageConfigError:
            raise FileStorageError("file_storage_configuration_invalid") from None
        return cls(
            endpoint=base.endpoint,
            access_key=base.access_key,
            secret_key=base.secret_key,
            bucket=base.bucket,
            secure=base.secure,
        )


class _BoundedExecutor:
    """为同步 MinIO SDK 调用提供进程级有界线程池。"""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="athena-file-minio",
        )
        self._closed = False
        self._lock = threading.Lock()

    async def run(self, call: Callable[[], _ResultT]) -> _ResultT:
        """执行一次同步 SDK 调用。"""
        with self._lock:
            if self._closed:
                raise FileStorageError()
            future = self._executor.submit(call)
        return await asyncio.wrap_future(future)

    async def aclose(self) -> None:
        """等待正在进行的调用后关闭线程池。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        await asyncio.to_thread(
            self._executor.shutdown,
            wait=True,
            cancel_futures=True,
        )


class MinioFileObjectStore(FileObjectStore):
    """不可变对象写入、范围读取和幂等孤立对象删除。"""

    def __init__(
        self,
        config: ObjectStorageConfig | None = None,
        *,
        client: _MinioClient | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存配置并延迟构建 SDK 客户端。"""
        self._config = config
        self._environ = environ
        self._client = client
        self._pool: Any | None = None
        self._started = client is not None
        self._executor = _BoundedExecutor()
        self._clock = clock
        self._close_lock = threading.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    def from_environment(
        cls,
        *,
        deferred: bool = False,
    ) -> MinioFileObjectStore:
        """按部署环境构建对象存储，可将配置校验延迟到 lifespan start。"""
        return cls(
            None if deferred else MinioFileStorageConfig.from_environment(),
            environ=os.environ if deferred else None,
        )

    async def start(self) -> None:
        """确认私有 bucket 存在。"""
        if self._started and self._client is not None:
            return
        try:
            if self._config is None:
                self._config = MinioFileStorageConfig.from_environment(self._environ)
            if self._client is None:
                self._client, self._pool = self._build_client()
            # 延迟构建完成后必须先启用该实例，bucket 探测本身也要走统一的
            # 有界 SDK 调用和稳定错误映射。
            self._started = True
            exists = await self._call(
                lambda: cast(_MinioClient, self._client).bucket_exists(
                    self._configuration().bucket
                )
            )
            if not exists:
                raise FileStorageError("file_storage_bucket_missing")
        except BaseException:
            await self.aclose()
            raise

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str,
    ) -> None:
        """从临时文件写入对象；已存在的同键对象视为幂等成功。"""
        _validate_key(key)
        size = path.stat().st_size
        if size < 0:
            raise FileStorageError("file_storage_size_invalid")

        def store() -> None:
            with path.open("rb") as content:
                self._require_client().put_object(
                    self._configuration().bucket,
                    key,
                    content,
                    size,
                    content_type=content_type,
                    num_parallel_uploads=1,
                )

        await self._call(store)

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        """从有界内存写入对象。"""
        _validate_key(key)
        import io

        await self._call(
            lambda: self._require_client().put_object(
                self._configuration().bucket,
                key,
                io.BytesIO(content),
                len(content),
                content_type=content_type,
                num_parallel_uploads=1,
            )
        )

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        """读取不超过业务上限的完整对象。"""
        _validate_key(key)
        if maximum_bytes < 1:
            raise ValueError("file_storage_read_limit_invalid")

        def read() -> bytes:
            response: _HTTPResponse | None = None
            try:
                response = self._require_client().get_object(
                    self._configuration().bucket,
                    key,
                )
                content = response.read(maximum_bytes + 1)
                if len(content) > maximum_bytes:
                    raise FileStorageError("file_storage_object_too_large")
                return content
            finally:
                _close_response(response)

        return await self._call(read)

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        """读取精确范围；对象缺失始终失败，只有越过 EOF 才返回空字节串。"""
        _validate_key(key)
        if offset < 0 or length < 1:
            raise ValueError("file_storage_range_invalid")

        def read() -> bytes:
            response: _HTTPResponse | None = None
            try:
                response = self._require_client().get_object(
                    self._configuration().bucket,
                    key,
                    offset=offset,
                    length=length,
                )
                content = response.read(length)
                return content
            finally:
                _close_response(response)

        return await self._call(read)

    async def iter_range(
        self,
        key: str,
        *,
        offset: int = 0,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        """打开一次对象响应并分块交付到 EOF。"""
        _validate_key(key)
        if offset < 0 or not 1 <= chunk_size <= 1024 * 1024:
            raise ValueError("file_storage_range_invalid")
        response = await self._call(
            lambda: self._require_client().get_object(
                self._configuration().bucket,
                key,
                offset=offset,
            )
        )
        try:
            while True:
                chunk = await self._call(lambda: response.read(chunk_size))
                if not chunk:
                    break
                yield chunk
        finally:
            await self._call(lambda: _close_response(response))

    async def delete(self, key: str) -> None:
        """幂等删除对象。"""
        _validate_key(key)

        def remove() -> None:
            try:
                self._require_client().remove_object(
                    self._configuration().bucket,
                    key,
                )
            except Exception as error:
                if not _is_not_found(error):
                    raise

        await self._call(remove)

    async def iter_maintenance_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
        after_key: str | None = None,
        maximum_items: int = _LIST_PAGE_SIZE,
    ) -> AsyncIterator[FileObjectScanEntry]:
        """从本次离线扫描位置按有界页返回对象及其候选判定。"""
        if older_than_seconds < 86400:
            raise ValueError("file_maintenance_grace_period_invalid")
        if not 1 <= maximum_items <= _LIST_PAGE_SIZE:
            raise ValueError("file_maintenance_page_size_invalid")
        cutoff = as_utc(self._clock()).timestamp() - older_than_seconds
        cursor = after_key
        remaining = maximum_items
        while remaining > 0:
            requested = min(remaining, _LIST_PAGE_SIZE)

            def list_page(
                page_cursor: str | None = cursor,
            ) -> Iterator[object]:
                return iter(
                    cast(
                        Any,
                        self._require_client().list_objects(
                            self._configuration().bucket,
                            recursive=True,
                            start_after=page_cursor,
                        ),
                    )
                )

            iterator = await self._call(list_page)
            received = 0
            try:
                while remaining > 0:
                    def next_item(
                        object_iterator: Iterator[object] = iterator,
                    ) -> object | None:
                        return next(object_iterator, None)

                    item = await self._call(next_item)
                    if item is None:
                        return
                    key = getattr(item, "object_name", None)
                    if not isinstance(key, str) or not key:
                        raise FileStorageError("file_storage_listing_invalid")
                    cursor = key
                    received += 1
                    remaining -= 1
                    modified_at = getattr(item, "last_modified", None)
                    is_candidate = (
                        isinstance(modified_at, datetime)
                        and as_utc(modified_at).timestamp() < cutoff
                        and _ORPHAN_KEY.fullmatch(key) is not None
                    )
                    yield FileObjectScanEntry(
                        object_key=key,
                        is_candidate=is_candidate,
                    )
            finally:
                close = getattr(iterator, "close", None)
                if callable(close):
                    try:
                        await self._call(close)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
            if received < requested:
                return

    async def aclose(self) -> None:
        """等待执行线程与 HTTP 连接释放。"""
        with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    self._close(),
                    name="athena-file-minio-shutdown",
                )
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _close(self) -> None:
        await self._executor.aclose()
        if self._pool is not None:
            await asyncio.to_thread(self._pool.clear)
            self._pool = None
        self._client = None
        self._started = False

    def _build_client(self) -> tuple[_MinioClient, Any]:
        try:
            from minio import Minio
            from urllib3 import PoolManager
            from urllib3.util import Retry, Timeout
        except ImportError:
            raise FileStorageError("file_storage_sdk_missing") from None
        pool: Any | None = None
        try:
            timeout = Timeout(connect=5.0, read=30.0)
            retries = Retry(
                total=2,
                connect=2,
                read=2,
                status=2,
                redirect=0,
                other=0,
                backoff_factor=0.2,
                respect_retry_after_header=False,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=frozenset({"HEAD", "GET", "PUT", "DELETE"}),
            )
            kwargs: dict[str, Any] = {
                "maxsize": 4,
                "block": True,
                "timeout": timeout,
                "retries": retries,
            }
            configuration = self._configuration()
            if configuration.secure:
                context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
                context.verify_mode = ssl.CERT_REQUIRED
                context.check_hostname = True
                kwargs["ssl_context"] = context
            pool = PoolManager(**kwargs)
            client = Minio(
                configuration.endpoint,
                access_key=configuration.access_key,
                secret_key=configuration.secret_key,
                secure=configuration.secure,
                http_client=pool,
            )
        except Exception:
            if pool is not None:
                pool.clear()
            raise FileStorageError("file_storage_configuration_invalid") from None
        return cast(_MinioClient, client), pool

    async def _call(self, call: Callable[[], _ResultT]) -> _ResultT:
        if not self._started or self._client is None:
            raise FileStorageError()
        try:
            return await self._executor.run(call)
        except asyncio.CancelledError:
            raise
        except FileStorageError:
            raise
        except Exception as error:
            logger.warning(
                "file_storage_operation_failed code=%s exception_type=%s",
                _error_code(error),
                type(error).__name__,
            )
            raise FileStorageError(_error_code(error)) from None

    def _require_client(self) -> _MinioClient:
        if self._client is None:
            raise FileStorageError()
        return self._client

    def _configuration(self) -> ObjectStorageConfig:
        """读取已在 lifespan 中完成的配置，禁止使用未初始化存储。"""
        if self._config is None:
            raise FileStorageError("file_storage_not_started")
        return self._config


def _validate_key(key: str) -> None:
    """校验文件/技能对象键合法性并限定于本域业务前缀，避免跨前缀触碰 knowledge/ 对象。"""
    if (
        _OBJECT_KEY.fullmatch(key) is None
        or "//" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or not key.startswith(FILE_OBJECT_PREFIXES)
    ):
        raise ValueError("file_storage_key_invalid")


def _close_response(response: _HTTPResponse | None) -> None:
    if response is None:
        return
    try:
        response.close()
    finally:
        response.release_conn()


def _is_not_found(error: Exception) -> bool:
    try:
        from minio.error import S3Error
    except ImportError:
        return False
    return isinstance(error, S3Error) and error.code in {
        "NoSuchKey",
        "NoSuchObject",
        "NoSuchVersion",
    }


def _error_code(error: Exception) -> str:
    try:
        from minio.error import S3Error
    except ImportError:
        return "file_storage_unavailable"
    if isinstance(error, S3Error):
        if error.code in {"NoSuchKey", "NoSuchObject", "NoSuchVersion"}:
            return "file_storage_not_found"
        if error.code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
            return "file_storage_access_denied"
        if error.code == "NoSuchBucket":
            return "file_storage_bucket_missing"
    return "file_storage_unavailable"


__all__ = [
    "FILE_OBJECT_PREFIXES",
    "FileStorageError",
    "MinioFileObjectStore",
    "MinioFileStorageConfig",
]
