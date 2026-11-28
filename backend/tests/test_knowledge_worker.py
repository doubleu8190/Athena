"""知识库后台处理与切片逻辑。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import anyio
import pytest

from app.knowledge.worker import (
    KnowledgeWorker,
    _build_index_text,
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


def test_chunker_splits_long_text_on_natural_boundaries_and_preserves_words() -> None:
    """长文本切分应优先落在段落/句末/空白边界，不切碎版本号小数点，且 overlap 不截断英文单词。"""
    # 1. 中文句子边界：构造在 512 字符附近有句号的长文本，验证首片在句号处收尾而非句中截断。
    sentence = "这是用于验证知识库自然边界切分能力的完整中文测试句子。"
    chinese_text = sentence * 25
    zh_chunks = _make_chunks([TextBlock(chinese_text, ChunkLocator(kind="text"))], _Counter())
    assert len(zh_chunks) >= 2
    assert all(token_count <= 512 for _, _, token_count in zh_chunks)
    assert zh_chunks[0][0].endswith("。")

    # 2. 英文单词与版本号/小数保护：不把 v2.1 / 3.14 的小数点当句末标点，也不截断英文单词。
    english_segment = "Athena release v2.1 measures ratio 3.14 across retrieval pipelines "
    english_text = (english_segment * 14).strip()
    en_chunks = _make_chunks([TextBlock(english_text, ChunkLocator(kind="text"))], _Counter())
    assert len(en_chunks) >= 2
    assert all(token_count <= 512 for _, _, token_count in en_chunks)
    assert not en_chunks[0][0].endswith("v2.")
    assert not en_chunks[0][0].endswith("3.")
    # 第二片开头应从完整单词起始（不会以某个单词中间残片开头）。
    valid_words = set(english_segment.split())
    second_first_word = en_chunks[1][0].split()[0]
    assert second_first_word in valid_words


def test_chunker_repeats_header_when_excel_sheet_splits() -> None:
    """Excel 同 Sheet 超限拆片时，后续分片自动前置首行表头；超长表头则不强拼。"""
    header = "A1: 姓名 | B1: 部门 | C1: 说明"
    rows = [
        TextBlock(header, ChunkLocator(kind="xlsx", sheet="成员表", cell_range="1:1")),
        *[
            TextBlock(
                f"A{i}: 用户{i} | B{i}: 研发部 | C{i}: " + ("说明" * 70),
                ChunkLocator(kind="xlsx", sheet="成员表", cell_range=f"{i}:{i}"),
            )
            for i in range(2, 8)
        ],
    ]
    chunks = _make_chunks(rows, _Counter())
    assert len(chunks) >= 2
    assert all(token_count <= 512 for _, _, token_count in chunks)
    # 第一片和后续拆片均以表头开头，且保留各自的数据行范围定位。
    assert chunks[0][0].startswith(header)
    assert chunks[1][0].startswith(f"{header}\n")
    assert chunks[0][1]["sheet"] == "成员表"
    assert chunks[1][1]["sheet"] == "成员表"
    assert chunks[0][1]["cell_range"] != chunks[1][1]["cell_range"]

    # 表头超过单块预算一半（> 256）时，拆片不强拼表头以免挤压正文或超限。
    huge_header = "A1: " + ("表" * 300)
    huge_rows = [
        TextBlock(huge_header, ChunkLocator(kind="xlsx", sheet="大表头", cell_range="1:1")),
        TextBlock(
            "A2: " + ("值" * 300), ChunkLocator(kind="xlsx", sheet="大表头", cell_range="2:2")
        ),
    ]
    huge_chunks = _make_chunks(huge_rows, _Counter())
    assert len(huge_chunks) == 2
    assert huge_chunks[1][0].startswith("A2:")

    # 表头 <= 256 但「表头 + 单行 > 512」或单行本身 > 512 时，
    # 均以表头为前缀切分，拆出的每个子分片都携带表头且总 token <= 512；后续短行仍正常前置表头合并。
    medium_header = "A1: " + ("头" * 97)  # 101 字符（按 _Counter 计为 101 token）
    border_rows = [
        TextBlock(medium_header, ChunkLocator(kind="xlsx", sheet="边界表", cell_range="1:1")),
        TextBlock(
            "A2: " + ("中" * 476), ChunkLocator(kind="xlsx", sheet="边界表", cell_range="2:2")
        ),
        TextBlock(
            "A3: " + ("长" * 620), ChunkLocator(kind="xlsx", sheet="边界表", cell_range="3:3")
        ),
        TextBlock("A4: 短行收尾", ChunkLocator(kind="xlsx", sheet="边界表", cell_range="4:4")),
    ]
    border_chunks = _make_chunks(border_rows, _Counter())
    assert all(token_count <= 512 for _, _, token_count in border_chunks)
    assert border_chunks[0][0] == medium_header
    # 第 2 行 480 token + 表头 101 token = 581 > 512，应按表头前缀拆片且每片均带表头。
    row2_chunks = [c for c in border_chunks if c[1]["cell_range"] == "2:2"]
    assert len(row2_chunks) >= 2
    assert all(c[0].startswith(f"{medium_header}\n") for c in row2_chunks)
    # 第 3 行单行 624 > 512，同样应按表头前缀拆片且每片均带表头、总 token <= 512。
    row3_chunks = [c for c in border_chunks if c[1]["cell_range"] == "3:3"]
    assert len(row3_chunks) >= 2
    assert all(c[0].startswith(f"{medium_header}\n") for c in row3_chunks)
    # 第 4 行短行在超长行切分后开启新缓冲，仍应自动前置表头。
    assert border_chunks[-1][0] == f"{medium_header}\nA4: 短行收尾"


def test_chunker_aggregates_docx_and_pptx_tables_with_header_repeat_and_isolates_from_text() -> (
    None
):
    """带表标识的 DOCX/PPTX 表格应表内聚合、超限拆片重复表头、且绝不与正文或跨表合并。"""
    docx_header = "指标 | 说明"
    pptx_header = "阶段 | 目标"
    blocks = [
        TextBlock("表前正文段落", ChunkLocator(kind="docx", start_line=1, heading="第一章")),
        TextBlock(
            docx_header, ChunkLocator(kind="docx_table", start_line=1, heading="table 1"), "table"
        ),
        TextBlock(
            "召回率 | " + ("详细说明" * 55),
            ChunkLocator(kind="docx_table", start_line=2, heading="table 1"),
            "table",
        ),
        TextBlock(
            "精确率 | " + ("详细说明" * 55),
            ChunkLocator(kind="docx_table", start_line=3, heading="table 1"),
            "table",
        ),
        TextBlock(
            "延迟 | " + ("详细说明" * 55),
            ChunkLocator(kind="docx_table", start_line=4, heading="table 1"),
            "table",
        ),
        # 第二张 DOCX 表必须独立分组，不与 table 1 合并。
        TextBlock(
            "表二列1 | 表二列2",
            ChunkLocator(kind="docx_table", start_line=1, heading="table 2"),
            "table",
        ),
        TextBlock("表后正文段落", ChunkLocator(kind="docx", start_line=2, heading="第一章")),
        # PPTX 同表行聚合与拆片重复表头。
        TextBlock(pptx_header, ChunkLocator(kind="pptx_table", slide=2, shape="3"), "table"),
        TextBlock(
            "P1 | " + ("推进计划" * 70),
            ChunkLocator(kind="pptx_table", slide=2, shape="3"),
            "table",
        ),
        TextBlock(
            "P2 | " + ("推进计划" * 70),
            ChunkLocator(kind="pptx_table", slide=2, shape="3"),
            "table",
        ),
    ]

    chunks = _make_chunks(blocks, _Counter())
    assert all(token_count <= 512 for _, _, token_count in chunks)

    # 正文与表格严格隔离。
    assert chunks[0][0] == "表前正文段落"
    docx_table1_chunks = [
        c for c in chunks if c[1]["kind"] == "docx_table" and c[1]["heading"] == "table 1"
    ]
    assert len(docx_table1_chunks) >= 2
    assert docx_table1_chunks[0][0].startswith(docx_header)
    assert docx_table1_chunks[1][0].startswith(f"{docx_header}\n")
    assert docx_table1_chunks[0][1]["start_line"] == 1
    first_table_end_line = docx_table1_chunks[0][1]["end_line"]
    assert isinstance(first_table_end_line, int) and first_table_end_line >= 2

    docx_table2_chunks = [
        c for c in chunks if c[1]["kind"] == "docx_table" and c[1]["heading"] == "table 2"
    ]
    assert len(docx_table2_chunks) == 1
    assert docx_table2_chunks[0][0] == "表二列1 | 表二列2"

    assert any(c[0] == "表后正文段落" for c in chunks)

    pptx_table_chunks = [c for c in chunks if c[1]["kind"] == "pptx_table"]
    assert len(pptx_table_chunks) == 2
    assert pptx_table_chunks[0][0].startswith(pptx_header)
    assert pptx_table_chunks[1][0].startswith(f"{pptx_header}\n")
    assert pptx_table_chunks[0][1]["slide"] == 2
    assert pptx_table_chunks[0][1]["shape"] == "3"


def test_chunker_isolates_markdown_code_blocks_from_adjacent_text() -> None:
    """Markdown 围栏代码块（kind='code'）即使与前后正文同章节且很短，也不得合并。"""
    blocks = [
        TextBlock(
            "说明前文", ChunkLocator(kind="markdown", start_line=1, end_line=1, heading="示例")
        ),
        TextBlock(
            "```python\nprint('hello')\n```",
            ChunkLocator(kind="markdown", start_line=2, end_line=4, heading="示例"),
            kind="code",
        ),
        TextBlock(
            "说明后文", ChunkLocator(kind="markdown", start_line=5, end_line=5, heading="示例")
        ),
    ]
    chunks = _make_chunks(blocks, _Counter())
    assert [c[0] for c in chunks] == ["说明前文", "```python\nprint('hello')\n```", "说明后文"]


def test_worker_enriches_index_text_with_contextual_header_and_keeps_clean_content() -> None:
    """向量化与分词必须使用带上下文头的索引文本，入库正文保持干净原文，且过滤样式占位名。"""
    assert (
        _build_index_text("正文内容", {"heading": "Normal"}, "研发手册")
        == "[文档: 研发手册]\n正文内容"
    )
    assert (
        _build_index_text("表格行", {"heading": "table 1"}, "研发手册")
        == "[文档: 研发手册]\n表格行"
    )
    assert (
        _build_index_text("正文", {"heading": "正文", "slide": 3}, "汇报")
        == "[文档: 汇报 | 第3页]\n正文"
    )
    assert (
        _build_index_text("单元格", {"sheet": "收入明细", "heading": "第一章"}, "财报")
        == "[文档: 财报 | 章节: 第一章 | 工作表: 收入明细]\n单元格"
    )
    assert _build_index_text("纯文本", {}, "") == "纯文本"

    embedded_inputs: list[str] = []
    tokenized_inputs: list[str] = []

    class _CapturingEmbedding:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            embedded_inputs.extend(texts)
            return [[0.1] * 1024 for _ in texts]

    class _CapturingTokenizer:
        def tokenize(self, text: str) -> tuple[str, ...]:
            tokenized_inputs.append(text)
            return tuple(text.split())

    class _StructuredParser:
        def create(self) -> _StructuredParser:
            return self

        async def parse(self, filename: str, content: bytes) -> ParsedDocument:
            del filename, content
            return ParsedDocument(
                (
                    TextBlock(
                        "核心检索正文",
                        ChunkLocator(
                            kind="markdown", start_line=1, end_line=1, heading="架构 > 检索"
                        ),
                    ),
                    TextBlock(
                        "A1: 营收 | B1: 100",
                        ChunkLocator(kind="xlsx", sheet="Q1财务", cell_range="1:1"),
                    ),
                    TextBlock(
                        "普通段落",
                        ChunkLocator(kind="docx", start_line=1, heading="Normal"),
                    ),
                    TextBlock(
                        "表格值",
                        ChunkLocator(kind="docx_table", start_line=1, heading="table 1"),
                        kind="table",
                    ),
                )
            )

    class _TitledFileRepository(_FileRepository):
        async def claim_job(
            self, *, lease_seconds: int, owner_concurrency: int = 2
        ) -> ClaimedKnowledgeJob | None:
            del lease_seconds, owner_concurrency
            return ClaimedKnowledgeJob(
                "job_ctx",
                "owner_1",
                "version_1",
                "document_1",
                "架构白皮书",
                None,
                "source_key",
                "whitepaper.md",
                1,
            )

    async def scenario() -> None:
        repository = _TitledFileRepository()
        worker = KnowledgeWorker(
            repository,
            _StructuredParser(),
            _BytesStore(),
            _CapturingEmbedding(),
            _CapturingTokenizer(),
            _Counter(),
            isolate_parsing=False,
        )
        assert await worker.run_once() is True
        assert repository.published is True
        assert not repository.failed

        expected_index_texts = [
            "[文档: 架构白皮书 | 章节: 架构 > 检索]\n核心检索正文",
            "[文档: 架构白皮书 | 工作表: Q1财务]\nA1: 营收 | B1: 100",
            "[文档: 架构白皮书]\n普通段落",
            "[文档: 架构白皮书]\n表格值",
        ]
        assert embedded_inputs == expected_index_texts
        assert tokenized_inputs == expected_index_texts
        # 最终发布入库的 content 必须是干净原文，不含上下文头。
        assert [content for content, _, _, _, _ in repository.chunks] == [
            "核心检索正文",
            "A1: 营收 | B1: 100",
            "普通段落",
            "表格值",
        ]
        # search_text 中包含文档/章节/工作表关键词，可供 BM25 命中。
        assert "架构白皮书" in repository.chunks[0][3]
        assert "检索" in repository.chunks[0][3]
        assert "Q1财务" in repository.chunks[1][3]
        assert "Normal" not in repository.chunks[2][3]
        assert "table 1" not in repository.chunks[3][3]

    anyio.run(scenario)
