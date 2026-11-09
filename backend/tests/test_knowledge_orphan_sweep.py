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

    async def claim_expired_upload_reservations(
        self, *, limit: int = 20, lease_seconds: int = 300
    ) -> tuple[FileUploadReservation, ...]:
        del limit, lease_seconds
        return self.reservations

    async def can_delete_expired_upload_reservation(
        self, reservation_token: str, fencing_token: int
    ) -> bool:
        if reservation_token in self.can_delete_error_tokens:
            raise RuntimeError("database unavailable while checking reservation")
        return any(
            item.reservation_token == reservation_token and item.fencing_token == fencing_token
            for item in self.reservations
        )

    async def finish_expired_upload_reservation(
        self, reservation_token: str, fencing_token: int
    ) -> bool:
        del fencing_token
        self.finished.append(reservation_token)
        return True

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        self.queries.append(storage_key)
        if self.reference_error is not None:
            raise self.reference_error
        return storage_key in self.referenced

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
        _Unused(),
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
        assert repository.finished == ["kres_2"]

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
