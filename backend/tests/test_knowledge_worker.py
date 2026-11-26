"""知识库后台处理与切片逻辑。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import anyio
import pytest

from app.knowledge.worker import (
    KnowledgeWorker,
    _error_code,
    _is_retryable_error,
    _make_chunks,
)
from domain.knowledge.model import (
    ChunkLocator,
    OriginalStorageAccessDeniedError,
    OriginalStorageIntegrityError,
    OriginalStorageNotFoundError,
    OriginalStorageUnavailableError,
    TextBlock,
)
from domain.knowledge.ports import ClaimedKnowledgeJob, ParsedDocument
from infrastructure.knowledge.parsers import LocalDocumentParserFactory


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
        self.error_code: str | None = None
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
        self.error_code = error_code
        self.retryable = retryable

    async def finish_cleanup_job(self, job: ClaimedKnowledgeJob) -> bool:
        self.finished_cleanup = True
        return True

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        del storage_key
        return False

    async def find_referenced_storage_keys(self, storage_keys: object) -> set[str]:
        del storage_keys
        return set()


class _UnusedParser:
    def create(self) -> _UnusedParser:
        return self

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


class _BytesStore(_UnusedStore):
    """文件源返回固定文本内容，供隔离解析子进程读取。"""

    async def get(self, key: str) -> bytes:
        assert key == "source_key"
        return b"valid text content for isolated parsing"


class _FileRepository(_Repository):
    """认领文件来源任务并记录发布的 chunks，供隔离解析全链路断言。"""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[tuple[str, dict[str, object], int, str, list[float]]] = []

    async def claim_job(
        self, *, lease_seconds: int, owner_concurrency: int = 2
    ) -> ClaimedKnowledgeJob | None:
        assert lease_seconds == 120
        assert owner_concurrency == 2
        return ClaimedKnowledgeJob(
            "job_file",
            "owner_1",
            "version_1",
            "document_1",
            "title",
            None,
            "source_key",
            "sample.txt",
            1,
        )

    async def publish_job(
        self,
        job: ClaimedKnowledgeJob,
        *,
        chunks: list[tuple[str, dict[str, object], int, str, list[float]]],
        warnings: list[str],
    ) -> bool:
        self.chunks = chunks
        self.published = job.fencing_token == 1 and bool(chunks) and warnings == []
        return self.published


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


async def _run_isolated_file_job() -> _FileRepository:
    repository = _FileRepository()
    worker = KnowledgeWorker(
        repository,
        # 保持默认 isolate_parsing=True，让真实 spawn 子进程完成解析。
        LocalDocumentParserFactory(),
        _BytesStore(),
        _Embedding(),
        _Tokenizer(),
        _Counter(),
    )
    try:
        assert await worker.run_once()
    finally:
        await worker.aclose()
    return repository


@pytest.mark.skipif(os.name == "nt", reason="spawn CLI import differs on Windows")
def test_isolated_parsing_publishes_file_content_from_child_process() -> None:
    repository = anyio.run(_run_isolated_file_job)
    assert repository.published
    assert not repository.failed
    assert repository.chunks
    assert any(
        "valid text content for isolated parsing" in content
        for content, _, _, _, _ in repository.chunks
    )


async def _run_no_job() -> bool:
    class EmptyRepository(_Repository):
        async def claim_job(
            self, *, lease_seconds: int, owner_concurrency: int = 2
        ) -> ClaimedKnowledgeJob | None:
            return None

    worker = KnowledgeWorker(
        EmptyRepository(),
        _UnusedParser(),
        _UnusedStore(),
        _Embedding(),
        _Tokenizer(),
        _Counter(),
        isolate_parsing=False,
    )
    return await worker.run_once()


def test_no_job_returns_without_processing() -> None:
    assert not anyio.run(_run_no_job)


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
        isolate_parsing=False,
        profile_id="worker-profile",
    )
    await worker.run_once()
    return repository


def test_worker_rejects_a_different_embedding_profile() -> None:
    repository = anyio.run(_run_profile_mismatch)
    assert repository.failed
    assert repository.error_code == "knowledge_embedding_profile_mismatch"
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
    assert repository.error_code == "knowledge_embedding_profile_mismatch"
    assert not repository.published
    assert not repository.retryable


def test_storage_error_codes_and_retry_policies_are_distinct() -> None:
    unavailable = OriginalStorageUnavailableError()
    missing = OriginalStorageNotFoundError()
    denied = OriginalStorageAccessDeniedError()
    integrity = OriginalStorageIntegrityError()

    assert _error_code(RuntimeError("knowledge_ocr_engine_unavailable")) == (
        "knowledge_ocr_engine_unavailable"
    )
    assert _error_code(unavailable) == "knowledge_storage_unavailable"
    assert _error_code(missing) == "knowledge_storage_object_not_found"
    assert _is_retryable_error(unavailable, "ingestion")
    assert _is_retryable_error(unavailable, "cleanup")
    assert not _is_retryable_error(missing, "ingestion")
    assert not _is_retryable_error(missing, "cleanup")
    assert not _is_retryable_error(denied, "ingestion")
    assert _is_retryable_error(denied, "cleanup")
    assert not _is_retryable_error(integrity, "cleanup")


def test_chunker_merges_adjacent_short_blocks_within_same_region_and_updates_locators() -> None:
    """相邻同区域短文本块应按换行合并并更新起止行与表格范围，跨区域或表格块不合并。"""
    blocks = [
        TextBlock("line one", ChunkLocator(kind="text", start_line=1, end_line=1)),
        TextBlock("line two", ChunkLocator(kind="text", start_line=2, end_line=2)),
        TextBlock("line three", ChunkLocator(kind="text", start_line=3, end_line=3)),
        # 不同页不合并
        TextBlock("page 1 text", ChunkLocator(kind="pdf", page=1)),
        TextBlock("page 2 text", ChunkLocator(kind="pdf", page=2)),
        # xlsx 同表相邻行合并并更新 cell_range，跨 sheet 断开
        TextBlock("A1: 1", ChunkLocator(kind="xlsx", sheet="S1", cell_range="1:1")),
        TextBlock("A2: 2", ChunkLocator(kind="xlsx", sheet="S1", cell_range="2:2")),
        TextBlock("A3: 3", ChunkLocator(kind="xlsx", sheet="S1", cell_range="3:3")),
        TextBlock("A1: x", ChunkLocator(kind="xlsx", sheet="S2", cell_range="1:1")),
        # docx 同 heading 合并，表格（kind="table"）不与前后合并
        TextBlock("para 1", ChunkLocator(kind="docx", start_line=1, heading="Normal")),
        TextBlock("para 2", ChunkLocator(kind="docx", start_line=2, heading="Normal")),
        TextBlock("col1 | col2", ChunkLocator(kind="docx_table", start_line=1), "table"),
        TextBlock("col3 | col4", ChunkLocator(kind="docx_table", start_line=2), "table"),
        # 超过单块上限（512）时分片，不合并超限
        TextBlock("x" * 300, ChunkLocator(kind="markdown", start_line=10, end_line=10)),
        TextBlock("y" * 300, ChunkLocator(kind="markdown", start_line=11, end_line=11)),
    ]

    chunks = _make_chunks(blocks, _Counter())

    assert chunks[0][0] == "line one\nline two\nline three"
    assert chunks[0][1]["start_line"] == 1
    assert chunks[0][1]["end_line"] == 3
    assert chunks[1][0] == "page 1 text"
    assert chunks[2][0] == "page 2 text"
    assert chunks[3][0] == "A1: 1\nA2: 2\nA3: 3"
    assert chunks[3][1]["sheet"] == "S1"
    assert chunks[3][1]["cell_range"] == "1:3"
    assert chunks[4][0] == "A1: x"
    assert chunks[4][1]["sheet"] == "S2"
    assert chunks[5][0] == "para 1\npara 2"
    assert chunks[5][1]["start_line"] == 1
    assert chunks[5][1]["end_line"] == 2
    assert chunks[6][0] == "col1 | col2"
    assert chunks[7][0] == "col3 | col4"
    assert chunks[8][0] == "x" * 300
    assert chunks[9][0] == "y" * 300


def test_worker_embeds_in_bounded_batches_offloads_cpu_and_stops_on_lost_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """大文档分批（32）向量化、卸载切片与分词到工作线程，且失租后立即中止后续批次。"""
    import asyncio
    import threading

    import app.knowledge.worker as worker_module

    main_thread = threading.current_thread()
    counter_threads: set[threading.Thread] = set()
    tokenizer_threads: set[threading.Thread] = set()

    class _ThreadRecordingCounter:
        def count(self, text: str) -> int:
            counter_threads.add(threading.current_thread())
            return len(text)

    class _ThreadRecordingTokenizer:
        def tokenize(self, text: str) -> tuple[str, ...]:
            tokenizer_threads.add(threading.current_thread())
            return tuple(text.split())

    class _BatchTrackingEmbedding:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.batch_sizes.append(len(texts))
            return [[0.1] * 1024 for _ in texts]

    class _MultiPageParser:
        def create(self) -> _MultiPageParser:
            return self

        async def parse(self, filename: str, content: bytes) -> ParsedDocument:
            del filename, content
            # 70 个独立页短块保证生成 70 个 chunk，验证 32 + 32 + 6 分批。
            return ParsedDocument(
                tuple(
                    TextBlock(f"page {index} content", ChunkLocator(kind="pdf", page=index))
                    for index in range(1, 71)
                )
            )

    async def scenario_batched_success() -> None:
        repository = _FileRepository()
        embedding = _BatchTrackingEmbedding()
        worker = KnowledgeWorker(
            repository,
            _MultiPageParser(),
            _BytesStore(),
            embedding,
            _ThreadRecordingTokenizer(),
            _ThreadRecordingCounter(),
            isolate_parsing=False,
        )
        assert await worker.run_once() is True
        assert repository.published is True
        assert not repository.failed
        assert embedding.batch_sizes == [32, 32, 6]
        assert len(repository.chunks) == 70
        assert main_thread not in counter_threads
        assert main_thread not in tokenizer_threads

    async def scenario_lost_lease_between_batches() -> None:
        monkeypatch.setattr(worker_module, "HEARTBEAT_SECONDS", 0.01)
        repository = _FileRepository()
        lease_lost_signaled = asyncio.Event()

        async def lose_lease(job_id: str, fencing_token: int, *, lease_seconds: int) -> bool:
            del job_id, fencing_token, lease_seconds
            lease_lost_signaled.set()
            return False

        repository.heartbeat_job = lose_lease  # type: ignore[method-assign]
        embedding = _BatchTrackingEmbedding()

        async def slow_embed(texts: list[str]) -> list[list[float]]:
            embedding.batch_sizes.append(len(texts))
            await lease_lost_signaled.wait()
            return [[0.1] * 1024 for _ in texts]

        embedding.embed = slow_embed  # type: ignore[method-assign]
        worker = KnowledgeWorker(
            repository,
            _MultiPageParser(),
            _BytesStore(),
            embedding,
            _Tokenizer(),
            _Counter(),
            isolate_parsing=False,
        )
        assert await worker.run_once() is True
        assert embedding.batch_sizes == [32]
        assert not repository.published
        assert not repository.failed

    asyncio.run(scenario_batched_success())
    asyncio.run(scenario_lost_lease_between_batches())
