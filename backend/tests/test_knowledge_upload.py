"""Upload/service integration against the real local original store."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from knowledge_fakes import MemoryOriginalStore

from app.knowledge.service import KnowledgeService
from domain.knowledge.model import Document, KnowledgeBase
from domain.knowledge.ports import FileUploadReservation, KnowledgeRepositoryPort


class _UploadRepository:
    def __init__(self) -> None:
        self.bases: dict[str, KnowledgeBase] = {}
        self.requests: dict[tuple[str, str], tuple[str, Document, str]] = {}
        self.content_hashes: list[str] = []
        self.referenced: set[str] = set()
        self.storage_keys: list[str] = []
        self.reservations: dict[tuple[str, str, str], FileUploadReservation] = {}
        self.reservation_hashes: dict[tuple[str, str, str], str] = {}
        self._reserved_sizes: dict[str, int] = {}
        self._reservation_fences: dict[str, int] = {}
        self._expired_reservations: set[str] = set()
        self.reserve_calls = 0
        self.quota_limit_bytes = 1024 * 1024
        self.committed_sizes: dict[str, int] = {}
        self.fail_next = False
        self.fail_after_commit = False

    async def reserve_file_upload(self, **values: Any) -> FileUploadReservation:
        self.reserve_calls += 1
        request = (values["owner_id"], values["operation"], values["request_key"])
        existing = self.requests.get((values["owner_id"], values["request_key"]))
        if existing is not None:
            request_hash, document, version_id = existing
            if request_hash != values["request_hash"]:
                raise ValueError("idempotency_conflict")
            return FileUploadReservation(
                None, None, None, document.id, version_id, None, replay_document=document
            )
        previous = self.reservations.get(request)
        if previous is not None:
            if self.reservation_hashes[request] != values["request_hash"]:
                raise ValueError("upload_reservation_fenced")
            if previous.reservation_token in self._expired_reservations:
                raise ValueError("knowledge_upload_reservation_stale")
            return previous
        used_bytes = sum(self.committed_sizes.values()) + sum(self._reserved_sizes.values())
        if used_bytes + values["size_bytes"] > self.quota_limit_bytes:
            raise ValueError("knowledge_quota_exceeded")
        reservation = FileUploadReservation(
            values["reservation_token"],
            values["storage_key"],
            values["source_id"],
            values["document_id"],
            values["version_id"],
            values["job_id"],
        )
        self.reservations[request] = reservation
        self.reservation_hashes[request] = values["request_hash"]
        self._reserved_sizes[values["reservation_token"]] = values["size_bytes"]
        return reservation

    async def release_file_upload_reservation(self, token: str) -> None:
        self._reserved_sizes.pop(token, None)
        self.reservations = {
            request: reservation
            for request, reservation in self.reservations.items()
            if reservation.reservation_token != token
        }
        self.reservation_hashes = {
            request: request_hash
            for request, request_hash in self.reservation_hashes.items()
            if request in self.reservations
        }

    async def list_expired_upload_reservations(
        self, *, limit: int = 20
    ) -> tuple[FileUploadReservation, ...]:
        expired: list[FileUploadReservation] = []
        for reservation in self.reservations.values():
            token = reservation.reservation_token
            if token is None or token not in self._expired_reservations:
                continue
            expired.append(reservation)
            if len(expired) >= limit:
                break
        return tuple(expired)

    async def delete_expired_upload_reservation(self, reservation_token: str) -> bool:
        if reservation_token not in self._expired_reservations:
            return False
        await self.release_file_upload_reservation(reservation_token)
        self._expired_reservations.discard(reservation_token)
        return True

    def retained_storage_keys(self) -> set[str]:
        return self.referenced.union(
            reservation.storage_key
            for reservation in self.reservations.values()
            if reservation.storage_key is not None
        )

    async def create_base(self, item: KnowledgeBase) -> KnowledgeBase:
        self.bases[item.id] = item
        return item

    async def get_base(self, owner_id: str, base_id: str) -> KnowledgeBase | None:
        base = self.bases.get(base_id)
        return base if base and base.owner_id == owner_id else None

    async def create_file_document(self, **values: Any) -> tuple[Document, str]:
        req = (values["owner_id"], values["request_key"])
        previous = self.requests.get(req)
        if previous:
            request_hash, document, version_id = previous
            if request_hash != values["request_hash"]:
                raise ValueError("idempotency_conflict")
            return document, version_id
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated_database_failure")
        if values["reservation_token"] in self._expired_reservations:
            raise ValueError("knowledge_upload_reservation_stale")
        document = Document(
            values["document_id"],
            values["owner_id"],
            values["base_id"],
            values["title"],
            None,
            values["version_id"],
            values["at"],
            values["at"],
        )
        self.requests[req] = (values["request_hash"], document, values["version_id"])
        self.content_hashes.append(values["sha256"])
        self.referenced.add(values["storage_key"])
        self.storage_keys.append(values["storage_key"])
        self.committed_sizes[values["storage_key"]] = values["size_bytes"]
        self.reservations = {
            request: reservation
            for request, reservation in self.reservations.items()
            if reservation.reservation_token != values["reservation_token"]
        }
        self.reservation_hashes = {
            request: request_hash
            for request, request_hash in self.reservation_hashes.items()
            if request in self.reservations
        }
        self._reserved_sizes.pop(values["reservation_token"], None)
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise RuntimeError("simulated_commit_result_unknown")
        return document, values["version_id"]

    async def replace_file_document(self, **values: Any) -> tuple[Document, str]:
        return await self.create_file_document(**values)

    async def is_storage_key_referenced(self, key: str) -> bool:
        return key in self.referenced

    async def get_document(self, owner_id: str, base_id: str, document_id: str) -> Document | None:
        for _, document, _ in self.requests.values():
            if (
                document.owner_id == owner_id
                and document.knowledge_base_id == base_id
                and document.id == document_id
            ):
                return document
        return None

    async def rename_document(
        self, owner_id: str, base_id: str, document_id: str, title: str, at: datetime
    ) -> Document | None:
        del at
        for request, (request_hash, document, version_id) in self.requests.items():
            if (
                document.owner_id == owner_id
                and document.knowledge_base_id == base_id
                and document.id == document_id
            ):
                renamed = replace(document, title=title)
                self.requests[request] = (request_hash, renamed, version_id)
                return renamed
        return None


class _Tokenizer:
    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(text.split())


class _FailAfterStoreOriginalStore(MemoryOriginalStore):
    async def put(self, key: str, content: bytes) -> None:
        await super().put(key, content)
        from domain.knowledge.model import OriginalStorageUnavailableError

        raise OriginalStorageUnavailableError()


class _RacingOriginalStore(MemoryOriginalStore):
    def __init__(self) -> None:
        super().__init__()
        self._both_writing = asyncio.Event()
        self._write_count = 0

    async def put(self, key: str, content: bytes) -> None:
        self._write_count += 1
        write_number = self._write_count
        if self._write_count == 2:
            self._both_writing.set()
        await self._both_writing.wait()
        await super().put(key, content)
        if write_number == 1:
            raise OSError("simulated_concurrent_write_error")


def _make(
    originals: MemoryOriginalStore | None = None,
) -> tuple[KnowledgeService, _UploadRepository]:
    repository = _UploadRepository()
    now = datetime(2026, 11, 7, tzinfo=UTC)
    identities = iter(range(1, 100))
    service = KnowledgeService(
        cast(KnowledgeRepositoryPort, repository),
        _Tokenizer(),
        originals=originals or MemoryOriginalStore(),
        id_factory=lambda kind: f"{kind}_{next(identities)}",
        clock=lambda: now,
    )
    return service, repository


def test_owner_scoped_uploads_same_bytes_to_distinct_storage_keys() -> None:
    async def scenario() -> None:
        service, repository = _make()
        alice_base = await service.create_base("alice", "private", None)
        bob_base = await service.create_base("bob", "private", None)
        alice, _ = await service.create_file_document(
            "alice", alice_base.id, "notes", "notes.txt", b"same bytes", "alice-1"
        )
        bob, _ = await service.create_file_document(
            "bob", bob_base.id, "notes", "notes.txt", b"same bytes", "bob-1"
        )
        assert alice.owner_id != bob.owner_id
        assert len(repository.referenced) == 2
        owner_scopes = {key.split("/")[0] for key in repository.storage_keys}
        assert owner_scopes == {
            hashlib.sha256(b"alice").hexdigest(),
            hashlib.sha256(b"bob").hexdigest(),
        }
        assert len({key for key in repository.referenced}) == 2
        originals = cast(MemoryOriginalStore, service._originals)
        assert originals is not None
        assert originals.keys() == set(repository.storage_keys)

    asyncio.run(scenario())


def test_upload_idempotency_replay_keeps_one_source_key() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        first, version_a = await service.create_file_document(
            "alice", base.id, "notes", "notes.txt", b"same bytes", "request-1"
        )
        replay, version_b = await service.create_file_document(
            "alice", base.id, "notes", "notes.txt", b"same bytes", "request-1"
        )
        assert (first.id, version_a) == (replay.id, version_b)
        request_hash = repository.requests[("alice", "request-1")][0]
        assert request_hash != hashlib.sha256(b"same bytes").hexdigest()
        assert repository.content_hashes == [hashlib.sha256(b"same bytes").hexdigest()]
        assert len(repository.referenced) == 1
        assert len(repository.storage_keys) == 1
        originals = cast(MemoryOriginalStore, service._originals)
        assert originals.keys() == set(repository.storage_keys)
        with pytest.raises(ValueError, match="idempotency_conflict"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"different", "request-1"
            )
        with pytest.raises(ValueError, match="idempotency_conflict"):
            await service.create_file_document(
                "alice", base.id, "renamed", "renamed.txt", b"same bytes", "request-1"
            )
        assert len(repository.referenced) == 1

    asyncio.run(scenario())


def test_quota_rejection_happens_before_original_write() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        repository.quota_limit_bytes = 2

        with pytest.raises(ValueError, match="knowledge_quota_exceeded"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"too large", "request-1"
            )

        assert repository.reservations == {}
        originals = cast(MemoryOriginalStore, service._originals)
        assert originals.keys() == set()
        assert originals.put_calls == []

    asyncio.run(scenario())


def test_finalize_failure_keeps_reservation_original_and_quota() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        repository.fail_next = True
        with pytest.raises(RuntimeError, match="simulated_database_failure"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"orphan", "request-1"
            )
        digest = hashlib.sha256(b"orphan").hexdigest()
        originals = cast(MemoryOriginalStore, service._originals)
        orphan = next(key for key in originals.keys() if key.endswith(digest))
        assert originals.content(orphan) == b"orphan"
        assert len(repository.reservations) == 1
        assert sum(repository._reserved_sizes.values()) == len(b"orphan")
        assert orphan in originals.keys()

        token = next(iter(repository._reserved_sizes))
        repository._expired_reservations.add(token)
        expired = await repository.list_expired_upload_reservations()
        assert len(expired) == 1
        reservation = expired[0]
        assert reservation.storage_key is not None
        assert not await repository.is_storage_key_referenced(reservation.storage_key)
        await originals.delete(reservation.storage_key)
        assert await repository.delete_expired_upload_reservation(token) is True
        assert not originals.contains(orphan)
        assert token not in repository._reserved_sizes

    asyncio.run(scenario())


def test_expired_reservation_cannot_be_committed_or_renewed() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        repository.fail_next = True
        with pytest.raises(RuntimeError, match="simulated_database_failure"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"orphan", "request-expired"
            )
        token = next(iter(repository._reserved_sizes))
        repository._expired_reservations.add(token)
        with pytest.raises(ValueError, match="knowledge_upload_reservation_stale"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"orphan", "request-expired"
            )

    asyncio.run(scenario())


def test_store_error_keeps_reservation_and_bytes_until_fenced_cleanup() -> None:
    async def scenario() -> None:
        storage = _FailAfterStoreOriginalStore()
        service, repository = _make(storage)
        base = await service.create_base("alice", "private", None)

        from domain.knowledge.model import OriginalStorageUnavailableError

        with pytest.raises(OriginalStorageUnavailableError):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"expected", "request-1"
            )

        assert len(repository.reservations) == 1
        assert sum(repository._reserved_sizes.values()) == len(b"expected")
        reservation = next(iter(repository.reservations.values()))
        assert reservation.storage_key is not None
        assert storage.content(reservation.storage_key) == b"expected"
        assert reservation.storage_key in repository.retained_storage_keys()

    asyncio.run(scenario())


def test_same_reservation_concurrent_put_failure_does_not_delete_successful_write() -> None:
    async def scenario() -> None:
        storage = _RacingOriginalStore()
        service, repository = _make(storage)
        base = await service.create_base("alice", "private", None)

        results = await asyncio.gather(
            *(
                service.create_file_document(
                    "alice", base.id, "notes", "notes.txt", b"same bytes", "request-1"
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )

        assert sum(isinstance(result, Exception) for result in results) == 1
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert len(repository.storage_keys) == 1
        key = repository.storage_keys[0]
        assert storage.content(key) == b"same bytes"
        assert repository.reservations == {}
        assert repository.committed_sizes[key] == len(b"same bytes")

    asyncio.run(scenario())


def test_uncertain_commit_keeps_referenced_original() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        repository.fail_after_commit = True
        with pytest.raises(RuntimeError, match="simulated_commit_result_unknown"):
            await service.create_file_document(
                "alice", base.id, "notes", "notes.txt", b"committed", "request-1"
            )

        assert len(repository.storage_keys) == 1
        key = repository.storage_keys[0]
        originals = cast(MemoryOriginalStore, service._originals)
        assert originals is not None
        assert originals.content(key) == b"committed"
        assert key in repository.referenced
        assert repository.reservations == {}
        assert sum(repository.committed_sizes.values()) == len(b"committed")
        assert key in originals.keys()

    asyncio.run(scenario())


def test_file_replacement_uses_same_owner_scope_and_persists_bytes() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        await service.create_file_document(
            "alice", base.id, "notes", "notes.txt", b"old", "request-1"
        )
        # The fake returns the newly registered version; this checks replacement's storage path.
        result, version = await service.replace_file_document(
            "alice",
            base.id,
            repository.requests[("alice", "request-1")][1].id,
            "new.txt",
            b"replacement",
            "request-2",
        )
        assert result.owner_id == "alice" and version.startswith("kver_")
        hashes = {key.rsplit("/", 1)[-1] for key in repository.referenced}
        assert hashes == {
            hashlib.sha256(b"old").hexdigest(),
            hashlib.sha256(b"replacement").hexdigest(),
        }

    asyncio.run(scenario())


def test_replace_request_replays_after_document_rename() -> None:
    async def scenario() -> None:
        service, repository = _make()
        base = await service.create_base("alice", "private", None)
        original, _ = await service.create_file_document(
            "alice", base.id, "notes", "notes.txt", b"original", "create-request"
        )
        replacement, version = await service.replace_file_document(
            "alice", base.id, original.id, "next.txt", b"replacement", "replace-request"
        )
        renamed = await service.rename_document("alice", base.id, original.id, "renamed")
        replay, replay_version = await service.replace_file_document(
            "alice", base.id, original.id, "next.txt", b"replacement", "replace-request"
        )

        assert renamed.title == "renamed"
        assert replay.id == replacement.id == original.id
        assert (replay.id, replay_version) == (replacement.id, version)
        assert len(repository.storage_keys) == 2

    asyncio.run(scenario())
