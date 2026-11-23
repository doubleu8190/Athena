"""Worker-side orphan scanning and fenced upload cleanup tests."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from knowledge_fakes import MemoryOriginalStore

from app.knowledge.worker import KnowledgeWorker
from domain.knowledge.model import OriginalStorageUnavailableError
from domain.knowledge.ports import ClaimedKnowledgeJob, FileUploadReservation


def _key(content: bytes, source: str) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"{'a' * 64}/{source}/{digest[:2]}/{digest}"


class _MaintenanceRepository:
    def __init__(self) -> None:
        self.reservations: tuple[FileUploadReservation, ...] = ()
        self.referenced: set[str] = set()
        self.queries: list[str] = []
        self.finished: list[str] = []
        self.reference_error: Exception | None = None
        self.can_delete_error_tokens: set[str] = set()

    async def list_expired_upload_reservations(
        self, *, limit: int = 20
    ) -> tuple[FileUploadReservation, ...]:
        del limit
        return self.reservations

    async def delete_expired_upload_reservation(self, reservation_token: str) -> bool:
        if reservation_token in self.can_delete_error_tokens:
            raise RuntimeError("database unavailable while deleting reservation")
        match = next(
            (item for item in self.reservations if item.reservation_token == reservation_token),
            None,
        )
        if match is None or match.storage_key in self.referenced:
            return False
        self.reservations = tuple(
            item for item in self.reservations if item.reservation_token != reservation_token
        )
        self.finished.append(reservation_token)
        return True

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        self.queries.append(storage_key)
        if self.reference_error is not None:
            raise self.reference_error
        return storage_key in self.referenced or any(
            item.storage_key == storage_key for item in self.reservations
        )

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


def test_expired_reservation_failure_does_not_block_later_items() -> None:
    async def scenario() -> None:
        now = datetime(2026, 11, 9, tzinfo=UTC)
        first_key = _key(b"first", "ksrc_first")
        second_key = _key(b"second", "ksrc_second")
        store = MemoryOriginalStore(clock=lambda: now)

        async def fail_first_delete(
            operation: str,
            key: str | None,
            content: bytes | None,
        ) -> None:
            del content
            if operation == "delete" and key == first_key:
                raise OriginalStorageUnavailableError()

        store.operation_hook = fail_first_delete
        store.seed(first_key, b"first")
        store.seed(second_key, b"second")
        repository = _MaintenanceRepository()
        repository.reservations = (
            FileUploadReservation(
                "kres_1",
                first_key,
                "ksrc_first",
                "doc",
                "ver",
                "job",
                fencing_token=1,
            ),
            FileUploadReservation(
                "kres_2",
                second_key,
                "ksrc_second",
                "doc2",
                "ver2",
                "job2",
                fencing_token=1,
            ),
        )

        await _worker(store, repository)._sweep_expired_uploads()

        assert store.contains(first_key)
        assert not store.contains(second_key)
        assert repository.finished == ["kres_1", "kres_2"]

    asyncio.run(scenario())


def test_expired_reservation_database_failure_does_not_block_later_items() -> None:
    async def scenario() -> None:
        now = datetime(2026, 11, 9, tzinfo=UTC)
        first_key = _key(b"first", "ksrc_first")
        second_key = _key(b"second", "ksrc_second")
        store = MemoryOriginalStore(clock=lambda: now)
        store.seed(first_key, b"first")
        store.seed(second_key, b"second")
        repository = _MaintenanceRepository()
        repository.reservations = (
            FileUploadReservation(
                "kres_1",
                first_key,
                "ksrc_first",
                "doc",
                "ver",
                "job",
                fencing_token=1,
            ),
            FileUploadReservation(
                "kres_2",
                second_key,
                "ksrc_second",
                "doc2",
                "ver2",
                "job2",
                fencing_token=1,
            ),
        )
        repository.can_delete_error_tokens = {"kres_1"}

        await _worker(store, repository)._sweep_expired_uploads()

        assert store.contains(first_key)
        assert not store.contains(second_key)
        assert repository.finished == ["kres_2"]


def test_referenced_expired_reservation_keeps_object_and_row() -> None:
    async def scenario() -> None:
        key = _key(b"referenced", "ksrc_referenced")
        store = MemoryOriginalStore()
        store.seed(key, b"referenced")
        repository = _MaintenanceRepository()
        repository.referenced = {key}
        repository.reservations = (
            FileUploadReservation(
                "kres_ref",
                key,
                "ksrc_referenced",
                "doc",
                "ver",
                "job",
            ),
        )

        await _worker(store, repository)._sweep_expired_uploads()

        assert store.contains(key)
        assert repository.finished == []

    asyncio.run(scenario())

    asyncio.run(scenario())


def test_missing_reservation_object_counts_as_deleted() -> None:
    async def scenario() -> None:
        key = _key(b"missing", "ksrc_missing")
        store = MemoryOriginalStore()
        repository = _MaintenanceRepository()
        repository.reservations = (
            FileUploadReservation(
                "kres_1",
                key,
                "ksrc_missing",
                "doc",
                "ver",
                "job",
                fencing_token=1,
            ),
        )

        await _worker(store, repository)._sweep_expired_uploads()

        assert repository.finished == ["kres_1"]

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
    from collections.abc import Sequence

    class _BatchRepository(_MaintenanceRepository):
        def __init__(self) -> None:
            super().__init__()
            self.batch_calls: list[list[str]] = []
            self.reservation_checks = 0

        async def list_expired_upload_reservations(
            self, *, limit: int = 20
        ) -> tuple[FileUploadReservation, ...]:
            self.reservation_checks += 1
            return await super().list_expired_upload_reservations(limit=limit)

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

        # 第 100 次 run_once：执行过期预留回收 + 首次全桶孤儿扫描（分 100 + 5 两批查询）。
        for _ in range(100):
            assert await worker.run_once() is False
        assert repo.reservation_checks == 1
        assert [len(batch) for batch in repo.batch_calls] == [100, 5]
        assert all(store.contains(key) for key in referenced_keys)
        assert all(not store.contains(key) for key in orphan_keys)

        # 未满 3600s 再次触发维护（第 200 次 run_once）：仍清理过期预留，但跳过全桶孤儿扫描。
        late_orphan = _key(b"late-orphan", "ksrc_late")
        store.seed(late_orphan, b"late", modified_at=now - timedelta(days=2))
        clock[0] = 2000.0
        for _ in range(100):
            assert await worker.run_once() is False
        assert repo.reservation_checks == 2
        assert len(repo.batch_calls) == 2
        assert store.contains(late_orphan)

        # 超过 3600s（第 300 次 run_once）：再次执行孤儿扫描并回收新孤儿。
        clock[0] = 4601.0
        for _ in range(100):
            assert await worker.run_once() is False
        assert repo.reservation_checks == 3
        assert len(repo.batch_calls) == 3
        assert not store.contains(late_orphan)

    asyncio.run(scenario())


def test_knowledge_repository_find_referenced_storage_keys_checks_both_tables() -> None:
    """find_referenced_storage_keys 应以单次 UNION 查询同时命中来源表与上传预留表。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from adapter.auth.model import UserModel
    from adapter.base import Base
    from adapter.knowledge.model import (
        KnowledgeSourceModel,
        KnowledgeUploadReservationModel,
    )
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
                                KnowledgeUploadReservationModel.__table__,
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
                        role="user",
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
                session.add(
                    KnowledgeUploadReservationModel(
                        owner_id="usr_ref",
                        operation="create",
                        request_key="req_1",
                        reservation_token="tok_1",
                        request_hash="h" * 64,
                        base_id="kb_1",
                        document_id="doc_1",
                        version_id="ver_1",
                        source_id="ksrc_2",
                        job_id="job_1",
                        storage_key="key/from/reservation",
                        size_bytes=20,
                        state="reserved",
                        fencing_token=0,
                        created_at=now,
                        expires_at=now + timedelta(hours=1),
                    )
                )
            repo = KnowledgeRepository(factory)
            assert await repo.find_referenced_storage_keys([]) == set()
            found = await repo.find_referenced_storage_keys(
                ["key/from/source", "key/from/reservation", "key/orphan"]
            )
            assert found == {"key/from/source", "key/from/reservation"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())
