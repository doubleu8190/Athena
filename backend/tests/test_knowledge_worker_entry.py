"""Knowledge worker entry-point resource lifecycle tests."""

from __future__ import annotations

import asyncio
from typing import Any

import knowledge_worker
import pytest

import bootstrap.database as bootstrap_database
import bootstrap.knowledge as bootstrap_knowledge
from domain.knowledge.model import OriginalStorageConfigurationError


def test_storage_start_failure_disposes_database_and_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDatabase:
        def __init__(self, _url: str, **_kwargs: Any) -> None:
            self.session_factory = object()
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    class FakeStore:
        instance: FakeStore

        def __init__(self) -> None:
            self.closed = False
            FakeStore.instance = self

        @classmethod
        def from_environment(cls) -> FakeStore:
            return cls()

        async def start(self) -> None:
            raise OriginalStorageConfigurationError("knowledge_storage_bucket_missing")

        async def aclose(self) -> None:
            self.closed = True

    databases: list[FakeDatabase] = []

    def database_factory(url: str, **_kwargs: Any) -> FakeDatabase:
        database = FakeDatabase(url)
        databases.append(database)
        return database

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setattr(bootstrap_database, "Database", database_factory)
    monkeypatch.setattr(bootstrap_knowledge, "MinioOriginalStore", FakeStore)

    with pytest.raises(OriginalStorageConfigurationError):
        asyncio.run(knowledge_worker.run_worker())

    assert databases[0].disposed
    assert FakeStore.instance.closed


def test_model_start_failure_closes_all_created_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeDatabase:
        def __init__(self, _url: str, **_kwargs: Any) -> None:
            self.session_factory = object()

        async def dispose(self) -> None:
            events.append("database")

    class FakeStore:
        @classmethod
        def from_environment(cls) -> FakeStore:
            return cls()

        async def start(self) -> None:
            events.append("store_start")

        async def aclose(self) -> None:
            events.append("store_close")

    class FakeEmbedding:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def start(self) -> None:
            events.append("embedding_start")
            raise RuntimeError("model unavailable")

        async def aclose(self) -> None:
            events.append("embedding_close")

    class FakeCounter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def start(self) -> None:
            events.append("counter_start")

        def close(self) -> None:
            events.append("counter_close")

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr(bootstrap_knowledge, "MinioOriginalStore", FakeStore)
    monkeypatch.setattr(bootstrap_knowledge, "OpenAICompatibleEmbeddingClient", FakeEmbedding)
    monkeypatch.setattr(bootstrap_knowledge, "TiktokenTokenCounter", FakeCounter)

    with pytest.raises(RuntimeError, match="model unavailable"):
        asyncio.run(knowledge_worker.run_worker())

    assert events == [
        "store_start",
        "embedding_start",
        "counter_close",
        "embedding_close",
        "store_close",
        "database",
    ]


def test_knowledge_worker_log_level_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证日志级别仅读取 ATHENA_LOG_LEVEL，忽略历史 LOG_LEVEL，默认 INFO。"""
    monkeypatch.delenv("ATHENA_LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert knowledge_worker._log_level() == "INFO"

    monkeypatch.setenv("LOG_LEVEL", "warning")
    assert knowledge_worker._log_level() == "INFO"

    monkeypatch.setenv("ATHENA_LOG_LEVEL", "debug")
    assert knowledge_worker._log_level() == "DEBUG"


def test_knowledge_worker_stop_event_cancels_consumers_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收到停机信号后取消消费协程，并按逆序释放 counter/embedding/storage/database。"""
    events: list[str] = []

    class FakeDatabase:
        def __init__(self, _url: str, **_kwargs: Any) -> None:
            self.session_factory = object()

        async def dispose(self) -> None:
            events.append("database")

    class FakeStore:
        @classmethod
        def from_environment(cls) -> FakeStore:
            return cls()

        async def start(self) -> None:
            events.append("store_start")

        async def aclose(self) -> None:
            events.append("store_close")

    class FakeEmbedding:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def start(self) -> None:
            events.append("embedding_start")

        async def aclose(self) -> None:
            events.append("embedding_close")

    class FakeCounter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def start(self) -> None:
            events.append("counter_start")

        def close(self) -> None:
            events.append("counter_close")

    consumer_started = asyncio.Event()

    async def fake_consumers(worker: Any, concurrency: int) -> None:
        del worker, concurrency
        consumer_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("consumers_cancelled")
            raise

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr(bootstrap_knowledge, "MinioOriginalStore", FakeStore)
    monkeypatch.setattr(bootstrap_knowledge, "OpenAICompatibleEmbeddingClient", FakeEmbedding)
    monkeypatch.setattr(bootstrap_knowledge, "TiktokenTokenCounter", FakeCounter)
    monkeypatch.setattr(knowledge_worker, "_run_consumers", fake_consumers)

    async def scenario() -> None:
        stop_event = asyncio.Event()
        runner = asyncio.create_task(knowledge_worker.run_worker(stop_event=stop_event))
        await asyncio.wait_for(consumer_started.wait(), timeout=2.0)
        stop_event.set()
        await asyncio.wait_for(runner, timeout=2.0)

    asyncio.run(scenario())

    assert events == [
        "store_start",
        "embedding_start",
        "counter_start",
        "consumers_cancelled",
        "counter_close",
        "embedding_close",
        "store_close",
        "database",
    ]


def test_knowledge_worker_pre_set_stop_event_skips_consumers_and_main_unbinds_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动期间提前收到停机信号时跳过消费循环；_main 退出时解绑信号处理器。"""
    import signal

    events: list[str] = []

    class FakeDatabase:
        def __init__(self, _url: str, **_kwargs: Any) -> None:
            self.session_factory = object()

        async def dispose(self) -> None:
            events.append("database")

    class FakeStore:
        @classmethod
        def from_environment(cls) -> FakeStore:
            return cls()

        async def start(self) -> None:
            events.append("store_start")

        async def aclose(self) -> None:
            events.append("store_close")

    class FakeEmbedding:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def start(self) -> None:
            events.append("embedding_start")

        async def aclose(self) -> None:
            events.append("embedding_close")

    class FakeCounter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def start(self) -> None:
            events.append("counter_start")

        def close(self) -> None:
            events.append("counter_close")

    async def fail_if_called(worker: Any, concurrency: int) -> None:
        del worker, concurrency
        events.append("consumers_started")

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setattr(bootstrap_knowledge, "MinioOriginalStore", FakeStore)
    monkeypatch.setattr(bootstrap_knowledge, "OpenAICompatibleEmbeddingClient", FakeEmbedding)
    monkeypatch.setattr(bootstrap_knowledge, "TiktokenTokenCounter", FakeCounter)
    monkeypatch.setattr(knowledge_worker, "_run_consumers", fail_if_called)

    async def scenario() -> None:
        pre_stopped = asyncio.Event()
        pre_stopped.set()
        await knowledge_worker.run_worker(stop_event=pre_stopped)

    asyncio.run(scenario())
    assert "consumers_started" not in events
    assert events == [
        "store_start",
        "embedding_start",
        "counter_start",
        "counter_close",
        "embedding_close",
        "store_close",
        "database",
    ]

    # 验证 _main 退出时对已安装信号调用 remove_signal_handler 解绑。
    installed: list[signal.Signals] = []
    removed: list[signal.Signals] = []

    async def main_scenario() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, *_a: installed.append(sig),
        )
        monkeypatch.setattr(
            loop,
            "remove_signal_handler",
            lambda sig: removed.append(sig) or True,
        )

        async def immediate_run(*, stop_event: asyncio.Event | None = None) -> None:
            assert stop_event is not None

        monkeypatch.setattr(knowledge_worker, "run_worker", immediate_run)
        await knowledge_worker._main()

    asyncio.run(main_scenario())
    assert installed == [signal.SIGINT, signal.SIGTERM]
    assert removed == [signal.SIGINT, signal.SIGTERM]
