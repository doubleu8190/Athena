"""MinIO-backed immutable knowledge original storage."""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import logging
import os
import re
import ssl
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, BinaryIO, Protocol, Self, TypeVar, cast
from urllib.parse import urlsplit

from common.date import as_utc, utc_now
from domain.knowledge.model import (
    MAX_ORIGINAL_BYTES,
    OriginalStorageAccessDeniedError,
    OriginalStorageConfigurationError,
    OriginalStorageError,
    OriginalStorageIntegrityError,
    OriginalStorageNotFoundError,
    OriginalStorageUnavailableError,
)
from domain.knowledge.ports import OriginalStore

logger = logging.getLogger(__name__)

_KEY = re.compile(
    r"^(?:[a-f0-9]{64}/[a-f0-9]{2}/[a-f0-9]{64}|"
    r"[a-f0-9]{64}/[a-z0-9_-]{1,64}/[a-f0-9]{2}/[a-f0-9]{64})$"
)
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_MAX_WORKERS = 4
_ResultT = TypeVar("_ResultT")


class _ObjectInfo(Protocol):
    last_modified: datetime | None


class _ListedObject(Protocol):
    object_name: str
    last_modified: datetime | None


class _HTTPResponse(Protocol):
    def read(self, amt: int | None = None) -> bytes: ...

    def close(self) -> None: ...

    def release_conn(self) -> None: ...


class _MinioClient(Protocol):
    def bucket_exists(self, bucket_name: str) -> bool: ...

    def stat_object(self, bucket_name: str, object_name: str) -> _ObjectInfo: ...

    def get_object(self, bucket_name: str, object_name: str) -> _HTTPResponse: ...

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

    def list_objects(self, bucket_name: str, *, recursive: bool) -> Iterator[_ListedObject]: ...


class _BoundedExecutor:
    """Run synchronous SDK calls with process-wide bounded concurrency."""

    def __init__(self, max_workers: int = _MAX_WORKERS) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="knowledge-minio",
        )
        self._lock = threading.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def run(self, call: Callable[[], _ResultT]) -> _ResultT:
        with self._lock:
            if self._closed:
                raise OriginalStorageUnavailableError()
            try:
                future = self._executor.submit(call)
            except RuntimeError:
                raise OriginalStorageUnavailableError() from None
        return await asyncio.wrap_future(future)

    async def aclose(self) -> None:
        with self._lock:
            self._closed = True
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._executor.shutdown,
                        wait=True,
                        cancel_futures=True,
                    ),
                    name="knowledge-minio-executor-shutdown",
                )
            close_task = self._close_task
        # A cancelled caller must not abandon or cancel the shared shutdown.
        await asyncio.shield(close_task)


@dataclass(frozen=True, slots=True)
class MinioStorageConfig:
    """Validated deployment-level MinIO connection settings."""

    endpoint: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    bucket: str
    secure: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str):
            raise OriginalStorageConfigurationError()
        _validate_endpoint(self.endpoint)
        if (
            not isinstance(self.access_key, str)
            or not self.access_key.strip()
            or not isinstance(self.secret_key, str)
            or not self.secret_key.strip()
        ):
            raise OriginalStorageConfigurationError()
        if not isinstance(self.bucket, str) or not _valid_bucket(self.bucket):
            raise OriginalStorageConfigurationError()
        if not isinstance(self.secure, bool):
            raise OriginalStorageConfigurationError()

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> MinioStorageConfig:
        source = os.environ if environ is None else environ
        endpoint = _required_setting(source, "ATHENA_KNOWLEDGE_MINIO_ENDPOINT")
        access_key = _required_setting(source, "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY")
        secret_key = _required_setting(source, "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY")
        bucket = _required_setting(source, "ATHENA_KNOWLEDGE_MINIO_BUCKET")
        return cls(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            secure=_strict_flag(
                source,
                "ATHENA_KNOWLEDGE_MINIO_SECURE",
                default=True,
            ),
        )


class MinioOriginalStore(OriginalStore):
    """Store content-addressed originals in one private MinIO bucket."""

    def __init__(
        self,
        config: MinioStorageConfig,
        *,
        client: _MinioClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        max_object_bytes: int = MAX_ORIGINAL_BYTES,
    ) -> None:
        if max_object_bytes < 1:
            raise ValueError("knowledge_storage_max_bytes_invalid")
        self._config = config
        self._client = client
        self._clock = clock
        self._max_object_bytes = max_object_bytes
        self._executor = _BoundedExecutor()
        self._http_pool: Any | None = None
        self._started = False
        self._close_lock = threading.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        client: _MinioClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        max_object_bytes: int = MAX_ORIGINAL_BYTES,
    ) -> Self:
        return cls(
            MinioStorageConfig.from_environment(environ),
            client=client,
            clock=clock,
            max_object_bytes=max_object_bytes,
        )

    async def start(self) -> None:
        """Build the SDK client and verify that the deployment bucket exists."""
        if self._started:
            return
        try:
            if self._client is None:
                self._client, self._http_pool = self._build_client()
            self._started = True
            exists = await self._call(
                "bucket_check",
                lambda: cast(_MinioClient, self._client).bucket_exists(self._config.bucket),
            )
            if not exists:
                raise OriginalStorageConfigurationError("knowledge_storage_bucket_missing")
        except BaseException:
            await self.aclose()
            raise

    async def put(self, key: str, content: bytes) -> None:
        """Store immutable bytes and reject a conflicting existing object."""
        expected_hash = _validate_key_and_content(key, content)
        if len(content) > self._max_object_bytes:
            raise OriginalStorageIntegrityError("knowledge_storage_object_too_large")

        def store() -> None:
            client = self._require_client()
            try:
                info = client.stat_object(self._config.bucket, key)
            except Exception as error:
                if _is_not_found_error(error):
                    info = None
                else:
                    raise
            if info is not None:
                existing = self._read_verified(client, key, expected_hash)
                if existing != content:
                    raise OriginalStorageIntegrityError("knowledge_storage_key_conflict")
                return
            client.put_object(
                self._config.bucket,
                key,
                io.BytesIO(content),
                len(content),
                content_type="application/octet-stream",
                num_parallel_uploads=1,
            )

        await self._call("put", store)

    async def get(self, key: str) -> bytes:
        """Read a bounded object and verify its bytes against the key."""
        expected_hash = _validate_key(key)
        return await self._call(
            "get",
            lambda: self._read_verified(self._require_client(), key, expected_hash),
        )

    async def delete(self, key: str) -> None:
        """Delete an object idempotently while preserving transport failures."""
        _validate_key(key)

        def remove() -> None:
            try:
                self._require_client().remove_object(self._config.bucket, key)
            except Exception as error:
                if _is_not_found_error(error):
                    return
                raise

        await self._call("delete", remove)

    async def iter_orphan_candidates(
        self, *, older_than_seconds: int = 86400
    ) -> AsyncIterator[str]:
        """Yield old legal keys from the dedicated bucket without reference checks."""
        if older_than_seconds < 86400:
            raise ValueError("knowledge_orphan_grace_period_invalid")
        cutoff = as_utc(self._clock()) - timedelta(seconds=older_than_seconds)
        iterator = await self._call(
            "list_start",
            lambda: iter(
                self._require_client().list_objects(
                    self._config.bucket,
                    recursive=True,
                )
            ),
        )
        try:
            while True:
                item = await self._call("list_next", lambda: _next_object(iterator))
                if item is None:
                    return
                modified_at = item.last_modified
                if (
                    modified_at is None
                    or as_utc(modified_at) >= cutoff
                    or _KEY.fullmatch(item.object_name) is None
                ):
                    continue
                yield item.object_name
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                try:
                    await self._call("list_close", close)
                except asyncio.CancelledError:
                    raise
                except OriginalStorageError:
                    pass
                except Exception as error:
                    logger.warning(
                        "knowledge_storage_operation_failed operation=list_close code=%s "
                        "exception_type=%s",
                        getattr(error, "code", "knowledge_storage_unavailable"),
                        type(error).__name__,
                    )

    async def aclose(self) -> None:
        """Stop new SDK calls, wait for running calls, and close pooled connections."""
        with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    self._close(),
                    name="knowledge-minio-store-shutdown",
                )
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _close(self) -> None:
        await self._executor.aclose()
        if self._http_pool is not None:
            await asyncio.to_thread(self._http_pool.clear)
            self._http_pool = None
        self._client = None
        self._started = False

    def _build_client(self) -> tuple[_MinioClient, Any]:
        try:
            from minio import Minio
            from urllib3 import PoolManager
            from urllib3.util import Retry, Timeout
        except ImportError:
            raise OriginalStorageConfigurationError("knowledge_storage_sdk_missing") from None

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
            if self._config.secure:
                context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
                context.verify_mode = ssl.CERT_REQUIRED
                context.check_hostname = True
                pool = PoolManager(
                    maxsize=_MAX_WORKERS,
                    block=True,
                    timeout=timeout,
                    retries=retries,
                    ssl_context=context,
                )
            else:
                pool = PoolManager(
                    maxsize=_MAX_WORKERS,
                    block=True,
                    timeout=timeout,
                    retries=retries,
                )
            client = Minio(
                self._config.endpoint,
                access_key=self._config.access_key,
                secret_key=self._config.secret_key,
                secure=self._config.secure,
                http_client=pool,
            )
        except Exception:
            if pool is not None:
                pool.clear()
            raise OriginalStorageConfigurationError() from None
        return cast(_MinioClient, client), pool

    def _read_verified(
        self,
        client: _MinioClient,
        key: str,
        expected_hash: str,
    ) -> bytes:
        response: _HTTPResponse | None = None
        try:
            response = client.get_object(self._config.bucket, key)
            content = response.read(self._max_object_bytes + 1)
            if len(content) > self._max_object_bytes:
                raise OriginalStorageIntegrityError("knowledge_storage_object_too_large")
            if hashlib.sha256(content).hexdigest() != expected_hash:
                raise OriginalStorageIntegrityError("knowledge_storage_hash_mismatch")
            return content
        except Exception as error:
            if _is_not_found_error(error):
                raise OriginalStorageNotFoundError() from None
            raise
        finally:
            if response is not None:
                try:
                    response.close()
                finally:
                    response.release_conn()

    async def _call(
        self,
        operation: str,
        call: Callable[[], _ResultT],
    ) -> _ResultT:
        self._require_started()
        try:
            return await self._executor.run(call)
        except OriginalStorageError as error:
            logger.warning(
                "knowledge_storage_operation_failed operation=%s code=%s exception_type=%s",
                operation,
                error.code,
                type(error).__name__,
            )
            raise
        except Exception as error:
            mapped = _map_storage_error(error)
            logger.warning(
                "knowledge_storage_operation_failed operation=%s code=%s exception_type=%s",
                operation,
                mapped.code,
                type(error).__name__,
            )
            raise mapped from None

    def _require_client(self) -> _MinioClient:
        if self._client is None:
            raise OriginalStorageUnavailableError()
        return self._client

    def _require_started(self) -> None:
        if not self._started:
            raise OriginalStorageUnavailableError()


def _required_setting(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise OriginalStorageConfigurationError()
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
    raise OriginalStorageConfigurationError()


def _validate_endpoint(endpoint: str) -> None:
    """使用标准库 urllib.parse + ipaddress 解析 host[:port]，拒绝路径、凭据和非法端口。"""
    if (
        not endpoint
        or any(character.isspace() or ord(character) < 32 for character in endpoint)
        or any(character in "/\\?#@%" for character in endpoint)
    ):
        raise OriginalStorageConfigurationError() from None
    try:
        parsed = urlsplit(f"//{endpoint}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise OriginalStorageConfigurationError() from None
    if (
        not host
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise OriginalStorageConfigurationError()
    trailing = endpoint.rsplit("]", 1)[-1] if endpoint.startswith("[") else endpoint
    if (":" in trailing and port is None) or (port is not None and not 1 <= port <= 65535):
        raise OriginalStorageConfigurationError()
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        if host.replace(".", "").isdigit() and "." in host:
            raise OriginalStorageConfigurationError() from None
    if len(host) > 253 or any(_HOST_LABEL.fullmatch(label) is None for label in host.split(".")):
        raise OriginalStorageConfigurationError()


def _valid_bucket(bucket: str) -> bool:
    return _BUCKET.fullmatch(bucket) is not None and ".." not in bucket


def _validate_key(key: str) -> str:
    if _KEY.fullmatch(key) is None:
        raise OriginalStorageIntegrityError("knowledge_storage_key_invalid")
    return key.rsplit("/", 1)[-1]


def _validate_key_and_content(key: str, content: bytes) -> str:
    expected_hash = _validate_key(key)
    if not isinstance(content, bytes):
        raise OriginalStorageIntegrityError("knowledge_storage_content_invalid")
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise OriginalStorageIntegrityError("knowledge_storage_hash_mismatch")
    return expected_hash


def _next_object(iterator: Iterator[_ListedObject]) -> _ListedObject | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


def _is_not_found_error(error: Exception) -> bool:
    try:
        from minio.error import S3Error
    except ImportError:
        return False
    return isinstance(error, S3Error) and error.code in {
        "NoSuchKey",
        "NoSuchObject",
        "NoSuchVersion",
    }


def _map_storage_error(error: Exception) -> OriginalStorageError:
    try:
        from minio.error import (
            InvalidResponseError,
            MinioException,
            S3Error,
            ServerError,
        )
        from urllib3.exceptions import HTTPError
    except ImportError:
        if isinstance(error, OSError | TimeoutError):
            return OriginalStorageUnavailableError()
        return OriginalStorageUnavailableError()

    if isinstance(error, S3Error):
        code = error.code or ""
        if code in {"NoSuchKey", "NoSuchObject", "NoSuchVersion"}:
            return OriginalStorageNotFoundError()
        if code in {
            "AccessDenied",
            "AllAccessDisabled",
            "AuthorizationHeaderMalformed",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
        }:
            return OriginalStorageAccessDeniedError()
        if code in {
            "BadDigest",
            "IncompleteBody",
            "InvalidDigest",
            "XAmzContentSHA256Mismatch",
        }:
            return OriginalStorageIntegrityError()
        if code == "NoSuchBucket":
            return OriginalStorageConfigurationError("knowledge_storage_bucket_missing")
        return OriginalStorageUnavailableError()
    if isinstance(error, ServerError):
        return OriginalStorageUnavailableError()
    if isinstance(error, (InvalidResponseError, MinioException, HTTPError, OSError, TimeoutError)):
        return OriginalStorageUnavailableError()
    return OriginalStorageUnavailableError()


__all__ = ["MinioOriginalStore", "MinioStorageConfig"]
