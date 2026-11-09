"""MinIO original storage contract and bounded-concurrency tests."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
import sys
import threading
import time
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, cast

import pytest
from minio.error import S3Error

from adapter.knowledge.storage import MinioOriginalStore, MinioStorageConfig
from domain.knowledge.model import (
    OriginalStorageAccessDeniedError,
    OriginalStorageConfigurationError,
    OriginalStorageIntegrityError,
    OriginalStorageNotFoundError,
    OriginalStorageUnavailableError,
)


def _key(content: bytes, *, owner: str = "a" * 64, source: str = "ksrc_1") -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"{owner}/{source}/{digest[:2]}/{digest}"


def _s3_error(code: str, status: int = 404) -> S3Error:
    return S3Error(
        response=cast(Any, SimpleNamespace(status=status, data=b"")),
        code=code,
        message="safe test error",
        resource="/bucket/key",
        request_id="request",
        host_id="host",
        bucket_name="test-bucket",
        object_name="key",
    )


@dataclass
class _StoredObject:
    content: bytes
    modified_at: datetime


class _GetResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.offset = 0
        self.closed = False
        self.released = False

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            result = self._content[self.offset :]
            self.offset = len(self._content)
            return result
        result = self._content[self.offset : self.offset + amt]
        self.offset += len(result)
        return result

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class FakeMinioClient:
    def __init__(self) -> None:
        self.bucket_exists_result = True
        self.objects: dict[str, _StoredObject] = {}
        self.put_calls = 0
        self.remove_calls: list[str] = []
        self.list_calls = 0
        self.responses: list[_GetResponse] = []
        self.stat_error: Exception | None = None
        self.get_error: Exception | None = None
        self.put_error: Exception | None = None
        self.remove_error: Exception | None = None
        self.list_error: Exception | None = None
        self.list_items: list[SimpleNamespace] = []

    def bucket_exists(self, bucket_name: str) -> bool:
        del bucket_name
        return self.bucket_exists_result

    def stat_object(self, bucket_name: str, object_name: str) -> SimpleNamespace:
        del bucket_name
        if self.stat_error is not None:
            raise self.stat_error
        try:
            item = self.objects[object_name]
        except KeyError:
            raise _s3_error("NoSuchKey") from None
        return SimpleNamespace(size=len(item.content), last_modified=item.modified_at)

    def get_object(self, bucket_name: str, object_name: str) -> _GetResponse:
        del bucket_name
        if self.get_error is not None:
            raise self.get_error
        try:
            content = self.objects[object_name].content
        except KeyError:
            raise _s3_error("NoSuchKey") from None
        response = _GetResponse(content)
        self.responses.append(response)
        return response

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: BinaryIO,
        length: int,
        *,
        content_type: str,
        num_parallel_uploads: int,
    ) -> object:
        del bucket_name, content_type
        assert num_parallel_uploads == 1
        self.put_calls += 1
        if self.put_error is not None:
            raise self.put_error
        content = data.read(length)
        assert len(content) == length
        self.objects[object_name] = _StoredObject(content, datetime.now(UTC))
        return SimpleNamespace(object_name=object_name)

    def remove_object(self, bucket_name: str, object_name: str) -> object:
        del bucket_name
        self.remove_calls.append(object_name)
        if self.remove_error is not None:
            raise self.remove_error
        self.objects.pop(object_name, None)
        return SimpleNamespace(object_name=object_name)

    def list_objects(self, bucket_name: str, *, recursive: bool) -> Iterator[SimpleNamespace]:
        del bucket_name
        assert recursive is True
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        yield from self.list_items


def _store(
    client: FakeMinioClient,
    *,
    clock: Any = lambda: datetime(2026, 11, 9, tzinfo=UTC),
    max_object_bytes: int = 1024,
) -> MinioOriginalStore:
    return MinioOriginalStore(
        MinioStorageConfig("minio.example:9000", "access", "secret-value", "test-bucket"),
        client=cast(Any, client),
        clock=clock,
        max_object_bytes=max_object_bytes,
    )


def test_config_is_strict_and_hides_credentials() -> None:
    config = MinioStorageConfig.from_environment(
        {
            "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "minio.example:9000",
            "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "access",
            "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "top-secret",
            "ATHENA_KNOWLEDGE_MINIO_BUCKET": "knowledge-test",
        }
    )
    assert config.secure is True
    assert "top-secret" not in repr(config)
    with pytest.raises(OriginalStorageConfigurationError):
        MinioStorageConfig.from_environment(
            {
                "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "https://minio.example",
                "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "access",
                "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "secret",
                "ATHENA_KNOWLEDGE_MINIO_BUCKET": "test-bucket",
            }
        )
    with pytest.raises(OriginalStorageConfigurationError):
        MinioStorageConfig.from_environment(
            {
                "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "minio.example:9000",
                "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "access",
                "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "secret",
                "ATHENA_KNOWLEDGE_MINIO_BUCKET": "test-bucket",
                "ATHENA_KNOWLEDGE_MINIO_SECURE": "maybe",
            }
        )
    with pytest.raises(OriginalStorageConfigurationError):
        MinioStorageConfig.from_environment({})
    insecure = MinioStorageConfig.from_environment(
        {
            "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "127.0.0.1:9000",
            "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "access",
            "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "secret",
            "ATHENA_KNOWLEDGE_MINIO_BUCKET": "test-bucket",
            "ATHENA_KNOWLEDGE_MINIO_SECURE": "false",
        }
    )
    assert insecure.secure is False


@pytest.mark.parametrize(
    "endpoint",
    (
        "minio.example:not-a-port",
        "minio.example:",
        ":9000",
        "[::1",
        "bad_host:9000",
        "localhost:0",
        "localhost:65536",
        "bad..host:9000",
        "-bad.example:9000",
        "host:-1",
        "[::1]:",
        "[::1]extra",
        "https://minio.example",
        "user@minio.example:9000",
        "minio.example/path",
        "minio.example?query",
        "minio.example#fragment",
        "minio.example\\path",
        "minio example:9000",
    ),
)
def test_config_rejects_malformed_endpoint(endpoint: str) -> None:
    with pytest.raises(OriginalStorageConfigurationError):
        MinioStorageConfig.from_environment(
            {
                "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": endpoint,
                "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "access",
                "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "secret",
                "ATHENA_KNOWLEDGE_MINIO_BUCKET": "test-bucket",
            }
        )


@pytest.mark.parametrize(
    "endpoint",
    (
        "minio.example",
        "minio.example:9000",
        "localhost",
        "localhost:9000",
        "127.0.0.1",
        "127.0.0.1:9000",
        "[::1]",
        "[::1]:9000",
        "[2001:db8::1]:65535",
    ),
)
def test_config_accepts_strict_host_port_forms(endpoint: str) -> None:
    config = MinioStorageConfig.from_environment(
        {
            "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": endpoint,
            "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "access",
            "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "secret",
            "ATHENA_KNOWLEDGE_MINIO_BUCKET": "test-bucket",
        }
    )
    assert config.endpoint == endpoint


def test_direct_config_construction_is_validated() -> None:
    with pytest.raises(OriginalStorageConfigurationError):
        MinioStorageConfig("localhost:abc", "access", "secret", "test-bucket")
    with pytest.raises(OriginalStorageConfigurationError):
        MinioStorageConfig("localhost:9000", "", "secret", "test-bucket")
    with pytest.raises(OriginalStorageConfigurationError):
        MinioStorageConfig("localhost:9000", "access", "secret", "Invalid_Bucket")
    with pytest.raises(OriginalStorageConfigurationError):
        MinioStorageConfig("localhost:9000", "access", "secret", "test-bucket", secure=1)  # type: ignore[arg-type]


def test_start_checks_bucket_without_creating_it() -> None:
    async def scenario() -> None:
        client = FakeMinioClient()
        client.bucket_exists_result = False
        store = _store(client)
        with pytest.raises(OriginalStorageConfigurationError, match="bucket_missing"):
            await store.start()

    asyncio.run(scenario())


def test_put_get_delete_are_content_verified_and_idempotent() -> None:
    async def scenario() -> None:
        client = FakeMinioClient()
        store = _store(client)
        await store.start()
        content = b"knowledge source"
        key = _key(content)

        await store.put(key, content)
        await store.put(key, content)
        assert client.put_calls == 1
        assert await store.get(key) == content
        assert client.responses[-1].closed and client.responses[-1].released

        await store.delete(key)
        await store.delete(key)
        assert client.remove_calls == [key, key]
        with pytest.raises(OriginalStorageNotFoundError):
            await store.get(key)

    asyncio.run(scenario())


def test_existing_object_is_fully_compared_and_corruption_is_rejected() -> None:
    async def scenario() -> None:
        client = FakeMinioClient()
        store = _store(client)
        await store.start()
        content = b"trusted bytes"
        key = _key(content)
        client.objects[key] = _StoredObject(b"x" * len(content), datetime.now(UTC))

        with pytest.raises(OriginalStorageIntegrityError):
            await store.put(key, content)
        assert client.put_calls == 0
        assert client.responses[-1].closed and client.responses[-1].released


def test_existing_object_with_different_size_is_read_before_conflict() -> None:
    async def scenario() -> None:
        client = FakeMinioClient()
        store = _store(client)
        await store.start()
        content = b"trusted bytes"
        key = _key(content)
        client.objects[key] = _StoredObject(b"x", datetime.now(UTC))

        with pytest.raises(OriginalStorageIntegrityError):
            await store.put(key, content)
        assert client.put_calls == 0
        assert client.responses[-1].closed and client.responses[-1].released

    asyncio.run(scenario())


def test_invalid_key_and_hash_mismatch_are_rejected_before_io() -> None:
    async def scenario() -> None:
        client = FakeMinioClient()
        store = _store(client)
        await store.start()
        with pytest.raises(OriginalStorageIntegrityError, match="key_invalid"):
            await store.get("../private")
        key = _key(b"expected")
        with pytest.raises(OriginalStorageIntegrityError, match="hash_mismatch"):
            await store.put(key, b"different")
        assert client.put_calls == 0

    asyncio.run(scenario())


def test_get_is_bounded_and_releases_response_on_error() -> None:
    async def scenario() -> None:
        client = FakeMinioClient()
        store = _store(client, max_object_bytes=4)
        await store.start()
        content = b"toolarge"
        key = _key(content)
        client.objects[key] = _StoredObject(content, datetime.now(UTC))

        with pytest.raises(OriginalStorageIntegrityError, match="too_large"):
            await store.get(key)
        assert client.responses[-1].closed and client.responses[-1].released

    asyncio.run(scenario())


def test_put_rejects_content_above_the_read_limit_before_io() -> None:
    async def scenario() -> None:
        client = FakeMinioClient()
        store = _store(client, max_object_bytes=4)
        await store.start()

        with pytest.raises(OriginalStorageIntegrityError, match="too_large"):
            await store.put(_key(b"toolarge"), b"toolarge")
        assert client.put_calls == 0

    asyncio.run(scenario())


def test_access_denied_is_not_reported_as_missing() -> None:
    async def scenario() -> None:
        client = FakeMinioClient()
        store = _store(client)
        await store.start()
        content = b"private"
        client.get_error = _s3_error("AccessDenied", 403)
        with pytest.raises(OriginalStorageAccessDeniedError):
            await store.get(_key(content))

    asyncio.run(scenario())


def test_delete_treats_only_not_found_as_success() -> None:
    async def scenario() -> None:
        client = FakeMinioClient()
        store = _store(client)
        await store.start()
        key = _key(b"delete")
        client.remove_error = _s3_error("NoSuchKey")
        await store.delete(key)

        client.remove_error = TimeoutError()
        with pytest.raises(OriginalStorageUnavailableError):
            await store.delete(key)

    asyncio.run(scenario())


def test_orphan_listing_filters_age_and_legal_keys_and_requires_grace_period() -> None:
    async def scenario() -> None:
        now = datetime(2026, 11, 9, tzinfo=UTC)
        client = FakeMinioClient()
        old = _key(b"old", source="ksrc_old")
        recent = _key(b"recent", source="ksrc_recent")
        client.list_items = [
            SimpleNamespace(object_name=old, last_modified=now - timedelta(days=2)),
            SimpleNamespace(object_name=recent, last_modified=now - timedelta(hours=1)),
            SimpleNamespace(object_name="not-a-key", last_modified=now - timedelta(days=2)),
            SimpleNamespace(object_name=_key(b"none"), last_modified=None),
        ]
        store = _store(client, clock=lambda: now)
        await store.start()

        assert [key async for key in store.iter_orphan_candidates()] == [old]
        with pytest.raises(ValueError, match="grace_period"):
            async for _ in store.iter_orphan_candidates(older_than_seconds=60):
                raise AssertionError("must not yield")

    asyncio.run(scenario())


def test_orphan_iterator_closes_when_consumer_stops_early() -> None:
    class ClosingClient(FakeMinioClient):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def list_objects(self, bucket_name: str, *, recursive: bool) -> Iterator[SimpleNamespace]:
            del bucket_name
            assert recursive is True
            try:
                yield from self.list_items
            finally:
                self.closed = True

    async def scenario() -> None:
        now = datetime(2026, 11, 9, tzinfo=UTC)
        client = ClosingClient()
        client.list_items = [
            SimpleNamespace(
                object_name=_key(b"first", source="ksrc_first"),
                last_modified=now - timedelta(days=2),
            ),
            SimpleNamespace(
                object_name=_key(b"second", source="ksrc_second"),
                last_modified=now - timedelta(days=2),
            ),
        ]
        store = _store(client, clock=lambda: now)
        await store.start()

        iterator = cast(AsyncGenerator[str, None], store.iter_orphan_candidates())
        assert await anext(iterator) == client.list_items[0].object_name
        await iterator.aclose()
        assert client.closed

    asyncio.run(scenario())


def test_four_calls_run_concurrently_and_capacity_survives_cancellation() -> None:
    class BlockingClient(FakeMinioClient):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()
            self.four_started = threading.Event()
            self.active = 0
            self.maximum = 0
            self._lock = threading.Lock()

        def get_object(self, bucket_name: str, object_name: str) -> _GetResponse:
            del bucket_name, object_name
            with self._lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 4:
                    self.four_started.set()
            try:
                assert self.release.wait(5)
                return _GetResponse(b"")
            finally:
                with self._lock:
                    self.active -= 1

    async def scenario() -> None:
        client = BlockingClient()
        store = _store(client)
        await store.start()
        keys = [_key(f"content-{index}".encode()) for index in range(5)]
        tasks = [asyncio.create_task(store.get(key)) for key in keys[:4]]
        assert await asyncio.to_thread(client.four_started.wait, 2)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert client.maximum == 4

        fifth = asyncio.create_task(store.get(keys[4]))
        await asyncio.sleep(0.1)
        assert client.maximum == 4
        assert not fifth.done()
        client.release.set()
        with pytest.raises(OriginalStorageIntegrityError):
            await fifth

    asyncio.run(scenario())


def test_aclose_waits_for_running_sync_call() -> None:
    class BlockingClient(FakeMinioClient):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()
            self.started = threading.Event()
            self.finished = False

        def get_object(self, bucket_name: str, object_name: str) -> _GetResponse:
            del bucket_name, object_name
            self.started.set()
            assert self.release.wait(5)
            self.finished = True
            return _GetResponse(b"")

    async def scenario() -> None:
        client = BlockingClient()
        store = _store(client)
        await store.start()
        task = asyncio.create_task(store.get(_key(b"content")))
        assert await asyncio.to_thread(client.started.wait, 2)
        close_task = asyncio.create_task(store.aclose())

        async def wait_for_close_task() -> None:
            for _ in range(100):
                if store._close_task is not None:
                    return
                await asyncio.sleep(0)
            raise AssertionError("store close task was not created")

        await wait_for_close_task()
        await asyncio.sleep(0)
        assert not close_task.done()
        client.release.set()
        await close_task
        assert client.finished
        with pytest.raises(OriginalStorageIntegrityError):
            await task

    asyncio.run(scenario())


def _blocking_store() -> tuple[MinioOriginalStore, Any]:
    class BlockingClient(FakeMinioClient):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()
            self.started = threading.Event()
            self.finished = False

        def get_object(self, bucket_name: str, object_name: str) -> _GetResponse:
            del bucket_name, object_name
            self.started.set()
            assert self.release.wait(5)
            self.finished = True
            return _GetResponse(b"")

    client = BlockingClient()
    return _store(client), client


def test_cancelled_aclose_does_not_abandon_shared_shutdown_or_pool_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        store, client = _blocking_store()
        await store.start()
        get_task = asyncio.create_task(store.get(_key(b"content")))
        assert await asyncio.to_thread(client.started.wait, 2)
        first_waiter = asyncio.Event()
        second_waiter = asyncio.Event()
        waiter_count = 0
        original_shield = asyncio.shield

        def track_caller_shield(awaitable: Any) -> Any:
            nonlocal waiter_count
            if (
                isinstance(awaitable, asyncio.Task)
                and awaitable.get_name() == "knowledge-minio-store-shutdown"
            ):
                waiter_count += 1
                if waiter_count == 1:
                    first_waiter.set()
                elif waiter_count == 2:
                    second_waiter.set()
            return original_shield(awaitable)

        class FakePool:
            def __init__(self) -> None:
                self.cleared = False

            def clear(self) -> None:
                self.cleared = True

        pool = FakePool()
        store._http_pool = pool
        monkeypatch.setattr(asyncio, "shield", track_caller_shield)
        first_close = asyncio.create_task(store.aclose())
        await asyncio.wait_for(first_waiter.wait(), timeout=2)
        first_close.cancel()
        assert (await asyncio.gather(first_close, return_exceptions=True))[0].__class__ is (
            asyncio.CancelledError
        )

        second_close = asyncio.create_task(store.aclose())
        await asyncio.wait_for(second_waiter.wait(), timeout=2)
        assert not second_close.done()
        assert not client.finished
        assert not pool.cleared

        client.release.set()
        await asyncio.wait_for(second_close, timeout=2)
        assert client.finished
        assert pool.cleared
        assert get_task.cancelled() or get_task.done()

    asyncio.run(scenario())


def test_concurrent_aclose_calls_converge_after_sync_call_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        store, client = _blocking_store()
        await store.start()
        get_task = asyncio.create_task(store.get(_key(b"content")))
        assert await asyncio.to_thread(client.started.wait, 2)
        both_waiters = asyncio.Event()
        waiter_count = 0
        original_shield = asyncio.shield

        def track_caller_shield(awaitable: Any) -> Any:
            nonlocal waiter_count
            if (
                isinstance(awaitable, asyncio.Task)
                and awaitable.get_name() == "knowledge-minio-store-shutdown"
            ):
                waiter_count += 1
                if waiter_count == 2:
                    both_waiters.set()
            return original_shield(awaitable)

        monkeypatch.setattr(asyncio, "shield", track_caller_shield)
        first_close = asyncio.create_task(store.aclose())
        second_close = asyncio.create_task(store.aclose())
        await asyncio.wait_for(both_waiters.wait(), timeout=2)
        assert not first_close.done()
        assert not second_close.done()
        assert not client.finished

        client.release.set()
        await asyncio.wait_for(asyncio.gather(first_close, second_close), timeout=2)
        assert client.finished
        assert get_task.cancelled() or get_task.done()

    asyncio.run(scenario())


def test_cancelled_get_still_closes_and_releases_response_connection() -> None:
    class BlockingClient(FakeMinioClient):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()
            self.started = threading.Event()

        def get_object(self, bucket_name: str, object_name: str) -> _GetResponse:
            self.started.set()
            assert self.release.wait(5)
            return super().get_object(bucket_name, object_name)

    async def scenario() -> None:
        client = BlockingClient()
        store = _store(client)
        await store.start()
        content = b"cancelled content"
        key = _key(content)
        client.objects[key] = _StoredObject(content, datetime.now(UTC))
        task = asyncio.create_task(store.get(key))
        assert await asyncio.to_thread(client.started.wait, 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        client.release.set()

        deadline = time.monotonic() + 2
        while not client.responses and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert client.responses
        assert client.responses[0].closed
        assert client.responses[0].released

    asyncio.run(scenario())


def test_sdk_http_pool_has_bounded_timeout_retry_and_capacity() -> None:
    store = MinioOriginalStore(
        MinioStorageConfig("minio.example:9000", "access", "secret-value", "test-bucket")
    )
    _, pool = store._build_client()
    try:
        assert pool.connection_pool_kw["maxsize"] == 4
        assert pool.connection_pool_kw["block"] is True
        timeout = pool.connection_pool_kw["timeout"]
        assert timeout.connect_timeout == 5.0
        assert timeout.read_timeout == 30.0
        retries = pool.connection_pool_kw["retries"]
        assert retries.total == 2
        assert retries.respect_retry_after_header is False
        assert retries.allowed_methods == frozenset({"HEAD", "GET", "PUT", "DELETE"})
        assert "POST" not in retries.allowed_methods
    finally:
        pool.clear()


def test_tls_uses_system_trust_store_and_requires_certificates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []
    original = ssl.create_default_context

    def spy(*, cafile: str | None = None) -> ssl.SSLContext:
        calls.append(cafile)
        return original(cafile=cafile)

    monkeypatch.setattr(ssl, "create_default_context", spy)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    store = MinioOriginalStore(
        MinioStorageConfig("minio.example:9000", "access", "secret-value", "test-bucket")
    )
    _, pool = store._build_client()
    try:
        assert calls == [None]
        context = pool.connection_pool_kw["ssl_context"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
    finally:
        pool.clear()


def test_tls_reads_explicit_ssl_cert_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = ssl.get_default_verify_paths().cafile
    if source is None or not Path(source).is_file():
        pytest.skip("no system CA bundle is available for the custom-CA test")
    ca_file = tmp_path / "private-ca.pem"
    ca_file.write_bytes(Path(source).read_bytes())
    calls: list[str | None] = []
    original = ssl.create_default_context

    def spy(*, cafile: str | None = None) -> ssl.SSLContext:
        calls.append(cafile)
        return original(cafile=cafile)

    monkeypatch.setattr(ssl, "create_default_context", spy)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    store = MinioOriginalStore(
        MinioStorageConfig("minio.example:9000", "access", "secret-value", "test-bucket")
    )
    _, pool = store._build_client()
    try:
        assert calls == [str(ca_file)]
        context = pool.connection_pool_kw["ssl_context"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
    finally:
        pool.clear()


def test_insecure_http_does_not_build_tls_context(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(**_kwargs: object) -> ssl.SSLContext:
        raise AssertionError("TLS must not be configured for controlled HTTP")

    monkeypatch.setattr(ssl, "create_default_context", unexpected)
    store = MinioOriginalStore(
        MinioStorageConfig(
            "minio.example:9000",
            "access",
            "secret-value",
            "test-bucket",
            secure=False,
        )
    )
    _, pool = store._build_client()
    try:
        assert "ssl_context" not in pool.connection_pool_kw
    finally:
        pool.clear()


def test_sdk_construction_errors_are_safe_configuration_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import minio

    def fail_construction(*_args: object, **_kwargs: object) -> object:
        raise ValueError("invalid port for private endpoint and credentials")

    monkeypatch.setattr(minio, "Minio", fail_construction)
    store = MinioOriginalStore(
        MinioStorageConfig("minio.example:9000", "access", "secret-value", "test-bucket")
    )
    with pytest.raises(OriginalStorageConfigurationError) as captured:
        store._build_client()
    assert "private endpoint" not in str(captured.value)


def test_missing_sdk_import_is_a_safe_configuration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "minio", None)
    store = MinioOriginalStore(
        MinioStorageConfig("minio.example:9000", "access", "secret-value", "test-bucket")
    )
    with pytest.raises(OriginalStorageConfigurationError, match="sdk_missing"):
        store._build_client()


def test_start_maps_missing_optional_sdk_and_releases_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        store = MinioOriginalStore(
            MinioStorageConfig("minio.example:9000", "access", "secret-value", "test-bucket")
        )

        def missing_sdk() -> tuple[Any, Any]:
            raise OriginalStorageConfigurationError("knowledge_storage_sdk_missing")

        monkeypatch.setattr(store, "_build_client", missing_sdk)
        with pytest.raises(OriginalStorageConfigurationError, match="sdk_missing"):
            await store.start()
        with pytest.raises(OriginalStorageUnavailableError):
            await store.get(_key(b"unreachable"))

    asyncio.run(scenario())
