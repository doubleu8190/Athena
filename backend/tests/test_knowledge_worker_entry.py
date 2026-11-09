"""Knowledge worker entry-point resource lifecycle tests."""

from __future__ import annotations

import asyncio
from typing import Any

import knowledge_worker
import pytest

from domain.knowledge.model import OriginalStorageConfigurationError


def test_storage_start_failure_disposes_database_and_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDatabase:
        def __init__(self, _url: str) -> None:
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

    def database_factory(url: str) -> FakeDatabase:
        database = FakeDatabase(url)
        databases.append(database)
        return database

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setattr(knowledge_worker, "Database", database_factory)
    monkeypatch.setattr(knowledge_worker, "MinioOriginalStore", FakeStore)

    with pytest.raises(OriginalStorageConfigurationError):
        asyncio.run(knowledge_worker.run_worker())

    assert databases[0].disposed
    assert FakeStore.instance.closed


def test_model_start_failure_closes_all_created_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeDatabase:
        def __init__(self, _url: str) -> None:
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
    monkeypatch.setattr(knowledge_worker, "Database", FakeDatabase)
    monkeypatch.setattr(knowledge_worker, "MinioOriginalStore", FakeStore)
    monkeypatch.setattr(knowledge_worker, "LocalEmbedding", FakeEmbedding)
    monkeypatch.setattr(knowledge_worker, "LocalKnowledgeTokenCounter", FakeCounter)

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
