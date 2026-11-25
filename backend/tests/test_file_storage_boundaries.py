"""文件对象存储配置隔离与范围读取边界测试。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import aclosing
from typing import Any, BinaryIO, cast

import pytest
from minio.error import S3Error

from adapter.file.storage import (
    _EXECUTOR_MAX_WORKERS,
    _HTTP_POOL_MAXSIZE,
    MAX_CONCURRENT_RANGE_STREAMS,
    FileStorageError,
    MinioFileObjectStore,
    MinioFileStorageConfig,
)


class _Response:
    """模拟 MinIO 响应并对每次读取计数。"""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.offset = 0
        self.closed = False
        self.released = False

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            amount = len(self.content) - self.offset
        chunk = self.content[self.offset : self.offset + amount]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _Client:
    """按对象内容返回响应，或模拟 NoSuchKey。"""

    def __init__(self, content: bytes, *, missing: bool = False) -> None:
        self.content = content
        self.missing = missing
        self.calls: list[tuple[int, int | None]] = []
        self.response: _Response | None = None

    def get_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> _Response:
        del bucket_name, object_name
        self.calls.append((offset, length))
        if self.missing:
            raise S3Error(
                response=cast(Any, object()),
                code="NoSuchKey",
                message="missing",
                resource="/missing",
                request_id="request",
                host_id="host",
            )
        self.response = _Response(self.content[offset : offset + (length or len(self.content))])
        return self.response


def _store(client: _Client) -> MinioFileObjectStore:
    return MinioFileObjectStore(
        MinioFileStorageConfig(
            endpoint="minio.example.test:443",
            access_key="test-access",
            secret_key="test-secret",
            bucket="athena-files-test",
            secure=True,
        ),
        client=client,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("offset", [0, 7])
def test_get_range_missing_object_is_not_mistaken_for_eof(offset: int) -> None:
    client = _Client(b"", missing=True)

    with pytest.raises(FileStorageError) as error:
        asyncio.run(
            _store(client).get_range(
                "session-files/aa/missing",
                offset=offset,
                length=4,
            )
        )

    assert error.value.code == "file_storage_not_found"


def test_get_range_returns_empty_only_for_true_eof_and_closes_response() -> None:
    client = _Client(b"abc")

    content = asyncio.run(
        _store(client).get_range(
            "session-files/aa/short",
            offset=10,
            length=4,
        )
    )

    assert content == b""
    assert client.response is not None
    assert client.response.closed is True
    assert client.response.released is True


def test_file_storage_config_ignores_legacy_minio_env_vars() -> None:
    legacy_only = {
        "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "knowledge.example.test:9000",
        "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "knowledge-access",
        "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "knowledge-secret",
        "ATHENA_KNOWLEDGE_MINIO_BUCKET": "knowledge-bucket",
        "ATHENA_FILE_MINIO_ENDPOINT": "files.example.test:9000",
        "ATHENA_FILE_MINIO_ACCESS_KEY": "files-access",
        "ATHENA_FILE_MINIO_SECRET_KEY": "files-secret",
        "ATHENA_FILE_MINIO_BUCKET": "files-bucket",
    }

    with pytest.raises(FileStorageError, match="file_storage_configuration_invalid"):
        MinioFileStorageConfig.from_environment(legacy_only)


def test_file_storage_config_reads_unified_storage_variables() -> None:
    config = MinioFileStorageConfig.from_environment(
        {
            "ATHENA_STORAGE_ENDPOINT": "storage.example.test:9000",
            "ATHENA_STORAGE_ACCESS_KEY": "storage-access",
            "ATHENA_STORAGE_SECRET_KEY": "storage-secret",
            "ATHENA_STORAGE_BUCKET": "athena-shared-bucket",
            "ATHENA_STORAGE_SECURE": "false",
            "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "legacy.example.test:9000",
            "ATHENA_KNOWLEDGE_MINIO_BUCKET": "legacy-bucket",
        }
    )

    assert config.endpoint == "storage.example.test:9000"
    assert config.access_key == "storage-access"
    assert config.secret_key == "storage-secret"
    assert config.bucket == "athena-shared-bucket"
    assert config.secure is False
    assert "storage-secret" not in repr(config)


def test_direct_file_storage_config_construction_is_validated() -> None:
    """验证直接构造与非法参数异常映射，并防止回退为 Python 3.12 不兼容的无参 super()。"""
    # 防止 __post_init__ 重新捕获 __class__ 闭包导致 Python 3.12 @dataclass(slots=True) 实例化崩溃。
    assert "__class__" not in MinioFileStorageConfig.__post_init__.__code__.co_freevars

    config = MinioFileStorageConfig(
        endpoint="minio.example.test:9000",
        access_key="direct-access",
        secret_key="direct-secret",
        bucket="athena-files-test",
        secure=False,
    )
    assert config.endpoint == "minio.example.test:9000"
    assert config.access_key == "direct-access"
    assert config.secret_key == "direct-secret"
    assert config.bucket == "athena-files-test"
    assert config.secure is False
    assert "direct-access" not in repr(config)
    assert "direct-secret" not in repr(config)

    invalid_cases = (
        ("localhost:abc", "access", "secret", "test-bucket", True),
        ("https://minio.example", "access", "secret", "test-bucket", True),
        ("localhost:9000", "", "secret", "test-bucket", True),
        ("localhost:9000", "access", "   ", "test-bucket", True),
        ("localhost:9000", "access", "secret", "Invalid_Bucket", True),
    )
    for endpoint, access_key, secret_key, bucket, secure in invalid_cases:
        with pytest.raises(FileStorageError) as error:
            MinioFileStorageConfig(endpoint, access_key, secret_key, bucket, secure=secure)
        assert error.value.code == "file_storage_configuration_invalid"

    with pytest.raises(FileStorageError) as error:
        MinioFileStorageConfig(
            "localhost:9000",
            "access",
            "secret",
            "test-bucket",
            secure=1,  # type: ignore[arg-type]
        )
    assert error.value.code == "file_storage_configuration_invalid"


def test_file_storage_rejects_knowledge_prefix_keys() -> None:
    client = _Client(b"secret")
    store = _store(client)

    with pytest.raises(ValueError, match="file_storage_key_invalid"):
        asyncio.run(store.get_bytes("knowledge/aa/bb", maximum_bytes=16))
    with pytest.raises(ValueError, match="file_storage_key_invalid"):
        asyncio.run(store.put_bytes("knowledge/aa/bb", b"x", content_type="text/plain"))
    with pytest.raises(ValueError, match="file_storage_key_invalid"):
        asyncio.run(store.delete("knowledge/aa/bb"))


def test_build_client_configures_nonblocking_pool_above_concurrency_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接池上限必须大于流上限与线程池大小之和，且禁用 block 防止无超时永久阻塞。"""
    import minio
    import urllib3

    captured_pool_kwargs: dict[str, Any] = {}

    class _FakePoolManager:
        def __init__(self, **kwargs: Any) -> None:
            captured_pool_kwargs.update(kwargs)

        def clear(self) -> None:
            return None

    class _FakeMinio:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

    monkeypatch.setattr(urllib3, "PoolManager", _FakePoolManager)
    monkeypatch.setattr(minio, "Minio", _FakeMinio)

    store = MinioFileObjectStore(
        MinioFileStorageConfig(
            endpoint="minio.example.test:9000",
            access_key="test-access",
            secret_key="test-secret",
            bucket="athena-files-test",
            secure=False,
        )
    )
    try:
        _client, _pool = store._build_client()
    finally:
        asyncio.run(store.aclose())

    assert MAX_CONCURRENT_RANGE_STREAMS == 8
    assert _EXECUTOR_MAX_WORKERS == 8
    assert _HTTP_POOL_MAXSIZE == 24
    assert MAX_CONCURRENT_RANGE_STREAMS + _EXECUTOR_MAX_WORKERS < _HTTP_POOL_MAXSIZE
    assert captured_pool_kwargs["maxsize"] == _HTTP_POOL_MAXSIZE
    assert captured_pool_kwargs["block"] is False


def test_iter_range_concurrency_limit_prevents_deadlock_and_closes_on_cancel() -> None:
    """并发 10 路挂起 iter_range 叠加大量短操作时不发生 AB-BA 死锁，取消与正常结束均归还响应。"""

    class _TrackedResponse(_Response):
        def __init__(self, content: bytes, on_release: Any) -> None:
            super().__init__(content)
            self._on_release = on_release
            self._released_once = False

        def release_conn(self) -> None:
            super().release_conn()
            if not self._released_once:
                self._released_once = True
                self._on_release()

    class _PooledClient:
        """带活跃连接计数的模拟 MinIO 客户端，验证并发上限与零泄漏。"""

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.objects: dict[str, bytes] = {
                f"session-files/aa/stream_{idx}": f"chunk1-{idx}|chunk2-{idx}".encode()
                for idx in range(10)
            }
            self.objects["session-files/aa/short"] = b"short-payload"
            self.active_connections = 0
            self.peak_connections = 0
            self.active_stream_responses = 0
            self.peak_stream_responses = 0
            self.responses: list[_TrackedResponse] = []

        def _acquire_conn(self, *, is_stream: bool) -> None:
            with self._lock:
                self.active_connections += 1
                self.peak_connections = max(self.peak_connections, self.active_connections)
                if is_stream:
                    self.active_stream_responses += 1
                    self.peak_stream_responses = max(
                        self.peak_stream_responses, self.active_stream_responses
                    )

        def _release_conn(self, *, is_stream: bool) -> None:
            with self._lock:
                self.active_connections -= 1
                if is_stream:
                    self.active_stream_responses -= 1

        def get_object(
            self,
            bucket_name: str,
            object_name: str,
            *,
            offset: int = 0,
            length: int | None = None,
        ) -> _TrackedResponse:
            del bucket_name
            is_stream = object_name.startswith("session-files/aa/stream_")
            self._acquire_conn(is_stream=is_stream)
            payload = self.objects[object_name]
            sliced = payload[offset : None if length is None else offset + length]
            resp = _TrackedResponse(sliced, lambda: self._release_conn(is_stream=is_stream))
            with self._lock:
                self.responses.append(resp)
            return resp

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
            del bucket_name, content_type, num_parallel_uploads
            self._acquire_conn(is_stream=False)
            try:
                self.objects[object_name] = data.read(length)
                return object()
            finally:
                self._release_conn(is_stream=False)

        def remove_object(self, bucket_name: str, object_name: str) -> object:
            del bucket_name
            self._acquire_conn(is_stream=False)
            try:
                self.objects.pop(object_name, None)
                return object()
            finally:
                self._release_conn(is_stream=False)

    async def scenario() -> None:
        client = _PooledClient()
        store = _store(cast(Any, client))
        first_chunk_ready = asyncio.Event()
        opened_streams = 0
        release_streams = asyncio.Event()
        stream_results: dict[int, bytes] = {}

        async def consume_stream(index: int) -> None:
            nonlocal opened_streams
            chunks: list[bytes] = []
            async with aclosing(
                cast(Any, store.iter_range(f"session-files/aa/stream_{index}", chunk_size=7))
            ) as stream:
                async for chunk in stream:
                    chunks.append(chunk)
                    if len(chunks) == 1:
                        opened_streams += 1
                        if opened_streams == MAX_CONCURRENT_RANGE_STREAMS:
                            first_chunk_ready.set()
                        await release_streams.wait()
            stream_results[index] = b"".join(chunks)

        stream_tasks = [asyncio.create_task(consume_stream(idx)) for idx in range(10)]
        try:
            # 等待前 8 路流全部打开并挂起；第 9、10 路须在信号量排队，不得挤占线程池。
            await asyncio.wait_for(first_chunk_ready.wait(), timeout=2.0)
            await asyncio.sleep(0.05)
            assert opened_streams == MAX_CONCURRENT_RANGE_STREAMS
            assert client.peak_stream_responses == MAX_CONCURRENT_RANGE_STREAMS

            # 在 8 路长流全部挂起期间，并发执行大批短操作，必须在短超时内全部完成。
            short_ops = (
                [store.get_bytes("session-files/aa/short", maximum_bytes=64) for _ in range(8)]
                + [
                    store.put_bytes(
                        f"session-files/aa/tmp_{i}",
                        f"val-{i}".encode(),
                        content_type="text/plain",
                    )
                    for i in range(4)
                ]
                + [store.get_range("session-files/aa/short", offset=0, length=5) for _ in range(4)]
            )
            short_results = await asyncio.wait_for(asyncio.gather(*short_ops), timeout=2.0)
            assert short_results[:8] == [b"short-payload"] * 8
            assert short_results[12:] == [b"short"] * 4
            await asyncio.wait_for(
                asyncio.gather(*(store.delete(f"session-files/aa/tmp_{i}") for i in range(4))),
                timeout=2.0,
            )

            # 取消前 3 路挂起中的流，验证取消不会泄漏响应连接或信号量。
            for idx in range(3):
                stream_tasks[idx].cancel()
            cancelled_outcomes = await asyncio.wait_for(
                asyncio.gather(*stream_tasks[:3], return_exceptions=True),
                timeout=2.0,
            )
            assert all(isinstance(item, asyncio.CancelledError) for item in cancelled_outcomes)

            # 放行其余流（含原先排队的第 9、10 路），全部应在短超时内正常读完。
            release_streams.set()
            await asyncio.wait_for(asyncio.gather(*stream_tasks[3:]), timeout=2.0)

            for idx in range(3, 10):
                assert stream_results[idx] == f"chunk1-{idx}|chunk2-{idx}".encode()
            assert client.peak_stream_responses <= MAX_CONCURRENT_RANGE_STREAMS
            assert client.peak_connections < _HTTP_POOL_MAXSIZE
            assert client.active_connections == 0
            assert client.active_stream_responses == 0
            assert all(resp.closed and resp.released for resp in client.responses)
            # 验证信号量配额完整恢复：可再次并发获取全部 MAX_CONCURRENT_RANGE_STREAMS 个槽位。
            for _ in range(MAX_CONCURRENT_RANGE_STREAMS):
                await asyncio.wait_for(store._range_stream_semaphore.acquire(), timeout=0.5)
            for _ in range(MAX_CONCURRENT_RANGE_STREAMS):
                store._range_stream_semaphore.release()
        finally:
            release_streams.set()
            for task in stream_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*stream_tasks, return_exceptions=True)
            await store.aclose()

    asyncio.run(scenario())


def test_iter_range_closes_response_via_fallback_during_shutdown_executor_race() -> None:
    """停机竞态下执行器已关闭但 _started 仍为 True 时，取消流仍通过回退线程关闭响应。"""

    async def scenario() -> None:
        client = _Client(b"hello-world-payload")
        store = _store(client)
        first_chunk_seen = asyncio.Event()

        async def read_stream() -> None:
            async with aclosing(
                cast(Any, store.iter_range("session-files/aa/shutdown-race", chunk_size=5))
            ) as stream:
                async for chunk in stream:
                    assert chunk == b"hello"
                    first_chunk_seen.set()
                    await asyncio.Event().wait()

        task = asyncio.create_task(read_stream())
        try:
            await asyncio.wait_for(first_chunk_seen.wait(), timeout=2.0)
            assert client.response is not None
            assert client.response.closed is False
            assert client.response.released is False

            # 复现 aclose() 竞态窗口：有界线程池已关闭，但 _started/_client 尚未清空。
            await store._executor.aclose()
            assert store._started is True
            assert store._client is not None

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2.0)

            assert client.response.closed is True
            assert client.response.released is True
            # 确认信号量配额完整归还。
            for _ in range(MAX_CONCURRENT_RANGE_STREAMS):
                await asyncio.wait_for(store._range_stream_semaphore.acquire(), timeout=0.5)
            for _ in range(MAX_CONCURRENT_RANGE_STREAMS):
                store._range_stream_semaphore.release()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await store.aclose()

    asyncio.run(scenario())


def test_close_stream_response_does_not_retry_when_close_itself_raises() -> None:
    """若 _close_response 已经在有界线程池内执行并抛错，不再通过回退线程重复关闭。"""

    class _FailingCloseResponse(_Response):
        def __init__(self) -> None:
            super().__init__(b"x")
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("socket close failed")

    async def scenario() -> None:
        client = _Client(b"x")
        store = _store(client)
        response = _FailingCloseResponse()
        try:
            await store._close_stream_response(response)
            assert response.close_calls == 1
            assert response.released is True
        finally:
            await store.aclose()

    asyncio.run(scenario())
