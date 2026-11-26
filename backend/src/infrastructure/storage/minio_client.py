"""单桶 MinIO 共享底层引擎：统一延迟有界线程池、连接池、生命周期与 S3 错误分类。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from typing import Any, BinaryIO, Protocol, TypeVar, cast

from infrastructure.storage.config import ObjectStorageConfig, ObjectStorageConfigError
from infrastructure.storage.http import build_minio_http_pool

_ResultT = TypeVar("_ResultT")
# 进程内共享规格：文件流上限(8) + 有界线程池(8) < 连接池容量(24)，消除 AB-BA 死锁。
SHARED_EXECUTOR_MAX_WORKERS, SHARED_HTTP_POOL_MAXSIZE = 8, 24


class StorageErrorKind(StrEnum):
    """底层对象存储故障的稳定分类，由各领域包装层映射为独立领域异常。"""

    NOT_FOUND = "not_found"
    ACCESS_DENIED = "access_denied"
    INTEGRITY = "integrity"
    BUCKET_MISSING = "bucket_missing"
    SDK_MISSING = "sdk_missing"
    CONFIGURATION_INVALID = "configuration_invalid"
    NOT_STARTED = "not_started"
    UNAVAILABLE = "unavailable"


class MinioClientError(RuntimeError):
    """底层 MinIO 引擎结构化异常，仅暴露稳定错误分类，不透传凭据或底层细节。"""

    def __init__(self, kind: StorageErrorKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


class _HTTPResponse(Protocol):
    def read(self, amt: int | None = None) -> bytes: ...
    def close(self) -> None: ...
    def release_conn(self) -> None: ...


class _MinioClient(Protocol):
    def bucket_exists(self, bucket: str) -> bool: ...
    def stat_object(self, bucket: str, key: str) -> Any: ...
    def get_object(self, bucket: str, key: str, **kwargs: Any) -> _HTTPResponse: ...
    def put_object(self, b: str, k: str, d: BinaryIO, n: int, **kw: Any) -> object: ...
    def remove_object(self, bucket: str, key: str) -> object: ...
    def list_objects(self, bucket: str, **kwargs: Any) -> object: ...


def close_response(response: _HTTPResponse | None) -> None:
    """关闭 MinIO HTTP 响应并在 finally 中归还连接池连接。"""
    if response is None:
        return
    try:
        response.close()
    finally:
        response.release_conn()


def classify_storage_error(error: Exception) -> StorageErrorKind:
    """将底层引擎、配置与 S3/网络异常归类为稳定枚举，隔离领域错误码语义。"""
    if isinstance(error, MinioClientError):
        return error.kind
    if isinstance(error, ObjectStorageConfigError):
        return StorageErrorKind.CONFIGURATION_INVALID
    try:
        from minio.error import S3Error
    except ImportError:
        return StorageErrorKind.UNAVAILABLE
    if not isinstance(error, S3Error):
        return StorageErrorKind.UNAVAILABLE
    c = error.code or ""
    if c in {"NoSuchKey", "NoSuchObject", "NoSuchVersion"}:
        return StorageErrorKind.NOT_FOUND
    if c in {
        "AccessDenied",
        "AllAccessDisabled",
        "AuthorizationHeaderMalformed",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
    }:
        return StorageErrorKind.ACCESS_DENIED
    if c in {"BadDigest", "IncompleteBody", "InvalidDigest", "XAmzContentSHA256Mismatch"}:
        return StorageErrorKind.INTEGRITY
    return StorageErrorKind.BUCKET_MISSING if c == "NoSuchBucket" else StorageErrorKind.UNAVAILABLE


class _StorageExecutor:
    """按需延迟初始化的有界线程池，确保构造中途失败不遗留未关闭线程资源。"""

    def __init__(self, max_workers: int, prefix: str) -> None:
        self.max_workers, self._prefix = max_workers, prefix
        self._pool: ThreadPoolExecutor | None = None
        self._lock, self._closed = threading.Lock(), False

    async def run(self, call: Callable[[], _ResultT]) -> _ResultT:
        with self._lock:
            if self._closed:
                raise MinioClientError(StorageErrorKind.UNAVAILABLE)
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self.max_workers, thread_name_prefix=self._prefix
                )
            try:
                future = self._pool.submit(call)
            except RuntimeError:
                raise MinioClientError(StorageErrorKind.UNAVAILABLE) from None
        return await asyncio.wrap_future(future)

    async def aclose(self) -> None:
        with self._lock:
            self._closed, pool = True, self._pool
        if pool is not None:
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


class MinioObjectClient:
    """统一管理 MinIO SDK 客户端、HTTP 连接池、延迟有界线程池与抗取消关闭。

    所有权与共享 Redis 客户端一致：组合根共享时由组合根在包装层关闭后统一 ``aclose()``；
    单域包装层独占自建时由该包装层负责关闭。
    """

    def __init__(
        self,
        config: ObjectStorageConfig | None = None,
        *,
        sdk_client: _MinioClient | None = None,
        environ: Mapping[str, str] | None = None,
        max_workers: int = SHARED_EXECUTOR_MAX_WORKERS,
        http_pool_maxsize: int = SHARED_HTTP_POOL_MAXSIZE,
        http_pool_block: bool = False,
        name: str = "athena-storage-minio",
        auto_start_injected_client: bool = False,
    ) -> None:
        if max_workers < 1 or http_pool_maxsize < 1:
            raise ValueError("minio_storage_pool_invalid")
        self.config, self._environ, self._client = config, environ, sdk_client
        self._http_pool: Any | None = None
        self._pool_maxsize, self._pool_block = http_pool_maxsize, http_pool_block
        self._shutdown_name = f"{name}-shutdown"
        self._executor = _StorageExecutor(max_workers, name)
        self._lock, self._start_lock = threading.Lock(), asyncio.Lock()
        self._started = auto_start_injected_client and sdk_client is not None
        self._closed, self._close_task = False, cast(asyncio.Task[None] | None, None)

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started and not self._closed

    def target(self) -> tuple[_MinioClient, str]:
        if self.config is None:
            raise MinioClientError(StorageErrorKind.NOT_STARTED)
        with self._lock:
            if (client := self._client) is None:
                raise MinioClientError(StorageErrorKind.UNAVAILABLE)
        return client, self.config.bucket

    def build_sdk_client(self) -> tuple[_MinioClient, Any]:
        """延迟导入可选 MinIO SDK 并按配置构造绑定有界连接池的客户端。"""
        pool: Any | None = None
        try:
            from minio import Minio

            if (cfg := self.config) is None:
                raise MinioClientError(StorageErrorKind.NOT_STARTED)
            pool = build_minio_http_pool(
                maxsize=self._pool_maxsize, block=self._pool_block, secure=cfg.secure
            )
            sdk = Minio(
                cfg.endpoint,
                access_key=cfg.access_key,
                secret_key=cfg.secret_key,
                secure=cfg.secure,
                http_client=pool,
            )
            return cast(_MinioClient, sdk), pool
        except BaseException as error:
            if pool is not None:
                pool.clear()
            if isinstance(error, MinioClientError) or not isinstance(error, Exception):
                raise
            kind = (
                StorageErrorKind.SDK_MISSING
                if isinstance(error, ImportError)
                else StorageErrorKind.CONFIGURATION_INVALID
            )
            raise MinioClientError(kind) from None

    async def start(
        self, *, client_builder: Callable[[], tuple[_MinioClient, Any]] | None = None
    ) -> None:
        """幂等完成延迟配置校验、SDK/连接池构建与 bucket_exists 探测。"""
        async with self._start_lock:
            with self._lock:
                if self._closed:
                    raise MinioClientError(StorageErrorKind.UNAVAILABLE)
                if self._started and self._client is not None:
                    return
            built = False
            try:
                if self.config is None:
                    self.config = ObjectStorageConfig.from_environment(self._environ)
                if self._client is None:
                    sdk, pool = (client_builder or self.build_sdk_client)()
                    with self._lock:
                        self._client, self._http_pool, built = sdk, pool, True
                sdk_client, bucket = self.target()
                if not await self._executor.run(lambda: sdk_client.bucket_exists(bucket)):
                    raise MinioClientError(StorageErrorKind.BUCKET_MISSING)
                with self._lock:
                    if self._closed:
                        raise MinioClientError(StorageErrorKind.UNAVAILABLE)
                    self._started = True
            except asyncio.CancelledError:
                # 探测线程可能仍在途使用新建连接池，不得抢先清理；交由后续 aclose() 收敛。
                with self._lock:
                    self._started = False
                raise
            except Exception as error:
                with self._lock:
                    self._started = False
                    pool_to_clear = self._http_pool if built else None
                    if built:
                        self._http_pool = self._client = None
                if pool_to_clear is not None:
                    await asyncio.to_thread(pool_to_clear.clear)
                if isinstance(error, ObjectStorageConfigError):
                    raise MinioClientError(StorageErrorKind.CONFIGURATION_INVALID) from None
                raise

    async def run(self, call: Callable[[], _ResultT]) -> _ResultT:
        """确认已启动后在有界线程池中执行一次同步 SDK 调用。"""
        with self._lock:
            if not (self._started and not self._closed and self._client is not None):
                raise MinioClientError(StorageErrorKind.UNAVAILABLE)
        return await self._executor.run(call)

    def put_sync(self, key: str, data: BinaryIO, length: int, content_type: str) -> None:
        sdk, bucket = self.target()
        sdk.put_object(bucket, key, data, length, content_type=content_type, num_parallel_uploads=1)

    def read_sync(
        self, key: str, *, max_read: int, offset: int | None = None, length: int | None = None
    ) -> bytes:
        kw: dict[str, Any] = {} if offset is None and length is None else {"offset": offset or 0}
        if length is not None:
            kw["length"] = length
        response: _HTTPResponse | None = None
        try:
            sdk, bucket = self.target()
            response = sdk.get_object(bucket, key, **kw)
            return response.read(max_read)
        finally:
            close_response(response)

    def delete_sync(self, key: str) -> None:
        sdk, bucket = self.target()
        try:
            sdk.remove_object(bucket, key)
        except Exception as error:
            if classify_storage_error(error) is not StorageErrorKind.NOT_FOUND:
                raise

    async def iter_objects(
        self,
        *,
        prefix: str | None = None,
        start_after: str | None = None,
        is_closed: Callable[[], bool] | None = None,
    ) -> AsyncGenerator[Any, None]:
        """在有界线程池中惰性迭代桶内对象，每次拉取前校验包装层关闭守卫。"""
        if is_closed is not None and is_closed():
            raise MinioClientError(StorageErrorKind.UNAVAILABLE)
        kw: dict[str, Any] = {"recursive": True}
        if prefix is not None:
            kw["prefix"] = prefix
        if start_after is not None:
            kw["start_after"] = start_after

        def _open() -> Any:
            sdk, bucket = self.target()
            return iter(cast(Iterable[Any], sdk.list_objects(bucket, **kw)))

        it = await self.run(_open)
        try:
            while True:
                if is_closed is not None and is_closed():
                    raise MinioClientError(StorageErrorKind.UNAVAILABLE)
                if (item := await self.run(lambda: next(it, None))) is None:
                    return
                yield item
        finally:
            if callable(close := getattr(it, "close", None)):
                try:
                    await self.run(close)
                except Exception:
                    pass

    async def aclose(self) -> None:
        """拒收新调用、等待在途线程收敛后释放连接池；调用方取消不中止共享关闭。"""
        with self._lock:
            self._closed = True
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close(), name=self._shutdown_name)
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _close(self) -> None:
        await self._executor.aclose()
        with self._lock:
            pool, self._http_pool, self._client, self._started = self._http_pool, None, None, False
        if pool is not None:
            await asyncio.to_thread(pool.clear)


__all__ = [
    "MinioClientError",
    "MinioObjectClient",
    "SHARED_EXECUTOR_MAX_WORKERS",
    "SHARED_HTTP_POOL_MAXSIZE",
    "StorageErrorKind",
    "classify_storage_error",
    "close_response",
]
