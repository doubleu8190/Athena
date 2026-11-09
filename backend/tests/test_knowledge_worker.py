"""知识库后台处理与切片逻辑。"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from types import ModuleType

import anyio
import pytest

from app.knowledge.worker import (
    KnowledgeWorker,
    _error_code,
    _is_retryable_error,
    _make_chunks,
    _parse_child_entry,
    _parse_in_child,
    _process_rss_mb,
    _terminate_and_reap,
)
from domain.knowledge.model import (
    ChunkLocator,
    OriginalStorageAccessDeniedError,
    OriginalStorageIntegrityError,
    OriginalStorageNotFoundError,
    OriginalStorageUnavailableError,
    TextBlock,
)
from domain.knowledge.ports import ClaimedKnowledgeJob, FileUploadReservation, ParsedDocument


class _Counter:
    def count(self, text: str) -> int:
        return len(text)


class _Tokenizer:
    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(text.split())


class _Embedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]


class _Repository:
    def __init__(self) -> None:
        self.published = False
        self.failed = False
        self.retryable = False
        self.finished_cleanup = False

    async def claim_job(
        self, *, lease_seconds: int, owner_concurrency: int = 2
    ) -> ClaimedKnowledgeJob | None:
        assert lease_seconds == 120
        assert owner_concurrency == 2
        return ClaimedKnowledgeJob(
            "job_1",
            "owner_1",
            "version_1",
            "document_1",
            "title",
            "text body",
            None,
            None,
            1,
        )

    async def heartbeat_job(self, job_id: str, fencing_token: int, *, lease_seconds: int) -> bool:
        return True

    async def publish_job(
        self,
        job: ClaimedKnowledgeJob,
        *,
        chunks: list[tuple[str, dict[str, object], int, str, list[float]]],
        warnings: list[str],
    ) -> bool:
        self.published = job.fencing_token == 1 and bool(chunks) and warnings == []
        return self.published

    async def fail_job(self, job: ClaimedKnowledgeJob, *, error_code: str, retryable: bool) -> None:
        self.failed = True
        self.retryable = retryable

    async def finish_cleanup_job(self, job: ClaimedKnowledgeJob) -> bool:
        self.finished_cleanup = True
        return True

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        del storage_key
        return False

    async def claim_expired_upload_reservations(
        self, *, limit: int = 20, lease_seconds: int = 300
    ) -> tuple[FileUploadReservation, ...]:
        return ()

    async def finish_expired_upload_reservation(
        self, reservation_token: str, fencing_token: int
    ) -> bool:
        return True

    async def can_delete_expired_upload_reservation(
        self, reservation_token: str, fencing_token: int
    ) -> bool:
        return True

    async def acquire_upload_slot(self, token: str, *, lease_seconds: int) -> bool:
        return True

    async def release_upload_slot(self, token: str) -> None:
        return None


class _UnusedParser:
    async def parse(self, filename: str, content: bytes) -> ParsedDocument:
        raise AssertionError("text source does not use parser")


class _UnusedStore:
    async def put(self, key: str, content: bytes) -> None:
        raise AssertionError

    async def get(self, key: str) -> bytes:
        raise AssertionError

    async def delete(self, key: str) -> None:
        raise AssertionError

    async def iter_orphan_candidates(
        self, *, older_than_seconds: int = 86400
    ) -> AsyncIterator[str]:
        del older_than_seconds
        if False:
            yield ""


def test_chunker_caps_token_count_and_preserves_locator() -> None:
    locator = ChunkLocator(kind="text", start_line=1, end_line=2)
    chunks = _make_chunks([TextBlock("a" * 1200, locator)], _Counter())
    assert len(chunks) >= 3
    assert all(token_count <= 512 for _, _, token_count in chunks)
    assert all(item_locator["start_line"] == 1 for _, item_locator, _ in chunks)
    assert chunks[0][0][-64:] == chunks[1][0][:64]


async def _run_text_job() -> bool:
    repository = _Repository()
    worker = KnowledgeWorker(
        repository,
        _UnusedParser(),
        _UnusedStore(),
        _Embedding(),
        _Tokenizer(),
        _Counter(),
        isolate_parsing=False,
    )
    result = await worker.run_once()
    assert repository.published
    assert not repository.failed
    return result


def test_text_job_is_published_with_embeddings() -> None:
    assert anyio.run(_run_text_job)


async def _run_no_job() -> bool:
    class EmptyRepository(_Repository):
        async def claim_job(
            self, *, lease_seconds: int, owner_concurrency: int = 2
        ) -> ClaimedKnowledgeJob | None:
            return None

    worker = KnowledgeWorker(
        EmptyRepository(), _UnusedParser(), _UnusedStore(), _Embedding(), _Tokenizer(), _Counter()
    )
    return await worker.run_once()


def test_no_job_returns_without_processing() -> None:
    assert not anyio.run(_run_no_job)


async def _parse_repeatedly() -> None:
    for index in range(3):
        parsed = await _parse_in_child(
            f"sample-{index}.txt", b"valid text content for parser process lifecycle", 30
        )
        assert parsed.blocks
        assert "valid text content" in parsed.blocks[0].text


@pytest.mark.skipif(__import__("os").name == "nt", reason="spawn CLI import differs on Windows")
def test_parser_child_result_is_not_mistaken_for_a_live_child() -> None:
    anyio.run(_parse_repeatedly)


def test_memory_monitor_can_read_process_rss() -> None:
    rss_mb = _process_rss_mb(os.getpid())
    assert rss_mb is not None and rss_mb > 0


def test_memory_monitor_handles_process_exit_and_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil  # type: ignore[import-untyped]

    def inaccessible(_: int) -> object:
        raise psutil.AccessDenied(pid=1)

    monkeypatch.setattr(psutil, "Process", inaccessible)
    assert _process_rss_mb(1) is None


def test_exception_cleanup_kills_unresponsive_child_and_joins() -> None:
    class UnresponsiveChild:
        alive = True
        joined = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            self.alive = False

        def join(self, timeout: float | None = None) -> None:
            self.joined = True

    child = UnresponsiveChild()
    anyio.run(_terminate_and_reap, child)
    assert child.joined and not child.alive


async def _run_profile_mismatch() -> _Repository:
    class MismatchedRepository(_Repository):
        async def claim_job(
            self, *, lease_seconds: int, owner_concurrency: int = 2
        ) -> ClaimedKnowledgeJob | None:
            job = await super().claim_job(
                lease_seconds=lease_seconds,
                owner_concurrency=owner_concurrency,
            )
            assert job is not None
            from dataclasses import replace

            return replace(job, profile_id="database-profile")

    repository = MismatchedRepository()
    worker = KnowledgeWorker(
        repository,
        _UnusedParser(),
        _UnusedStore(),
        _Embedding(),
        _Tokenizer(),
        _Counter(),
        profile_id="worker-profile",
    )
    await worker.run_once()
    return repository


def test_worker_rejects_a_different_embedding_profile() -> None:
    repository = anyio.run(_run_profile_mismatch)
    assert repository.failed
    assert not repository.published
    assert not repository.retryable


async def _run_cleanup(error: Exception) -> _Repository:
    class CleanupRepository(_Repository):
        async def claim_job(
            self, *, lease_seconds: int, owner_concurrency: int = 2
        ) -> ClaimedKnowledgeJob:
            assert lease_seconds == 120
            assert owner_concurrency == 2
            return ClaimedKnowledgeJob(
                "job_cleanup",
                "owner_1",
                "version_1",
                "document_1",
                "title",
                None,
                None,
                None,
                1,
                kind="cleanup",
                source_keys=("a" * 64 + "/ksrc_1/" + "b" * 2 + "/" + "b" * 64,),
                target_id="doc:document_1",
            )

    class DeleteStore(_UnusedStore):
        async def delete(self, key: str) -> None:
            del key
            raise error

    repository = CleanupRepository()
    worker = KnowledgeWorker(
        repository,
        _UnusedParser(),
        DeleteStore(),
        _Embedding(),
        _Tokenizer(),
        _Counter(),
        isolate_parsing=False,
    )
    await worker.run_once()
    return repository


def test_cleanup_treats_missing_object_as_success() -> None:
    repository = anyio.run(
        _run_cleanup,
        OriginalStorageNotFoundError(),
    )
    assert repository.finished_cleanup
    assert not repository.failed


def test_cleanup_keeps_transient_storage_failure_retryable() -> None:
    repository = anyio.run(
        _run_cleanup,
        OriginalStorageUnavailableError(),
    )
    assert not repository.finished_cleanup
    assert repository.failed
    assert repository.retryable


async def _run_invalid_vector() -> _Repository:
    class InvalidEmbedding(_Embedding):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            vector = [0.0] * 1024
            vector[0] = float("inf")
            return [vector]

    repository = _Repository()
    worker = KnowledgeWorker(
        repository,
        _UnusedParser(),
        _UnusedStore(),
        InvalidEmbedding(),
        _Tokenizer(),
        _Counter(),
        isolate_parsing=False,
    )
    await worker.run_once()
    return repository


def test_worker_does_not_publish_nonfinite_or_zero_embedding() -> None:
    repository = anyio.run(_run_invalid_vector)
    assert repository.failed
    assert not repository.published
    assert not repository.retryable


def test_parser_ipc_marks_runtime_engine_errors_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    class ResultQueue:
        value: tuple[bool, ParsedDocument | str] | None = None

        def put(self, value: tuple[bool, ParsedDocument | str]) -> None:
            self.value = value

    parser_module = ModuleType("infrastructure.knowledge.parsers")

    class RuntimeFailureParser:
        def __init__(self, **_: object) -> None:
            pass

        async def parse(self, filename: str, content: bytes) -> ParsedDocument:
            raise RuntimeError("knowledge_ocr_engine_unavailable")

    parser_module.LocalDocumentParser = RuntimeFailureParser  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "infrastructure.knowledge.parsers", parser_module)
    queue = ResultQueue()
    _parse_child_entry("scan.png", b"image", queue, 4096)
    assert queue.value == (False, "transient:knowledge_ocr_engine_unavailable")
    assert _error_code(RuntimeError("knowledge_ocr_engine_unavailable")) == (
        "knowledge_ocr_engine_unavailable"
    )


def test_storage_error_codes_and_retry_policies_are_distinct() -> None:
    unavailable = OriginalStorageUnavailableError()
    missing = OriginalStorageNotFoundError()
    denied = OriginalStorageAccessDeniedError()
    integrity = OriginalStorageIntegrityError()

    assert _error_code(unavailable) == "knowledge_storage_unavailable"
    assert _error_code(missing) == "knowledge_storage_object_not_found"
    assert _is_retryable_error(unavailable, "ingestion")
    assert _is_retryable_error(unavailable, "cleanup")
    assert not _is_retryable_error(missing, "ingestion")
    assert not _is_retryable_error(missing, "cleanup")
    assert not _is_retryable_error(denied, "ingestion")
    assert _is_retryable_error(denied, "cleanup")
    assert not _is_retryable_error(integrity, "cleanup")
