"""Worker 侧 24 小时宽限期孤儿对象扫描与批量引用核验测试。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from knowledge_fakes import MemoryOriginalStore

from app.knowledge.worker import KnowledgeWorker
from domain.knowledge.model import OriginalStorageUnavailableError
from domain.knowledge.ports import ClaimedKnowledgeJob


def _key(content: bytes, source: str) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"{'a' * 64}/{source}/{digest[:2]}/{digest}"


class _MaintenanceRepository:
    def __init__(self) -> None:
        self.referenced: set[str] = set()
        self.queries: list[str] = []
        self.reference_error: Exception | None = None

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        self.queries.append(storage_key)
        if self.reference_error is not None:
            raise self.reference_error
        return storage_key in self.referenced

    async def find_referenced_storage_keys(self, storage_keys: Sequence[str]) -> set[str]:
        referenced: set[str] = set()
        for key in storage_keys:
            if await self.is_storage_key_referenced(key):
                referenced.add(key)
        return referenced

    async def claim_job(
        self, *, lease_seconds: int, owner_concurrency: int = 2
    ) -> ClaimedKnowledgeJob | None:
        raise AssertionError("maintenance is invoked directly")


class _Unused:
    async def parse(self, filename: str, content: bytes) -> Any:
        raise AssertionError

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError

    def tokenize(self, text: str) -> tuple[str, ...]:
        raise AssertionError

    def count(self, text: str) -> int:
        raise AssertionError


def _worker(store: MemoryOriginalStore, repository: _MaintenanceRepository) -> KnowledgeWorker:
    return KnowledgeWorker(
        cast(Any, repository),
        cast(Any, _Unused()),
        store,
        _Unused(),
        _Unused(),
        _Unused(),
        isolate_parsing=False,
    )


def test_orphan_scan_rechecks_each_key_and_preserves_references_and_recent_objects() -> None:
    async def scenario() -> None:
        now = datetime(2026, 11, 9, tzinfo=UTC)
        store = MemoryOriginalStore(clock=lambda: now)
        referenced = _key(b"referenced", "ksrc_referenced")
        orphan = _key(b"orphan", "ksrc_orphan")
        recent = _key(b"recent", "ksrc_recent")
        store.seed(referenced, b"referenced", modified_at=now - timedelta(days=3))
        store.seed(orphan, b"orphan", modified_at=now - timedelta(days=3))
        store.seed(recent, b"recent", modified_at=now - timedelta(hours=2))
        repository = _MaintenanceRepository()
        repository.referenced = {referenced}

        await _worker(store, repository)._sweep_expired_uploads()

        assert store.contains(referenced)
        assert store.contains(recent)
        assert not store.contains(orphan)
        assert repository.queries == [orphan, referenced]

    asyncio.run(scenario())


def test_reference_lookup_failure_stops_deletion_without_using_an_empty_set() -> None:
    async def scenario() -> None:
        now = datetime(2026, 11, 9, tzinfo=UTC)
        store = MemoryOriginalStore(clock=lambda: now)
        orphan = _key(b"orphan", "ksrc_orphan")
        store.seed(orphan, b"orphan", modified_at=now - timedelta(days=2))
        repository = _MaintenanceRepository()
        repository.reference_error = RuntimeError("database unavailable")

        await _worker(store, repository)._sweep_expired_uploads()

        assert store.contains(orphan)
        assert store.delete_calls == []

    asyncio.run(scenario())


def test_reference_lookup_failure_closes_orphan_iterator() -> None:
    class ClosingStore(MemoryOriginalStore):
        closed = False

        async def iter_orphan_candidates(
            self, *, older_than_seconds: int = 86400
        ) -> AsyncIterator[str]:
            try:
                async for key in super().iter_orphan_candidates(
                    older_than_seconds=older_than_seconds
                ):
                    yield key
            finally:
                self.closed = True

    async def scenario() -> None:
        now = datetime(2026, 11, 9, tzinfo=UTC)
        store = ClosingStore(clock=lambda: now)
        orphan = _key(b"orphan", "ksrc_orphan")
        store.seed(orphan, b"orphan", modified_at=now - timedelta(days=2))
        repository = _MaintenanceRepository()
        repository.reference_error = RuntimeError("database unavailable")

        await _worker(store, repository)._sweep_expired_uploads()

        assert store.closed
        assert store.contains(orphan)
        assert store.delete_calls == []

    asyncio.run(scenario())


def test_listing_failure_never_deletes_candidates() -> None:
    class ListingFailureStore(MemoryOriginalStore):
        async def iter_orphan_candidates(
            self, *, older_than_seconds: int = 86400
        ) -> AsyncIterator[str]:
            del older_than_seconds
            raise OriginalStorageUnavailableError()
            yield ""

    async def scenario() -> None:
        store = ListingFailureStore()
        repository = _MaintenanceRepository()
        await _worker(store, repository)._sweep_expired_uploads()
        assert store.delete_calls == []
        assert repository.queries == []

    asyncio.run(scenario())


def test_orphan_scan_batches_reference_lookups_and_throttles_interval_in_run_once() -> None:
    """孤儿扫描优先按 100 条/页批量查库，且在 run_once 中按单调时钟周期降频执行。"""

    class _BatchRepository(_MaintenanceRepository):
        def __init__(self) -> None:
            super().__init__()
            self.batch_calls: list[list[str]] = []

        async def find_referenced_storage_keys(self, storage_keys: Sequence[str]) -> set[str]:
            batch = list(storage_keys)
            self.batch_calls.append(batch)
            return {key for key in batch if key in self.referenced}

        async def is_storage_key_referenced(self, storage_key: str) -> bool:
            raise AssertionError("batch lookup should be preferred over per-key query")

        async def claim_job(
            self, *, lease_seconds: int, owner_concurrency: int = 2
        ) -> ClaimedKnowledgeJob | None:
            del lease_seconds, owner_concurrency
            return None

    async def scenario() -> None:
        now = datetime(2026, 11, 9, tzinfo=UTC)
        store = MemoryOriginalStore(clock=lambda: now)
        referenced_keys: set[str] = set()
        orphan_keys: list[str] = []
        for idx in range(105):
            key = _key(f"payload-{idx}".encode(), f"ksrc_{idx:03d}")
            store.seed(key, b"payload", modified_at=now - timedelta(days=2))
            if idx % 10 == 0:
                referenced_keys.add(key)
            else:
                orphan_keys.append(key)

        repo = _BatchRepository()
        repo.referenced = referenced_keys
        clock = [1000.0]
        worker = KnowledgeWorker(
            cast(Any, repo),
            cast(Any, _Unused()),
            store,
            _Unused(),
            _Unused(),
            _Unused(),
            isolate_parsing=False,
            orphan_sweep_interval_seconds=3600.0,
            monotonic=lambda: clock[0],
        )

        # 第 100 次 run_once：执行首次全桶孤儿扫描（分 100 + 5 两批查询）。
        for _ in range(100):
            assert await worker.run_once() is False
        assert [len(batch) for batch in repo.batch_calls] == [100, 5]
        assert all(store.contains(key) for key in referenced_keys)
        assert all(not store.contains(key) for key in orphan_keys)

        # 未满 3600s 再次触发维护（第 200 次 run_once）：跳过全桶孤儿扫描。
        late_orphan = _key(b"late-orphan", "ksrc_late")
        store.seed(late_orphan, b"late", modified_at=now - timedelta(days=2))
        clock[0] = 2000.0
        for _ in range(100):
            assert await worker.run_once() is False
        assert len(repo.batch_calls) == 2
        assert store.contains(late_orphan)

        # 超过 3600s（第 300 次 run_once）：再次执行孤儿扫描并回收新孤儿。
        clock[0] = 4601.0
        for _ in range(100):
            assert await worker.run_once() is False
        assert len(repo.batch_calls) == 3
        assert not store.contains(late_orphan)

    asyncio.run(scenario())


def test_knowledge_repository_reference_lookups_check_sources_table() -> None:
    """is_storage_key_referenced 与 find_referenced_storage_keys 仅核验来源表。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from adapter.auth.model import UserModel
    from adapter.base import Base
    from adapter.knowledge.model import KnowledgeSourceModel
    from adapter.knowledge.repository import KnowledgeRepository

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync: Base.metadata.create_all(
                        sync,
                        tables=cast(
                            Any,
                            [
                                UserModel.__table__,
                                KnowledgeSourceModel.__table__,
                            ],
                        ),
                    )
                )
            factory = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(UTC)
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="usr_ref",
                        email="ref@example.test",
                        password_hash="unused",
                        display_name=None,
                        status="active",
                        role="member",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                session.add(
                    KnowledgeSourceModel(
                        id="ksrc_1",
                        owner_id="usr_ref",
                        storage_key="key/from/source",
                        filename="a.txt",
                        media_type="text/plain",
                        size_bytes=10,
                        sha256="a" * 64,
                        created_at=now,
                    )
                )
            repo = KnowledgeRepository(factory)
            assert await repo.is_storage_key_referenced("key/from/source") is True
            assert await repo.is_storage_key_referenced("key/orphan") is False
            assert await repo.find_referenced_storage_keys([]) == set()
            found = await repo.find_referenced_storage_keys(["key/from/source", "key/orphan"])
            assert found == {"key/from/source"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())
