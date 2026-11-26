"""统一 MinIO 底层引擎生命周期、取消安全、关闭守卫与组合根共享所有权测试。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, BinaryIO, cast

import pytest
import run_worker as run_worker_entry
from minio.error import S3Error

import bootstrap.api as bootstrap_api
import bootstrap.database as bootstrap_database
import bootstrap.knowledge as bootstrap_knowledge
import bootstrap.run_worker as bootstrap_run_worker
import bootstrap.tool as bootstrap_tool
from adapter.file.storage import FileStorageError, MinioFileObjectStore
from adapter.knowledge.storage import KNOWLEDGE_OBJECT_PREFIX, MinioOriginalStore
from bootstrap.run_worker import RunWorkerBundle, build_run_worker_dependencies
from domain.knowledge.model import OriginalStorageUnavailableError
from infrastructure.embedding import EmbeddingConfigurationError
from infrastructure.storage import (
    SHARED_EXECUTOR_MAX_WORKERS,
    SHARED_HTTP_POOL_MAXSIZE,
    MinioClientError,
    MinioObjectClient,
    ObjectStorageConfig,
    ObjectStorageConfigError,
    StorageErrorKind,
    classify_storage_error,
    close_response,
)


def _s3_error(code: str, status: int = 400) -> S3Error:
    return S3Error(
        response=cast(Any, SimpleNamespace(status=status, data=b"")),
        code=code,
        message="test error",
        resource="/bucket/key",
        request_id="req",
        host_id="host",
        bucket_name="athena-shared",
        object_name="key",
    )


class _FakePoolManager:
    """记录构造参数与 clear() 次数，验证单池复用与释放边界。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.clear_calls = 0

    @property
    def cleared(self) -> bool:
        return self.clear_calls > 0

    def clear(self) -> None:
        self.clear_calls += 1


class _MemoryResponse:
    def __init__(self, payload: bytes) -> None:
        self._buf = io.BytesIO(payload)
        self.closed = False
        self.released = False

    def read(self, amt: int | None = None) -> bytes:
        return self._buf.read() if amt is None else self._buf.read(amt)

    def close(self) -> None:
        self.closed = True
        self._buf.close()

    def release_conn(self) -> None:
        self.released = True


class _FakeMinioSDK:
    """支持文件域与知识库域共同操作的内存 MinIO SDK 替身。"""

    def __init__(self, *, bucket_exists: bool = True) -> None:
        self.bucket_exists_result = bucket_exists
        self.bucket_check_calls = 0
        self.list_next_calls = 0
        self.objects: dict[str, bytes] = {}
        self.init_kwargs: dict[str, Any] = {}
        self.bucket_error: Exception | None = None

    def bucket_exists(self, bucket_name: str) -> bool:
        del bucket_name
        self.bucket_check_calls += 1
        if self.bucket_error is not None:
            raise self.bucket_error
        return self.bucket_exists_result

    def stat_object(self, bucket_name: str, object_name: str) -> SimpleNamespace:
        del bucket_name
        if object_name not in self.objects:
            raise _s3_error("NoSuchKey", 404)
        return SimpleNamespace(
            size=len(self.objects[object_name]),
            last_modified=datetime(2026, 11, 1, tzinfo=UTC),
        )

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
        self.objects[object_name] = data.read(length)
        return SimpleNamespace(object_name=object_name)

    def get_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> _MemoryResponse:
        del bucket_name
        if object_name not in self.objects:
            raise _s3_error("NoSuchKey", 404)
        raw = self.objects[object_name]
        sliced = raw[offset : None if length is None else offset + length]
        return _MemoryResponse(sliced)

    def remove_object(self, bucket_name: str, object_name: str) -> object:
        del bucket_name
        self.objects.pop(object_name, None)
        return SimpleNamespace(object_name=object_name)

    def list_objects(
        self,
        bucket_name: str,
        *,
        prefix: str | None = None,
        recursive: bool,
        start_after: str | None = None,
    ) -> Iterator[SimpleNamespace]:
        del bucket_name, recursive
        for name in sorted(self.objects):
            if prefix is not None and not name.startswith(prefix):
                continue
            if start_after is not None and name <= start_after:
                continue
            self.list_next_calls += 1
            yield SimpleNamespace(
                object_name=name,
                last_modified=datetime(2026, 11, 1, tzinfo=UTC),
            )


def _knowledge_key(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"{'a' * 64}/ksrc_shared/{digest[:2]}/{digest}"


def _sample_env() -> dict[str, str]:
    return {
        "ATHENA_STORAGE_ENDPOINT": "minio.internal:9000",
        "ATHENA_STORAGE_ACCESS_KEY": "shared-ak",
        "ATHENA_STORAGE_SECRET_KEY": "shared-sk",
        "ATHENA_STORAGE_BUCKET": "athena-shared",
        "ATHENA_STORAGE_SECURE": "false",
    }


def _patch_minio_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bucket_exists: bool = True,
    sdk_factory: Any = None,
) -> tuple[list[_FakePoolManager], list[_FakeMinioSDK]]:
    import minio
    import urllib3

    pools: list[_FakePoolManager] = []
    clients: list[_FakeMinioSDK] = []

    def make_pool(**kwargs: Any) -> _FakePoolManager:
        pool = _FakePoolManager(**kwargs)
        pools.append(pool)
        return pool

    def make_minio(*args: Any, **kwargs: Any) -> _FakeMinioSDK:
        sdk = (
            sdk_factory() if sdk_factory is not None else _FakeMinioSDK(bucket_exists=bucket_exists)
        )
        sdk.init_kwargs = {"args": args, **kwargs}
        clients.append(sdk)
        return sdk

    monkeypatch.setattr(urllib3, "PoolManager", make_pool)
    monkeypatch.setattr(minio, "Minio", make_minio)
    return pools, clients


def _install_common_bootstrap_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeDatabase:
        session_factory = cast(Any, object())

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def dispose(self) -> None:
            return None

    class _FakeEmbedding:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def start(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

    class _FakeCounter:
        async def start(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeTokenizer:
        def warmup(self) -> None:
            return None

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.delenv("ATHENA_REDIS_URL", raising=False)
    for name, value in _sample_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(bootstrap_database, "Database", _FakeDatabase)
    monkeypatch.setattr(bootstrap_knowledge, "OpenAICompatibleEmbeddingClient", _FakeEmbedding)
    monkeypatch.setattr(bootstrap_knowledge, "TiktokenTokenCounter", _FakeCounter)
    monkeypatch.setattr(bootstrap_knowledge, "JiebaChineseTokenizer", _FakeTokenizer)


async def _async_noop() -> None:
    return None


def test_minio_object_client_error_classification_and_close_response() -> None:
    """验证 S3/网络/配置异常分类以及 close_response 在 close 异常时仍归还连接。"""
    assert classify_storage_error(_s3_error("NoSuchKey", 404)) is StorageErrorKind.NOT_FOUND
    assert classify_storage_error(_s3_error("NoSuchObject", 404)) is StorageErrorKind.NOT_FOUND
    assert classify_storage_error(_s3_error("NoSuchVersion", 404)) is StorageErrorKind.NOT_FOUND
    assert classify_storage_error(_s3_error("AccessDenied", 403)) is StorageErrorKind.ACCESS_DENIED
    assert (
        classify_storage_error(_s3_error("AuthorizationHeaderMalformed", 403))
        is StorageErrorKind.ACCESS_DENIED
    )
    assert classify_storage_error(_s3_error("BadDigest", 400)) is StorageErrorKind.INTEGRITY
    assert (
        classify_storage_error(_s3_error("XAmzContentSHA256Mismatch", 400))
        is StorageErrorKind.INTEGRITY
    )
    assert classify_storage_error(_s3_error("NoSuchBucket", 404)) is StorageErrorKind.BUCKET_MISSING
    assert classify_storage_error(_s3_error("InternalError", 500)) is StorageErrorKind.UNAVAILABLE
    assert classify_storage_error(TimeoutError("timeout")) is StorageErrorKind.UNAVAILABLE
    assert (
        classify_storage_error(ObjectStorageConfigError()) is StorageErrorKind.CONFIGURATION_INVALID
    )
    assert (
        classify_storage_error(MinioClientError(StorageErrorKind.SDK_MISSING))
        is StorageErrorKind.SDK_MISSING
    )

    class _ExplodingCloseResponse(_MemoryResponse):
        def close(self) -> None:
            raise RuntimeError("socket close error")

    resp = _ExplodingCloseResponse(b"abc")
    with pytest.raises(RuntimeError, match="socket close error"):
        close_response(resp)
    assert resp.released is True


def test_minio_object_client_lazy_executor_deferred_start_and_idempotent_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证线程池延迟初始化、延迟配置校验、并发幂等启动、缺桶回滚与幂等关闭。"""
    pools, clients = _patch_minio_sdk(monkeypatch, bucket_exists=True)

    async def scenario() -> None:
        # 1. 构造阶段不创建 ThreadPoolExecutor，未启动即关闭亦不创建任何线程。
        idle = MinioObjectClient(ObjectStorageConfig.from_environment(_sample_env()))
        assert idle._executor._pool is None
        await idle.aclose()
        assert idle._executor._pool is None
        assert idle._closed is True

        # 2. 非法延迟配置仅在 start() 失败且零线程创建。
        bad = MinioObjectClient(None, environ={})
        with pytest.raises(MinioClientError) as bad_err:
            await bad.start()
        assert bad_err.value.kind is StorageErrorKind.CONFIGURATION_INVALID
        assert bad._executor._pool is None
        await bad.aclose()

        # 3. 合法延迟配置并发/重复 start() 只建 1 个池并只探测 1 次 bucket_exists。
        client = MinioObjectClient(None, environ=_sample_env())
        await asyncio.gather(client.start(), client.start())
        await client.start()
        assert len(pools) == 1
        assert len(clients) == 1
        assert clients[0].bucket_check_calls == 1
        assert client.started is True
        assert client._executor._pool is not None

        # 4. 幂等关闭释放连接池与线程池，关闭后拒绝新调用。
        await client.aclose()
        await client.aclose()
        assert client._closed is True
        assert client.started is False
        assert client._executor._closed is True
        assert pools[0].clear_calls == 1
        with pytest.raises(MinioClientError) as err:
            await client.run(lambda: 1)
        assert err.value.kind is StorageErrorKind.UNAVAILABLE

    asyncio.run(scenario())

    # 5. bucket_exists 返回 False（工作线程已退出）时立即清理本次新建连接池。
    missing_pools, _ = _patch_minio_sdk(monkeypatch, bucket_exists=False)

    async def missing_bucket_scenario() -> None:
        client = MinioObjectClient(ObjectStorageConfig.from_environment(_sample_env()))
        with pytest.raises(MinioClientError) as err:
            await client.start()
        assert err.value.kind is StorageErrorKind.BUCKET_MISSING
        assert len(missing_pools) == 1
        assert missing_pools[0].clear_calls == 1
        assert client.started is False
        await client.aclose()
        assert missing_pools[0].clear_calls == 1

    asyncio.run(missing_bucket_scenario())


def test_start_cancellation_does_not_clear_pool_while_bucket_probe_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start() 探测阻塞期间被取消不得抢先 pool.clear()；须等 aclose() 收敛后清理。"""

    class _BlockingProbeSDK(_FakeMinioSDK):
        def __init__(self) -> None:
            super().__init__(bucket_exists=True)
            self.probe_started = threading.Event()
            self.release_probe = threading.Event()
            self.probe_finished = False

        def bucket_exists(self, bucket_name: str) -> bool:
            del bucket_name
            self.probe_started.set()
            assert self.release_probe.wait(5)
            self.probe_finished = True
            return True

    pools, clients = _patch_minio_sdk(monkeypatch, sdk_factory=_BlockingProbeSDK)

    async def scenario() -> None:
        client = MinioObjectClient(ObjectStorageConfig.from_environment(_sample_env()))
        start_task = asyncio.create_task(client.start())
        while not clients:
            await asyncio.sleep(0)
        sdk = cast(_BlockingProbeSDK, clients[0])
        assert await asyncio.to_thread(sdk.probe_started.wait, 2)

        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

        # 探测线程仍在运行：此时严禁提前清理连接池。
        assert client.started is False
        assert sdk.probe_finished is False
        assert len(pools) == 1
        assert pools[0].cleared is False

        # aclose() 必须先等执行器中的探测线程退出，再清理连接池。
        close_task = asyncio.create_task(client.aclose())
        await asyncio.sleep(0.02)
        assert not close_task.done()
        assert pools[0].cleared is False

        sdk.release_probe.set()
        await asyncio.wait_for(close_task, timeout=2.0)
        assert sdk.probe_finished is True
        assert pools[0].clear_calls == 1
        assert client._executor._closed is True

    asyncio.run(scenario())


def test_minio_object_client_cancelled_aclose_converges_and_clears_pool() -> None:
    """首个关闭调用被取消后，共享关闭任务不被中止，后续等待方收敛并清理连接池。"""

    class _BlockingSDK(_FakeMinioSDK):
        def __init__(self) -> None:
            super().__init__(bucket_exists=True)
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = False

        def get_object(
            self,
            bucket_name: str,
            object_name: str,
            *,
            offset: int = 0,
            length: int | None = None,
        ) -> _MemoryResponse:
            del bucket_name, object_name, offset, length
            self.started.set()
            assert self.release.wait(5)
            self.finished = True
            return _MemoryResponse(b"ok")

    async def scenario() -> None:
        sdk = _BlockingSDK()
        pool = _FakePoolManager()
        client = MinioObjectClient(
            ObjectStorageConfig.from_environment(_sample_env()),
            sdk_client=cast(Any, sdk),
            max_workers=2,
            http_pool_maxsize=4,
        )
        client._http_pool = pool
        await client.start()

        in_flight = asyncio.create_task(client.run(lambda: client.read_sync("k", max_read=8)))
        assert await asyncio.to_thread(sdk.started.wait, 2)

        first_close = asyncio.create_task(client.aclose())
        while client._close_task is None:
            await asyncio.sleep(0)
        first_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_close

        assert not sdk.finished
        assert not pool.cleared

        second_close = asyncio.create_task(client.aclose())
        await asyncio.sleep(0)
        assert not second_close.done()

        sdk.release.set()
        await asyncio.wait_for(second_close, timeout=2.0)
        assert await in_flight == b"ok"
        assert sdk.finished is True
        assert pool.clear_calls == 1
        assert client._executor._closed is True

    asyncio.run(scenario())


def test_shared_wrappers_ownership_closed_iteration_guard_config_conflict_and_start_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证共享所有权、单方关闭中止迭代守卫、配置冲突校验与 file start 异常告警日志。"""
    pools, clients = _patch_minio_sdk(monkeypatch, bucket_exists=True)
    cfg = ObjectStorageConfig.from_environment(_sample_env())
    other_cfg = ObjectStorageConfig(
        endpoint="other.internal:9000",
        access_key="ak",
        secret_key="sk",
        bucket="other-bucket",
        secure=False,
    )

    async def scenario() -> None:
        shared = MinioObjectClient(cfg)
        with pytest.raises(ValueError, match="file_storage_config_conflict"):
            MinioFileObjectStore(other_cfg, object_client=shared)
        with pytest.raises(ValueError, match="knowledge_storage_config_conflict"):
            MinioOriginalStore(other_cfg, object_client=shared)

        file_store = MinioFileObjectStore(object_client=shared)
        knowledge_store = MinioOriginalStore(object_client=shared)
        assert file_store.object_client is shared
        assert knowledge_store.object_client is shared

        await file_store.start()
        await knowledge_store.start()
        assert len(pools) == 1
        assert clients[0].bucket_check_calls == 1

        k1_bytes, k2_bytes = b"knowledge-doc-1", b"knowledge-doc-2"
        k1, k2 = _knowledge_key(k1_bytes), _knowledge_key(k2_bytes)
        f1, f2 = "session-files/aa/file_1", "session-files/bb/file_2"
        await knowledge_store.put(k1, k1_bytes)
        await knowledge_store.put(k2, k2_bytes)
        await file_store.put_bytes(f1, b"f1", content_type="text/plain")
        await file_store.put_bytes(f2, b"f2", content_type="text/plain")

        # 1. 文件包装层迭代途中关闭：下次拉取立即抛 FileStorageError，不触碰共享池且知识库正常。
        file_iter = file_store.iter_maintenance_candidates(older_than_seconds=86400)
        first_file = await anext(file_iter)
        assert first_file.object_key
        calls_before_close = clients[0].list_next_calls
        await file_store.aclose()
        assert shared._closed is False
        assert pools[0].cleared is False
        with pytest.raises(FileStorageError):
            await anext(file_iter)
        assert clients[0].list_next_calls == calls_before_close

        # 2. 知识库包装层仍正常读写，且迭代途中关闭自身时立即抛 OriginalStorageUnavailableError。
        assert await knowledge_store.get(k1) == k1_bytes
        knowledge_iter = knowledge_store.iter_orphan_candidates(older_than_seconds=86400)
        first_knowledge = await anext(knowledge_iter)
        assert first_knowledge in {k1, k2}
        calls_before_k_close = clients[0].list_next_calls
        await knowledge_store.aclose()
        assert shared._closed is False
        assert pools[0].cleared is False
        with pytest.raises(OriginalStorageUnavailableError):
            await anext(knowledge_iter)
        assert clients[0].list_next_calls == calls_before_k_close

        # 3. 组合根在所有包装层之后统一释放共享底层客户端。
        await shared.aclose()
        assert shared._closed is True
        assert shared._executor._closed is True
        assert pools[0].clear_calls == 1
        assert f"{KNOWLEDGE_OBJECT_PREFIX}{k1}" in clients[0].objects

    asyncio.run(scenario())

    # 4. file_store.start() 遇 S3/传输异常时记录 file_storage_operation_failed warning。
    failing_sdk = _FakeMinioSDK(bucket_exists=True)
    failing_sdk.bucket_error = _s3_error("AccessDenied", 403)
    _patch_minio_sdk(monkeypatch, sdk_factory=lambda: failing_sdk)

    async def start_log_scenario() -> None:
        store = MinioFileObjectStore(cfg)
        with caplog.at_level(logging.WARNING, logger="adapter.file.storage"):
            with pytest.raises(FileStorageError) as err:
                await store.start()
        assert err.value.code == "file_storage_access_denied"
        assert any("file_storage_operation_failed" in rec.message for rec in caplog.records)

    asyncio.run(start_log_scenario())


def test_api_bootstrap_shares_single_minio_client_and_closes_in_reverse_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API 同时开启文件与知识库时共享单个底层客户端，并由 ApiDependencies.aclose() 逆序释放。"""
    _install_common_bootstrap_fakes(monkeypatch)
    pools, clients = _patch_minio_sdk(monkeypatch, bucket_exists=True)
    monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("ATHENA_FILES_ENABLED", "true")

    async def scenario() -> None:
        deps = bootstrap_api.build_api_dependencies()
        assert deps.storage_client is not None
        assert isinstance(deps.file_object_store, MinioFileObjectStore)
        assert isinstance(deps.knowledge_originals, MinioOriginalStore)
        assert deps.file_object_store.object_client is deps.storage_client
        assert deps.knowledge_originals.object_client is deps.storage_client
        assert deps.storage_client._executor.max_workers == SHARED_EXECUTOR_MAX_WORKERS
        assert deps.storage_client._executor._pool is None

        await deps.start()
        assert len(pools) == 1
        assert pools[0].kwargs["maxsize"] == SHARED_HTTP_POOL_MAXSIZE
        assert pools[0].kwargs["block"] is False
        assert len(clients) == 1
        assert clients[0].bucket_check_calls == 1

        events: list[str] = []
        orig_ko_close = deps.knowledge_originals.aclose
        orig_fs_close = deps.file_object_store.aclose
        orig_sc_close = deps.storage_client.aclose

        async def track_ko() -> None:
            events.append("knowledge_originals")
            await orig_ko_close()

        async def track_fs() -> None:
            events.append("file_object_store")
            await orig_fs_close()

        async def track_sc() -> None:
            assert pools[0].cleared is False
            events.append("storage_client")
            await orig_sc_close()

        monkeypatch.setattr(deps.knowledge_originals, "aclose", track_ko)
        monkeypatch.setattr(deps.file_object_store, "aclose", track_fs)
        monkeypatch.setattr(deps.storage_client, "aclose", track_sc)

        await deps.aclose()
        assert events == ["knowledge_originals", "file_object_store", "storage_client"]
        assert deps.storage_client._closed is True
        assert deps.storage_client._executor._closed is True
        assert pools[0].clear_calls == 1

    asyncio.run(scenario())


def test_run_worker_process_shares_single_minio_client_and_closes_in_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run Worker 进程同时开启文件与知识库时共享单个底层客户端，并在 finally 逆序统一释放。"""
    _install_common_bootstrap_fakes(monkeypatch)
    pools, clients = _patch_minio_sdk(monkeypatch, bucket_exists=True)
    monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("ATHENA_FILES_ENABLED", "true")

    class _ImmediateWorker:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def run(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    class _FakeProvider:
        async def aclose(self) -> None:
            return None

    captured_bundles: list[RunWorkerBundle] = []
    real_build = bootstrap_run_worker.build_run_worker_dependencies

    def capture_build(bundle: RunWorkerBundle, *, database_url: str | None = None) -> None:
        real_build(bundle, database_url=database_url)
        captured_bundles.append(bundle)

    monkeypatch.setattr(run_worker_entry, "prepare_run_tokenizers", _async_noop)
    monkeypatch.setattr(run_worker_entry, "build_run_worker_dependencies", capture_build)
    monkeypatch.setattr(
        bootstrap_run_worker,
        "build_mcp_executor",
        lambda **_kw: SimpleNamespace(aclose=_async_noop),
    )
    monkeypatch.setattr(bootstrap_run_worker, "OpenAIChatCompletionsAdapter", _FakeProvider)
    monkeypatch.setattr(bootstrap_run_worker, "RunExecutor", lambda *_a, **_kw: object())
    monkeypatch.setattr(bootstrap_run_worker, "RunWorker", _ImmediateWorker)

    asyncio.run(run_worker_entry.run_worker_process())

    assert len(captured_bundles) == 1
    bundle = captured_bundles[0]
    assert bundle.storage_client is not None
    assert isinstance(bundle.file_object_store, MinioFileObjectStore)
    assert bundle.query_services is not None
    assert isinstance(bundle.query_services.knowledge_original_store, MinioOriginalStore)
    assert bundle.file_object_store.object_client is bundle.storage_client
    assert bundle.query_services.knowledge_original_store.object_client is bundle.storage_client
    assert len(pools) == 1
    assert pools[0].kwargs["maxsize"] == SHARED_HTTP_POOL_MAXSIZE
    assert pools[0].kwargs["block"] is False
    assert len(clients) == 1
    assert clients[0].bucket_check_calls == 1
    assert bundle.storage_client._closed is True
    assert bundle.storage_client._executor._closed is True
    assert pools[0].clear_calls == 1


def test_api_and_run_worker_single_domain_disabled_and_injected_stub_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """覆盖 API 与 Run Worker 单域模式、双关模式及 API 注入替身（绝不创建孤儿共享客户端）。"""
    _install_common_bootstrap_fakes(monkeypatch)
    monkeypatch.setattr(
        bootstrap_run_worker,
        "build_mcp_executor",
        lambda **_kw: SimpleNamespace(aclose=_async_noop),
    )

    async def scenario() -> None:
        # 1. API & Run Worker 仅知识库模式（无共享客户端，独立 4 并发）。
        monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "true")
        monkeypatch.setenv("ATHENA_FILES_ENABLED", "false")
        deps_k = bootstrap_api.build_api_dependencies()
        assert deps_k.storage_client is None
        assert deps_k.file_object_store is None
        assert isinstance(deps_k.knowledge_originals, MinioOriginalStore)
        assert deps_k.knowledge_originals.object_client._executor.max_workers == 4
        await deps_k.aclose()

        rw_k = RunWorkerBundle()
        build_run_worker_dependencies(rw_k)
        assert rw_k.storage_client is None
        assert rw_k.file_object_store is None
        assert rw_k.query_services is not None
        assert isinstance(rw_k.query_services.knowledge_original_store, MinioOriginalStore)
        assert rw_k.query_services.knowledge_original_store.object_client._executor.max_workers == 4
        await rw_k.query_services.aclose()
        assert rw_k.provider is not None
        await rw_k.provider.aclose()

        # 2. API & Run Worker 仅文件/技能模式（无共享客户端，独立 8 并发）。
        monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "false")
        monkeypatch.setenv("ATHENA_FILES_ENABLED", "true")
        deps_f = bootstrap_api.build_api_dependencies()
        assert deps_f.storage_client is None
        assert deps_f.knowledge_originals is None
        assert isinstance(deps_f.file_object_store, MinioFileObjectStore)
        assert deps_f.file_object_store.object_client._executor.max_workers == 8
        await deps_f.aclose()

        rw_f = RunWorkerBundle()
        build_run_worker_dependencies(rw_f)
        assert rw_f.storage_client is None
        assert rw_f.query_services is not None
        assert rw_f.query_services.knowledge_original_store is None
        assert isinstance(rw_f.file_object_store, MinioFileObjectStore)
        assert rw_f.file_object_store.object_client._executor.max_workers == 8
        await rw_f.file_object_store.aclose()
        assert rw_f.provider is not None
        await rw_f.provider.aclose()

        # 3. API & Run Worker 双关模式。
        monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "false")
        monkeypatch.setenv("ATHENA_FILES_ENABLED", "false")
        deps_off = bootstrap_api.build_api_dependencies()
        assert deps_off.storage_client is None
        assert deps_off.file_object_store is None
        assert deps_off.knowledge_originals is None
        await deps_off.aclose()

        rw_off = RunWorkerBundle()
        build_run_worker_dependencies(rw_off)
        assert rw_off.storage_client is None
        assert rw_off.file_object_store is None
        assert rw_off.query_services is not None
        assert rw_off.query_services.knowledge_original_store is None
        assert rw_off.provider is not None
        await rw_off.provider.aclose()

        # 4. API 注入服务替身：不创建孤儿共享客户端。
        monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "true")
        monkeypatch.setenv("ATHENA_FILES_ENABLED", "true")
        deps_inj_k = bootstrap_api.build_api_dependencies(
            knowledge_service=cast(Any, object()),
        )
        assert deps_inj_k.storage_client is None
        assert isinstance(deps_inj_k.file_object_store, MinioFileObjectStore)
        assert deps_inj_k.file_object_store._owns_client is True
        await deps_inj_k.aclose()

        deps_inj_f = bootstrap_api.build_api_dependencies(
            skill_service=cast(Any, object()),
            session_file_service=cast(Any, object()),
        )
        assert deps_inj_f.storage_client is None
        assert deps_inj_f.file_object_store is None
        assert isinstance(deps_inj_f.knowledge_originals, MinioOriginalStore)
        assert deps_inj_f.knowledge_originals._owns_client is True
        await deps_inj_f.aclose()

    asyncio.run(scenario())


def test_mid_construction_failure_leaks_zero_thread_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构造中途发生配置异常时：bundle 已登记共享客户端且延迟线程池零创建、不构造原件存储。"""
    _install_common_bootstrap_fakes(monkeypatch)
    monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("ATHENA_FILES_ENABLED", "true")
    monkeypatch.delenv("ATHENA_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.setattr(
        bootstrap_run_worker,
        "build_mcp_executor",
        lambda **_kw: SimpleNamespace(aclose=_async_noop),
    )

    store_built = False

    def spy_original_store(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal store_built
        store_built = True
        raise AssertionError("original store must not be constructed when embedding config fails")

    monkeypatch.setattr(bootstrap_tool, "build_knowledge_original_store", spy_original_store)

    bundle = RunWorkerBundle()
    with pytest.raises(EmbeddingConfigurationError, match="embedding_base_url_missing"):
        build_run_worker_dependencies(bundle)

    assert store_built is False
    assert bundle.storage_client is not None
    assert bundle.storage_client._executor._pool is None
    asyncio.run(bundle.storage_client.aclose())
    assert bundle.storage_client._closed is True
    assert bundle.storage_client._executor._pool is None
