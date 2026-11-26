"""Phase 2-2 模块 C（RAG 检索提速与知识库入库 Worker 重构）专项回归测试。"""

from __future__ import annotations

import asyncio
import gc
import gzip
import io
import json
import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace, TracebackType
from typing import Any, Self, cast

import httpx2
import pytest
from PIL import Image
from sqlalchemy import DateTime, event, func, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from adapter.auth.model import UserModel
from adapter.knowledge.model import (
    KnowledgeBaseModel,
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    KnowledgeJobModel,
    KnowledgeVersionModel,
)
from adapter.knowledge.repository import (
    MAX_KEYWORD_TOKENS,
    PUBLISH_CHUNK_BATCH_SIZE,
    KnowledgeRepository,
    _select_keyword_tokens,
    _visible_chunks_query,
)
from adapter.models import Base
from app.knowledge.service import KnowledgeService
from app.knowledge.worker import (
    KnowledgeWorker,
    _IsolatedParserPool,
    _make_chunks,
)
from domain.knowledge.model import ChunkLocator, SearchHit, TextBlock
from domain.knowledge.ports import (
    ClaimedKnowledgeJob,
    DocumentParser,
    DocumentParserFactory,
    KnowledgeRepositoryPort,
    ParsedDocument,
)
from infrastructure.embedding import (
    EmbeddingUpstreamError,
    OpenAICompatibleEmbeddingClient,
    OpenAICompatibleEmbeddingConfig,
    TiktokenTokenCounter,
)
from infrastructure.knowledge.parsers import LocalDocumentParser

# ---------------------------------------------------------------------------
# 可被 spawn 子进程 pickle 的顶层测试解析器工厂（供常驻进程池单测使用）
# ---------------------------------------------------------------------------


class _PidReportingParser(DocumentParser):
    """在子进程内上报当前 PID、创建次数与按文件名模拟正常/异常/超时/崩溃行为。"""

    def __init__(self) -> None:
        self._created_pid = os.getpid()
        self._calls = 0

    async def parse(self, filename: str, content: bytes) -> ParsedDocument:
        del content
        self._calls += 1
        if filename == "invalid.txt":
            raise ValueError("knowledge_document_invalid")
        if filename == "oom.txt":
            raise MemoryError("secret_internal_buffer_details")
        if filename == "boom.txt":
            raise RuntimeError("leaked /etc/secret/path details")
        if filename == "slow.txt":
            time.sleep(10.0)
        if filename == "crash.txt":
            os._exit(1)
        return ParsedDocument(
            (
                TextBlock(
                    f"pid={self._created_pid};calls={self._calls};file={filename}",
                    ChunkLocator(kind="text", start_line=1, end_line=1),
                ),
            )
        )


class _PidReportingParserFactory(DocumentParserFactory):
    def create(self) -> DocumentParser:
        return _PidReportingParser()


# ---------------------------------------------------------------------------
# C.1 RAG 查询侧优化测试
# ---------------------------------------------------------------------------


def test_search_semantic_sets_hnsw_iterative_scan_only_on_postgresql_and_projects_scalars() -> None:
    """PostgreSQL 语义检索须在显式事务内开启 hnsw.iterative_scan 且仅投影标量列；SQLite 跳过。"""
    pg_statements: list[str] = []
    pg_began = False

    class _FakePgTransaction:
        async def __aenter__(self) -> None:
            nonlocal pg_began
            pg_began = True

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            del exc_type, exc, tb

    class _FakePgSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            del exc_type, exc, tb

        def begin(self) -> _FakePgTransaction:
            return _FakePgTransaction()

        async def execute(self, statement: Any) -> list[Any]:
            compiled = str(
                statement.compile(
                    dialect=cast(Any, postgresql).dialect(),
                    compile_kwargs={"literal_binds": False},
                )
            )
            pg_statements.append(compiled)
            if compiled.startswith("SET LOCAL"):
                return []
            return [
                (
                    "kchunk_1",
                    "kdoc_1",
                    "kver_1",
                    "差旅住宿标准 600 元",
                    {"kind": "text", "start_line": 1},
                    0,
                    "差旅制度",
                    "kb_1",
                )
            ]

    sqlite_statements: list[str] = []

    class _FakeSqliteSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            del exc_type, exc, tb

        async def execute(self, statement: Any) -> list[Any]:
            compiled = str(
                statement.compile(
                    dialect=cast(Any, postgresql).dialect(),
                    compile_kwargs={"literal_binds": False},
                )
            )
            sqlite_statements.append(compiled)
            return []

    async def scenario() -> tuple[SearchHit, ...]:
        pg_repo = KnowledgeRepository(cast(Any, lambda: _FakePgSession()))
        hits = await pg_repo.search_semantic(
            "owner", ["kb_1"], [0.1] * 1024, 10, "prof", min_score=0.35
        )
        sqlite_repo = KnowledgeRepository(cast(Any, lambda: _FakeSqliteSession()))
        await sqlite_repo.search_semantic("owner", ["kb_1"], [0.1] * 1024, 10, "prof")
        return hits

    hits = asyncio.run(scenario())

    assert pg_began is True
    assert len(pg_statements) == 2
    assert pg_statements[0] == "SET LOCAL hnsw.iterative_scan = relaxed_order"
    select_clause = pg_statements[1].split("FROM")[0]
    assert "knowledge_chunks.embedding" not in select_clause
    assert "knowledge_chunks.search_document" not in select_clause
    assert "knowledge_chunks.content" in select_clause
    assert "knowledge_chunks.locator" in select_clause
    assert "knowledge_chunks.ordinal" in select_clause
    assert len(hits) == 1
    assert hits[0].chunk_id == "kchunk_1"
    assert hits[0].title == "差旅制度"
    assert hits[0].knowledge_base_id == "kb_1"
    assert hits[0].channels == ("semantic",)

    # 非 PostgreSQL 会话不得执行 SET LOCAL hnsw.iterative_scan。
    assert len(sqlite_statements) == 1
    assert all("hnsw.iterative_scan" not in sql for sql in sqlite_statements)


def test_search_keyword_get_chunk_and_visible_query_exclude_large_columns() -> None:
    """关键词检索、分片读取与可见查询均不得 SELECT embedding/search_document 大列。"""
    captured_sql: list[str] = []

    class _FakeResult:
        def __iter__(self) -> Any:
            return iter(())

        def one_or_none(self) -> None:
            return None

    class _RecordingSession:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            del exc_type, exc, tb

        async def execute(self, statement: Any) -> _FakeResult:
            compiled = str(
                statement.compile(
                    dialect=cast(Any, postgresql).dialect(),
                    compile_kwargs={"literal_binds": False},
                )
            )
            captured_sql.append(compiled)
            return _FakeResult()

    async def scenario() -> None:
        repo = KnowledgeRepository(cast(Any, lambda: _RecordingSession()))
        await repo.search_keyword("owner", ["kb_1"], ("差旅", "报销"), 10)
        await repo.get_chunk("owner", "kb_1", "kchunk_1")

    asyncio.run(scenario())
    assert len(captured_sql) == 2

    kw_select = captured_sql[0].split("FROM")[0]
    assert "knowledge_chunks.embedding" not in kw_select
    assert "knowledge_chunks.search_document" not in kw_select
    assert "knowledge_chunks.content" in kw_select

    chunk_select = captured_sql[1].split("FROM")[0]
    assert "knowledge_chunks.embedding" not in chunk_select
    assert "knowledge_chunks.search_document" not in chunk_select
    assert "knowledge_chunks.content" in chunk_select
    assert "knowledge_documents.title" in chunk_select

    # 兼容测试的 _visible_chunks_query 仍返回 3 元组投影，但通过 load_only 跳过大列。
    compat_sql = str(
        _visible_chunks_query("owner", ["kb_1"]).compile(
            dialect=cast(Any, postgresql).dialect(),
        )
    )
    compat_select = compat_sql.split("FROM")[0]
    assert "knowledge_chunks.embedding" not in compat_select
    assert "knowledge_chunks.search_document" not in compat_select
    assert "knowledge_chunks.content" in compat_select


def test_select_keyword_tokens_filters_stopwords_preserves_identifiers_and_falls_back() -> None:
    """验证停用词/标点过滤、否定词与等级字母保留、超 20 词优先保尾部编号以及全过滤兜底。"""
    # 1. 过滤纯标点与高频结构停用词，同时保留 C++/foo:bar/(测试)、否定词及单字母绩效等级。
    mixed = (
        "请问",
        "的",
        "？",
        "...",
        "如何",
        "不",
        "未",
        "无",
        "非",
        "s",
        "b",
        "c",
        "C++",
        "foo:bar",
        "(测试)",
        "the",
        "is",
        "not",
        "报销",
        "标准",
        "吗",
    )
    selected = _select_keyword_tokens(mixed)
    assert "请问" not in selected
    assert "的" not in selected
    assert "？" not in selected
    assert "..." not in selected
    assert "如何" not in selected
    assert "the" not in selected
    assert "is" not in selected
    assert "吗" not in selected
    for kept in ("不", "未", "无", "非", "s", "b", "c", "C++", "foo:bar", "(测试)", "not", "报销"):
        assert kept in selected

    # 2. 超 20 词时必须优先保留位于列表尾部的复合编号、版本号与邮箱。
    many_words = tuple(f"实词{index}" for index in range(25)) + (
        "trv-2026-1102",
        "v2.1.0",
        "hr@athena.com",
        "foo:bar",
    )
    capped = _select_keyword_tokens(many_words)
    assert len(capped) == MAX_KEYWORD_TOKENS == 20
    assert "trv-2026-1102" in capped
    assert "v2.1.0" in capped
    assert "hr@athena.com" in capped
    assert "foo:bar" in capped
    # 原顺序保留且前 16 个实词与 4 个尾部标识符共同填满 20 词上限。
    assert capped[:16] == tuple(f"实词{index}" for index in range(16))

    # 3. 全部为停用词/标点时兜底回退原始去重非空词集，避免空查询误判。
    assert _select_keyword_tokens(("的", "吗", "？", "的")) == ("的", "吗", "？")
    assert _select_keyword_tokens(("", "   ")) == ()


def test_search_parallelizes_keyword_and_semantic_branches_and_subqueries() -> None:
    """验证关键词分支不等 Embedding RTT、向量就绪后并行兼容性探测与语义查库、取消不被吞。"""

    async def scenario() -> None:
        keyword_started = asyncio.Event()
        release_embedding = asyncio.Event()
        incompatible_entered = asyncio.Event()
        semantic_entered = asyncio.Event()

        class _Tokenizer:
            def tokenize(self, text: str) -> tuple[str, ...]:
                return tuple(text.split())

        class _BlockingEmbedding:
            async def embed(self, texts: list[str]) -> list[list[float]]:
                del texts
                # 只有当关键词查库真正启动后才放行 Embedding，若串行此处必定超时死锁。
                await asyncio.wait_for(keyword_started.wait(), timeout=2.0)
                await release_embedding.wait()
                return [[0.2] * 1024]

        class _ParallelRepo:
            async def validate_search_scope(self, owner_id: str, base_ids: list[str]) -> None:
                del owner_id, base_ids

            async def search_keyword(
                self, owner_id: str, base_ids: list[str], tokens: tuple[str, ...], limit: int
            ) -> tuple[SearchHit, ...]:
                del owner_id, base_ids, tokens, limit
                keyword_started.set()
                release_embedding.set()
                return (
                    SearchHit(
                        chunk_id="kw_1",
                        document_id="doc_1",
                        version_id="ver_1",
                        title="T",
                        text="keyword hit",
                        locators=(ChunkLocator(kind="text"),),
                        channels=("keyword",),
                        rank=1.0,
                        chunk_ordinal=0,
                        chunk_ids=("kw_1",),
                    ),
                )

            async def has_incompatible_profile(
                self, owner_id: str, base_ids: list[str], profile_id: str
            ) -> bool:
                del owner_id, base_ids, profile_id
                incompatible_entered.set()
                # 与 search_semantic 互等屏障，证明二者确实并行执行而非串行等待。
                await asyncio.wait_for(semantic_entered.wait(), timeout=2.0)
                return False

            async def search_semantic(
                self,
                owner_id: str,
                base_ids: list[str],
                vector: list[float],
                limit: int,
                profile_id: str | None = None,
                *,
                min_score: float = 0.0,
            ) -> tuple[SearchHit, ...]:
                del owner_id, base_ids, vector, limit, profile_id, min_score
                semantic_entered.set()
                await asyncio.wait_for(incompatible_entered.wait(), timeout=2.0)
                return (
                    SearchHit(
                        chunk_id="sem_1",
                        document_id="doc_2",
                        version_id="ver_2",
                        title="T2",
                        text="semantic hit",
                        locators=(ChunkLocator(kind="text"),),
                        channels=("semantic",),
                        rank=1.0,
                        chunk_ordinal=0,
                        chunk_ids=("sem_1",),
                    ),
                )

            async def filter_active_hits(
                self, owner_id: str, hits: tuple[SearchHit, ...]
            ) -> tuple[SearchHit, ...]:
                del owner_id
                return hits

        service = KnowledgeService(
            cast(KnowledgeRepositoryPort, _ParallelRepo()),
            _Tokenizer(),
            _BlockingEmbedding(),
        )
        result = await service.search("owner", ["kb_1"], "差旅 报销")
        assert result.channels == ("keyword", "semantic")
        assert {hit.chunk_id for hit in result.hits} == {"kw_1", "sem_1"}

        # 校验 CancelledError 绝不被吞为降级原因，并正确向上传播。
        class _CancellingEmbedding:
            async def embed(self, texts: list[str]) -> list[list[float]]:
                del texts
                raise asyncio.CancelledError

        cancelling_service = KnowledgeService(
            cast(KnowledgeRepositoryPort, _ParallelRepo()),
            _Tokenizer(),
            _CancellingEmbedding(),
        )
        with pytest.raises(asyncio.CancelledError):
            await cancelling_service.search("owner", ["kb_1"], "差旅")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# C.2 批写入、1A 旧版本索引清理、O(N) 分块与 Embedding 并发/重试测试
# ---------------------------------------------------------------------------


def test_publish_job_batches_pg_unnest_and_clears_old_version_index_columns_1a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 PostgreSQL 200 条/批 unnest 写入与 SQLite 实库 1A 旧版本索引置空且保留历史正文。"""
    now = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)

    # 第一部分：PostgreSQL 方言批量 unnest 与 1A UPDATE SQL 形态校验。
    pg_insert_batches: list[dict[str, Any]] = []
    pg_executed_sql: list[str] = []

    class _FakePgTx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            del exc_type, exc, tb

    class _FakePgPublishSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def __init__(self) -> None:
            self._scalar_queue: list[Any] = [
                "kb_1",
                SimpleNamespace(id="kb_1", deleted_time=None),
                SimpleNamespace(
                    id="doc_1",
                    deleted_time=None,
                    pending_version_id="ver_2",
                    active_version_id="ver_1",
                    updated_at=now,
                ),
                SimpleNamespace(
                    id="job_2",
                    state="processing",
                    fencing_token=1,
                    lease_until=now + timedelta(minutes=5),
                    error_code=None,
                    updated_at=now,
                ),
                SimpleNamespace(
                    id="ver_2",
                    profile_id="prof_1",
                    state="processing",
                    warnings=[],
                    chunk_count=0,
                    completed_at=None,
                    error_code=None,
                ),
                now,
            ]

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            del exc_type, exc, tb

        def begin(self) -> _FakePgTx:
            return _FakePgTx()

        async def scalar(self, _stmt: Any) -> Any:
            return self._scalar_queue.pop(0)

        async def scalars(self, stmt: Any) -> list[str]:
            compiled = str(stmt.compile(dialect=cast(Any, postgresql).dialect()))
            pg_executed_sql.append(compiled)
            return ["ver_1"]

        async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> None:
            compiled = str(stmt.compile(dialect=cast(Any, postgresql).dialect()))
            pg_executed_sql.append(compiled)
            if "FROM unnest(" in compiled and params is not None:
                pg_insert_batches.append(params)

    async def verify_pg_plan() -> None:
        repo = KnowledgeRepository(cast(Any, lambda: _FakePgPublishSession()))
        job = ClaimedKnowledgeJob(
            job_id="job_2",
            owner_id="user_1",
            version_id="ver_2",
            document_id="doc_1",
            title="制度",
            source_text="正文",
            source_key=None,
            source_filename=None,
            fencing_token=1,
            profile_id="prof_1",
        )
        chunks = [
            (f"chunk-{idx}", {"kind": "text", "start_line": idx + 1}, 5, f"chunk {idx}", [0.25] * 4)
            for idx in range(PUBLISH_CHUNK_BATCH_SIZE * 2 + 50)
        ]
        assert await repo.publish_job(job, chunks=chunks, warnings=[]) is True

    asyncio.run(verify_pg_plan())

    assert [len(batch["ids"]) for batch in pg_insert_batches] == [200, 200, 50]
    assert pg_insert_batches[0]["ordinals"][:3] == [0, 1, 2]
    assert pg_insert_batches[2]["ordinals"][0] == 400
    assert pg_insert_batches[0]["embeddings"][0] == "[0.25,0.25,0.25,0.25]"
    assert json.loads(pg_insert_batches[0]["locators"][0]) == {"kind": "text", "start_line": 1}

    update_stmts = [sql for sql in pg_executed_sql if sql.startswith("UPDATE knowledge_chunks")]
    assert len(update_stmts) == 1
    assert "knowledge_chunks.version_id IN" in update_stmts[0]
    assert "knowledge_chunks.document_id" not in update_stmts[0]

    # 第二部分：SQLite 真实数据库端到端验证批量写入 + 1A 置空旧版本索引列 + get_chunk 读历史正文。
    monkeypatch.setattr(
        func,
        "clock_timestamp",
        lambda: literal(now, type_=DateTime(timezone=True)),
    )

    async def verify_sqlite_e2e() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        executed_inserts: list[str] = []

        def _capture_sql(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            del conn, cursor, parameters, context, executemany
            if statement.lstrip().upper().startswith("INSERT INTO KNOWLEDGE_CHUNKS"):
                executed_inserts.append(statement)

        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="owner_1",
                        email="owner1@modulec.test",
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
                    KnowledgeBaseModel(
                        id="kb_1",
                        owner_id="owner_1",
                        name="制度库",
                        description=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                session.add(
                    KnowledgeDocumentModel(
                        id="doc_1",
                        owner_id="owner_1",
                        knowledge_base_id="kb_1",
                        title="差旅制度",
                        active_version_id="ver_1",
                        pending_version_id="ver_2",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        KnowledgeVersionModel(
                            id="ver_1",
                            document_id="doc_1",
                            owner_id="owner_1",
                            source_id=None,
                            source_text="旧版正文",
                            content_hash="1" * 64,
                            profile_id="prof_1",
                            processing_config={},
                            state="ready",
                            warnings=[],
                            chunk_count=1,
                            created_at=now - timedelta(hours=1),
                            completed_at=now - timedelta(hours=1),
                        ),
                        KnowledgeVersionModel(
                            id="ver_2",
                            document_id="doc_1",
                            owner_id="owner_1",
                            source_id=None,
                            source_text="新版正文",
                            content_hash="2" * 64,
                            profile_id="prof_1",
                            processing_config={},
                            state="processing",
                            warnings=[],
                            chunk_count=0,
                            created_at=now,
                            completed_at=None,
                        ),
                    ]
                )
                await session.flush()
                session.add(
                    KnowledgeChunkModel(
                        id="kchunk_old_1",
                        document_id="doc_1",
                        version_id="ver_1",
                        ordinal=0,
                        content="旧版本保留正文",
                        locator={"kind": "text", "start_line": 1},
                        token_count=7,
                        search_document="旧 版本 保留 正文",
                        embedding=[0.5] * 1024,
                    )
                )
                session.add(
                    KnowledgeJobModel(
                        id="job_2",
                        owner_id="owner_1",
                        kind="ingestion",
                        target_id="ver_2",
                        state="processing",
                        attempt=1,
                        lease_until=now + timedelta(minutes=5),
                        fencing_token=1,
                        next_attempt_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )

            repo = KnowledgeRepository(factory)
            job = ClaimedKnowledgeJob(
                job_id="job_2",
                owner_id="owner_1",
                version_id="ver_2",
                document_id="doc_1",
                title="差旅制度",
                source_text="新版正文",
                source_key=None,
                source_filename=None,
                fencing_token=1,
                profile_id="prof_1",
            )
            new_chunks = [
                (
                    f"新版片段-{idx}",
                    {"kind": "text", "start_line": idx + 1},
                    6,
                    f"新版 片段 {idx}",
                    [0.1] * 1024,
                )
                for idx in range(250)
            ]

            event.listen(engine.sync_engine, "before_cursor_execute", _capture_sql)
            try:
                published = await repo.publish_job(job, chunks=new_chunks, warnings=["ok"])
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", _capture_sql)

            assert published is True
            # 250 条分片应通过 2 次批量 INSERT（200 + 50）写入，而非 250 次逐行写入。
            assert len(executed_inserts) == 2

            async with factory() as session:
                old_row = (
                    await session.execute(
                        select(
                            KnowledgeChunkModel.content,
                            KnowledgeChunkModel.search_document,
                            KnowledgeChunkModel.embedding,
                        ).where(KnowledgeChunkModel.id == "kchunk_old_1")
                    )
                ).one()
                assert old_row[0] == "旧版本保留正文"
                assert old_row[1] == ""
                assert old_row[2] is None

                new_count = await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeChunkModel)
                    .where(
                        KnowledgeChunkModel.version_id == "ver_2",
                        KnowledgeChunkModel.embedding.is_not(None),
                    )
                )
                assert new_count == 250

            # 1A 方案必须 100% 保留 get_chunk 读取历史版本正文与定位器的契约。
            historic_hit = await repo.get_chunk("owner_1", "kb_1", "kchunk_old_1")
            assert historic_hit is not None
            assert historic_hit.text == "旧版本保留正文"
            assert historic_hit.version_id == "ver_1"
            assert historic_hit.locators[0].start_line == 1
        finally:
            await engine.dispose()

    asyncio.run(verify_sqlite_e2e())


def test_tiktoken_cursor_chunking_is_utf8_safe_and_o_n() -> None:
    """验证统一游标分块对中文/emoji 无 UTF-8 乱码、严守 512 上限、保留重叠且严格 O(N)。"""
    counter = TiktokenTokenCounter()
    assert counter.encode("") == []
    assert counter.count("literal <|endoftext|> 中文🚀") > 0

    # 构造混合中文、4 字节 emoji、长空格和 BPE 前导空格分裂词（如 " differ"/" includ"）的长文本。
    sample_text = (
        "差旅报销规范与财务审批流程 🚀🎉🔥 differ includ comple provid "
        "包含多字节中文字符与 <|endoftext|> 特殊串测试。"
    ) * 180

    locator = ChunkLocator(kind="text", start_line=1, end_line=10)
    chunks = _make_chunks([TextBlock(sample_text, locator)], counter)

    assert len(chunks) >= 5
    for chunk_text, chunk_locator, token_count in chunks:
        assert "\ufffd" not in chunk_text
        assert 1 <= token_count <= 512
        assert counter.count(chunk_text) <= 512
        assert chunk_locator["start_line"] == 1
    # 相邻分片应共享重叠尾部/头部上下文。
    assert chunks[0][0][-20:] in chunks[1][0]

    # 通用 TokenCounterPort 游标 + 指数倍增探测路径：校验严格 O(N) 局部窗口，绝不重扫尾部。
    class _BoundedProbeCounter:
        def __init__(self) -> None:
            self.call_lengths: list[int] = []

        def count(self, text: str) -> int:
            self.call_lengths.append(len(text))
            return len(text)

    fallback_counter = _BoundedProbeCounter()
    long_plain = "x" * 20_000
    fallback_chunks = _make_chunks([TextBlock(long_plain, locator)], fallback_counter)

    assert len(fallback_chunks) >= 40
    assert all(count <= 512 for _, _, count in fallback_chunks)
    assert fallback_chunks[0][0][-64:] == fallback_chunks[1][0][:64]
    # 第 1 次为块长度预判（20000），之后所有分片探测均受指数窗口限制（<= 1024），绝不重扫尾部。
    assert max(fallback_counter.call_lengths[1:]) <= 1024


def test_worker_respects_configured_embedding_batch_window_and_stops_on_lost_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """嵌入客户端暴露 .config.batch_size 时，Worker 应按 batch_size*4 窗口送检并保持失租中止。"""
    import app.knowledge.worker as worker_module

    class _ConfiguredEmbedding:
        def __init__(self, batch_size: int) -> None:
            self.config = SimpleNamespace(batch_size=batch_size)
            self.windows: list[int] = []

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.windows.append(len(texts))
            return [[0.1] * 1024 for _ in texts]

    class _Repo:
        def __init__(self) -> None:
            self.published = False

        async def claim_job(
            self, *, lease_seconds: int, owner_concurrency: int = 2
        ) -> ClaimedKnowledgeJob | None:
            del lease_seconds, owner_concurrency
            return ClaimedKnowledgeJob(
                "job_1",
                "owner_1",
                "ver_1",
                "doc_1",
                "title",
                None,
                "ksrc_1",
                "multi.pdf",
                1,
            )

        async def heartbeat_job(
            self, job_id: str, fencing_token: int, *, lease_seconds: int
        ) -> bool:
            del job_id, fencing_token, lease_seconds
            return True

        async def publish_job(
            self,
            job: ClaimedKnowledgeJob,
            *,
            chunks: list[tuple[str, dict[str, object], int, str, list[float]]],
            warnings: list[str],
        ) -> bool:
            del job, warnings
            self.published = len(chunks) == 70
            return self.published

        async def fail_job(
            self, job: ClaimedKnowledgeJob, *, error_code: str, retryable: bool
        ) -> None:
            raise AssertionError(f"unexpected failure: {error_code} {retryable}")

        async def finish_cleanup_job(self, job: ClaimedKnowledgeJob) -> bool:
            del job
            return True

        async def is_storage_key_referenced(self, storage_key: str) -> bool:
            del storage_key
            return False

        async def find_referenced_storage_keys(self, storage_keys: Sequence[str]) -> set[str]:
            del storage_keys
            return set()

    class _MultiPageParser:
        def create(self) -> Self:
            return self

        async def parse(self, filename: str, content: bytes) -> ParsedDocument:
            del filename, content
            return ParsedDocument(
                tuple(
                    TextBlock(f"page {idx}", ChunkLocator(kind="pdf", page=idx))
                    for idx in range(1, 71)
                )
            )

    class _Store:
        async def put(self, key: str, content: bytes) -> None:
            del key, content

        async def get(self, key: str) -> bytes:
            del key
            return b"pdf"

        async def delete(self, key: str) -> None:
            del key

        async def iter_orphan_candidates(self, *, older_than_seconds: int = 86400) -> Any:
            del older_than_seconds
            if False:
                yield ""

    class _Tokenizer:
        def tokenize(self, text: str) -> tuple[str, ...]:
            return tuple(text.split())

    class _Counter:
        def count(self, text: str) -> int:
            return len(text)

    async def scenario() -> None:
        repo = _Repo()
        embedding = _ConfiguredEmbedding(batch_size=16)
        worker = KnowledgeWorker(
            repo,
            _MultiPageParser(),
            _Store(),
            embedding,
            _Tokenizer(),
            _Counter(),
            isolate_parsing=False,
        )
        assert await worker.run_once() is True
        assert repo.published is True
        # batch_size=16 * 并发系数 4 = 64 窗口，70 个 chunk 分为 [64, 6]。
        assert embedding.windows == [64, 6]

        # 失租后在窗口间立即中止，不提交第二轮窗口。
        monkeypatch.setattr(worker_module, "HEARTBEAT_SECONDS", 0.01)
        lost_event = asyncio.Event()
        repo2 = _Repo()

        async def lose_lease(job_id: str, fencing_token: int, *, lease_seconds: int) -> bool:
            del job_id, fencing_token, lease_seconds
            lost_event.set()
            return False

        repo2.heartbeat_job = lose_lease  # type: ignore[method-assign]
        embedding2 = _ConfiguredEmbedding(batch_size=16)

        async def slow_embed(texts: list[str]) -> list[list[float]]:
            embedding2.windows.append(len(texts))
            await lost_event.wait()
            return [[0.1] * 1024 for _ in texts]

        embedding2.embed = slow_embed  # type: ignore[method-assign]
        worker2 = KnowledgeWorker(
            repo2,
            _MultiPageParser(),
            _Store(),
            embedding2,
            _Tokenizer(),
            _Counter(),
            isolate_parsing=False,
        )
        assert await worker2.run_once() is True
        assert embedding2.windows == [64]
        assert repo2.published is False

    asyncio.run(scenario())


def test_embedding_client_concurrent_batches_gzip_and_transient_transport_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Semaphore(4) 并发保序、失败取消同组、gzip 压缩协商与 SDK 瞬态网络重试。"""

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("openai._base_client.anyio.sleep", _fast_sleep)

    # 1. 并发调度（上限 4）且逆序完成时结果仍严格保序。
    async def verify_concurrency_and_order() -> None:
        in_flight = 0
        max_in_flight = 0
        gate = asyncio.Event()

        async def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal in_flight, max_in_flight
            body = json.loads(request.content.decode("utf-8"))
            idx = int(body["input"][0])
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if in_flight >= 4:
                gate.set()
            try:
                await asyncio.wait_for(gate.wait(), timeout=2.0)
                # 故意让小下标批次更晚返回，验证按批次下标保序拼接。
                await asyncio.sleep(0.01 * (8 - idx))
            finally:
                in_flight -= 1
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"index": 0, "embedding": [float(idx), 1.0, 2.0, 3.0]}],
                },
            )

        config = OpenAICompatibleEmbeddingConfig(
            base_url="http://127.0.0.1:8000/v1",
            dimensions=4,
            batch_size=1,
        )
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
        )
        try:
            vectors = await client.embed([str(i) for i in range(8)])
        finally:
            await client.aclose()

        assert max_in_flight == 4
        assert vectors == [[float(i), 1.0, 2.0, 3.0] for i in range(8)]

    asyncio.run(verify_concurrency_and_order())

    # 2. 任一批失败立即取消同组未完成批次。
    async def verify_sibling_cancellation() -> None:
        cancelled_siblings = 0
        sibling_started = asyncio.Event()

        async def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal cancelled_siblings
            body = json.loads(request.content.decode("utf-8"))
            token = body["input"][0]
            if token == "fail":
                await asyncio.wait_for(sibling_started.wait(), timeout=2.0)
                return httpx2.Response(400, text="bad request")
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_siblings += 1
                raise
            return httpx2.Response(200, json={"object": "list", "data": []})

        config = OpenAICompatibleEmbeddingConfig(
            base_url="http://127.0.0.1:8000/v1",
            dimensions=4,
            batch_size=1,
        )
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(handler),
        )
        try:
            with pytest.raises(EmbeddingUpstreamError, match="embedding_request_invalid"):
                await client.embed(["slow-1", "slow-2", "fail", "slow-3"])
        finally:
            await client.aclose()
        assert cancelled_siblings >= 1

    asyncio.run(verify_sibling_cancellation())

    # 3. 协商 gzip 压缩由标准 httpx2 自动解压。
    async def verify_gzip_negotiation() -> None:
        seen_accept_encodings: list[str] = []
        normal_payload = json.dumps(
            {"object": "list", "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]}
        ).encode("utf-8")
        normal_gz = gzip.compress(normal_payload)

        def ok_gzip_handler(request: httpx2.Request) -> httpx2.Response:
            seen_accept_encodings.append(request.headers.get("accept-encoding", ""))
            return httpx2.Response(
                200,
                content=normal_gz,
                headers={"content-type": "application/json", "content-encoding": "gzip"},
            )

        config = OpenAICompatibleEmbeddingConfig(
            base_url="http://127.0.0.1:8000/v1",
            dimensions=4,
        )
        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(ok_gzip_handler),
        )
        try:
            vectors = await client.embed(["hello"])
            assert vectors == [[0.1, 0.2, 0.3, 0.4]]
        finally:
            await client.aclose()

        assert seen_accept_encodings and "gzip" in seen_accept_encodings[0].lower()
        assert seen_accept_encodings[0].lower() != "identity"

    asyncio.run(verify_gzip_negotiation())

    # 4. 瞬态 NetworkError / RemoteProtocolError 由 SDK 重试，耗尽报 retryable=True 且不泄密钥。
    async def verify_transient_transport_retry() -> None:
        secret = "sk-transient-secret-999"
        config = OpenAICompatibleEmbeddingConfig(
            base_url="http://127.0.0.1:8000/v1",
            api_key=secret,
            dimensions=4,
        )

        # 4a. 前两次遭遇 RemoteProtocolError / ConnectError，第 3 次恢复成功。
        attempts = 0

        def flaky_handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx2.ConnectError(f"reset {request.headers.get('authorization')}")
            if attempts == 2:
                raise httpx2.RemoteProtocolError(f"eof {request.headers.get('authorization')}")
            return httpx2.Response(
                200,
                json={"object": "list", "data": [{"index": 0, "embedding": [1.0, 2.0, 3.0, 4.0]}]},
            )

        client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(flaky_handler),
        )
        try:
            assert await client.embed(["retry"]) == [[1.0, 2.0, 3.0, 4.0]]
        finally:
            await client.aclose()
        assert attempts == 3

        # 4b. 持续 RemoteProtocolError 耗尽后抛 retryable=True 且截断异常链。
        exhausted_attempts = 0

        def broken_handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal exhausted_attempts
            exhausted_attempts += 1
            raise httpx2.RemoteProtocolError(f"eof {request.headers.get('authorization')}")

        broken_client = OpenAICompatibleEmbeddingClient(
            config,
            transport=httpx2.MockTransport(broken_handler),
        )
        try:
            with pytest.raises(
                EmbeddingUpstreamError, match="embedding_transport_error"
            ) as exc_info:
                await broken_client.embed(["fail"])
        finally:
            await broken_client.aclose()
        assert exhausted_attempts == 3
        assert exc_info.value.retryable is True
        assert exc_info.value.__cause__ is None
        assert secret not in str(exc_info.value)

    asyncio.run(verify_transient_transport_retry())


# ---------------------------------------------------------------------------
# C.3 常驻解析进程池与数字 PDF 图像去重/免 PNG 往返测试
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="spawn CLI import differs on Windows")
def test_isolated_parser_pool_reuses_recycles_handles_errors_and_closes_cleanly() -> None:
    """验证标准库隔离解析池复用、任务上限轮换、错误脱敏、超时强杀/崩溃自愈与优雅关闭。"""

    def _parse_meta(doc: ParsedDocument) -> tuple[int, int]:
        parts = dict(item.split("=", 1) for item in doc.blocks[0].text.split(";"))
        return int(parts["pid"]), int(parts["calls"])

    # 1. 同进程复用 + 业务 ValueError 不破坏复用 + 达到 max_tasks_per_child=3 自动轮换新 PID。
    async def verify_reuse_and_task_limit() -> None:
        pool = _IsolatedParserPool(
            _PidReportingParserFactory(),
            max_workers=1,
            timeout_seconds=15,
            max_tasks_per_child=3,
        )
        try:
            first = await pool.parse("a.txt", b"1")
            pid1, calls1 = _parse_meta(first)
            assert calls1 == 1

            # 合法业务 ValueError 不销毁健康子进程，计为 1 次任务。
            with pytest.raises(ValueError, match="knowledge_document_invalid"):
                await pool.parse("invalid.txt", b"bad")

            second = await pool.parse("b.txt", b"2")
            pid2, calls2 = _parse_meta(second)
            assert pid2 == pid1
            assert calls2 == 3  # 同一子进程处理了第 1、2、3 次请求

            # 第 3 次完成后由 max_tasks_per_child=3 自动轮换，第 4 次落入新 PID。
            fourth = await pool.parse("c.txt", b"3")
            pid4, calls4 = _parse_meta(fourth)
            assert pid4 != pid1
            assert calls4 == 1
        finally:
            await pool.aclose()
            # 重复 aclose 幂等。
            await pool.aclose()

    asyncio.run(verify_reuse_and_task_limit())

    # 2. MemoryError 与意外 RuntimeError 均脱敏并主动回收重建子进程池。
    async def verify_error_sanitization_and_recycle() -> None:
        pool = _IsolatedParserPool(
            _PidReportingParserFactory(),
            max_workers=1,
            timeout_seconds=15,
        )
        try:
            pid_initial, _ = _parse_meta(await pool.parse("ok1.txt", b"x"))

            with pytest.raises(ValueError, match="knowledge_parse_memory_limit") as oom_exc:
                await pool.parse("oom.txt", b"x")
            assert "secret_internal_buffer_details" not in str(oom_exc.value)

            pid_after_oom, _ = _parse_meta(await pool.parse("ok2.txt", b"x"))
            assert pid_after_oom != pid_initial

            with pytest.raises(RuntimeError, match="knowledge_parser_transient") as boom_exc:
                await pool.parse("boom.txt", b"x")
            assert "/etc/secret/path" not in str(boom_exc.value)

            pid_after_boom, _ = _parse_meta(await pool.parse("ok3.txt", b"x"))
            assert pid_after_boom != pid_after_oom
        finally:
            await pool.aclose()

    asyncio.run(verify_error_sanitization_and_recycle())

    # 3. 超时强杀卡死子进程并重建，以及子进程崩溃后自动自愈重建。
    async def verify_timeout_and_crash_recovery() -> None:
        pool = _IsolatedParserPool(
            _PidReportingParserFactory(),
            max_workers=1,
            timeout_seconds=2,
        )
        try:
            pid_before_timeout, _ = _parse_meta(await pool.parse("ok_before.txt", b"x"))
            assert pool._current is not None
            stuck_processes = list((getattr(pool._current, "_processes", None) or {}).values())
            assert stuck_processes and any(proc.is_alive() for proc in stuck_processes)

            with pytest.raises(TimeoutError, match="knowledge_parse_timeout"):
                await pool.parse("slow.txt", b"x")
            # 超时后旧池卡死子进程已被显式强杀回收，不会遗留后台阻塞进程。
            assert all(not proc.is_alive() for proc in stuck_processes)
            assert pool._current is None

            pid_after_timeout, _ = _parse_meta(await pool.parse("ok_after_timeout.txt", b"x"))
            assert pid_after_timeout > 0
            assert pid_after_timeout != pid_before_timeout

            with pytest.raises(RuntimeError, match="knowledge_parser_process_failed"):
                await pool.parse("crash.txt", b"x")
            assert pool._current is None

            pid_after_crash, _ = _parse_meta(await pool.parse("ok_after_crash.txt", b"x"))
            assert pid_after_crash != pid_after_timeout
        finally:
            await pool.aclose()

    asyncio.run(verify_timeout_and_crash_recovery())

    # 4. 未显式调用 aclose 时，weakref.finalize 在 GC 回收后必须自动终止残留子进程防挂起。
    async def verify_weakref_finalizer_cleans_up_without_aclose() -> list[Any]:
        pool = _IsolatedParserPool(
            _PidReportingParserFactory(),
            max_workers=1,
            timeout_seconds=15,
        )
        await pool.parse("gc.txt", b"x")
        assert pool._current is not None
        processes = list((getattr(pool._current, "_processes", None) or {}).values())
        assert processes and all(proc.is_alive() for proc in processes)
        return processes

    leaked_processes = asyncio.run(verify_weakref_finalizer_cleans_up_without_aclose())
    gc.collect()
    assert all(not proc.is_alive() for proc in leaked_processes)


@pytest.mark.skipif(os.name == "nt", reason="spawn CLI import differs on Windows")
def test_isolated_parser_pool_concurrent_recycle_still_kills_timed_out_sibling() -> None:
    """max_workers=2 下同池一任务触发脏轮换退位后，卡死的兄弟任务超时仍须 100% 强杀回收。"""

    def _parse_meta(doc: ParsedDocument) -> tuple[int, int]:
        parts = dict(item.split("=", 1) for item in doc.blocks[0].text.split(";"))
        return int(parts["pid"]), int(parts["calls"])

    async def scenario() -> None:
        pool = _IsolatedParserPool(
            _PidReportingParserFactory(),
            max_workers=2,
            timeout_seconds=2,
        )
        try:
            # 预热初始进程池并抓取旧池引用，随后并发提交卡死慢任务与触发脏轮换的失败任务。
            await pool.parse("warmup.txt", b"x")
            old_executor = pool._current
            assert old_executor is not None

            slow_task = asyncio.create_task(pool.parse("slow.txt", b"x"))
            boom_task = asyncio.create_task(pool.parse("boom.txt", b"x"))

            with pytest.raises(RuntimeError, match="knowledge_parser_transient"):
                await boom_task

            # boom.txt 仅将旧池退位（self._current 置空），不得提前清空 _processes。
            assert pool._current is None
            assert old_executor in pool._executors
            old_processes = list((getattr(old_executor, "_processes", None) or {}).values())
            assert old_processes and any(proc.is_alive() for proc in old_processes)
            old_pids = {proc.pid for proc in old_processes if proc.pid is not None}

            # 随后慢兄弟任务超时，必须完整快照并强杀旧池全部子进程且将其移出跟踪集合。
            with pytest.raises(TimeoutError, match="knowledge_parse_timeout"):
                await slow_task

            assert old_executor not in pool._executors
            assert all(not proc.is_alive() for proc in old_processes)

            # 后续新解析自动懒重建全新进程池并成功返回新 PID。
            new_pid, calls = _parse_meta(await pool.parse("ok_after_concurrent.txt", b"x"))
            assert new_pid > 0
            assert new_pid not in old_pids
            assert calls == 1
        finally:
            await pool.aclose()

    asyncio.run(scenario())


def test_knowledge_worker_entry_closes_worker_pool_before_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """knowledge_worker.run_worker 退出清理必须在 counter.close() 之前调用 worker.aclose()。"""
    import knowledge_worker

    import bootstrap.database as bootstrap_database
    import bootstrap.knowledge as bootstrap_knowledge
    import bootstrap.knowledge_worker as bootstrap_kw

    events: list[str] = []

    class _FakeDb:
        def __init__(self, _url: str, **_kwargs: Any) -> None:
            self.session_factory = object()

        async def dispose(self) -> None:
            events.append("database")

    class _FakeStore:
        @classmethod
        def from_environment(cls) -> Self:
            return cls()

        async def start(self) -> None:
            events.append("store_start")

        async def aclose(self) -> None:
            events.append("store_close")

    class _FakeEmbedding:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def start(self) -> None:
            events.append("embedding_start")

        async def aclose(self) -> None:
            events.append("embedding_close")

    class _FakeCounter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def start(self) -> None:
            events.append("counter_start")

        def close(self) -> None:
            events.append("counter_close")

    class _SpyWorker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def aclose(self) -> None:
            events.append("worker_aclose")

    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://invalid/test")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setattr(bootstrap_database, "Database", _FakeDb)
    monkeypatch.setattr(bootstrap_knowledge, "MinioOriginalStore", _FakeStore)
    monkeypatch.setattr(bootstrap_knowledge, "OpenAICompatibleEmbeddingClient", _FakeEmbedding)
    monkeypatch.setattr(bootstrap_knowledge, "TiktokenTokenCounter", _FakeCounter)
    monkeypatch.setattr(bootstrap_kw, "KnowledgeWorker", _SpyWorker)

    async def scenario() -> None:
        stop_event = asyncio.Event()
        stop_event.set()
        await knowledge_worker.run_worker(stop_event=stop_event)

    asyncio.run(scenario())
    assert events == [
        "store_start",
        "embedding_start",
        "counter_start",
        "worker_aclose",
        "counter_close",
        "embedding_close",
        "store_close",
        "database",
    ]


def _multi_page_pdf_with_images(pages: list[tuple[str, bytes | None, tuple[int, int]]]) -> bytes:
    """构造多页测试 PDF，每页可附带指定像素尺寸的 JPEG 内嵌图元。"""
    objects: list[bytes] = []
    # 1: Catalog, 2: Pages, 3: Font
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"PLACEHOLDER_PAGES")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_obj_nums: list[int] = []
    for text, img_bytes, (w, h) in pages:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        page_num = len(objects) + 1
        content_num = page_num + 1
        page_obj_nums.append(page_num)
        if img_bytes is not None:
            img_num = content_num + 1
            stream = (
                f"BT /F1 12 Tf 40 760 Td ({escaped}) Tj ET\nq 100 0 0 100 40 500 cm /Im1 Do Q"
            ).encode()
            page_dict = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
                f"<< /Font << /F1 3 0 R >> /XObject << /Im1 {img_num} 0 R >> >> "
                f"/Contents {content_num} 0 R >>"
            ).encode()
            content_obj = (
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
            img_obj = (
                (
                    f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                    f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
                    f"{len(img_bytes)} >>\nstream\n"
                ).encode()
                + img_bytes
                + b"\nendstream"
            )
            objects.extend([page_dict, content_obj, img_obj])
        else:
            stream = f"BT /F1 12 Tf 40 760 Td ({escaped}) Tj ET".encode()
            page_dict = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
                f"<< /Font << /F1 3 0 R >> >> /Contents {content_num} 0 R >>"
            ).encode()
            content_obj = (
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
            objects.extend([page_dict, content_obj])

    kids = " ".join(f"{num} 0 R" for num in page_obj_nums)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_obj_nums)} >>".encode()

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(output)


def test_pdf_parser_deduplicates_images_preserves_sparse_page_guard_and_skips_png_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证跨页图元哈希去重、命中缓存同步更新 image_ocr_succeeded 防全页回退、跳过 PNG 编解码。"""
    # 生成两张 64x64 JPEG（不同像素以区分哈希）：图 A 含文本，图 B 空白。
    buf_a = io.BytesIO()
    Image.new("RGB", (64, 64), (20, 40, 200)).save(buf_a, format="JPEG")
    img_a = buf_a.getvalue()

    buf_b = io.BytesIO()
    Image.new("RGB", (64, 64), (240, 240, 240)).save(buf_b, format="JPEG")
    img_b = buf_b.getvalue()

    pdf_bytes = _multi_page_pdf_with_images(
        [
            # 第 1 页：充足正文 + 含文字图 A（首次 OCR，缓存 True）
            ("Page 1 has plenty of native text content", img_a, (64, 64)),
            # 第 2 页：稀疏正文 + 相同图 A（命中缓存 True -> 跳过图 OCR 且不触发全页 OCR）
            ("P2", img_a, (64, 64)),
            # 第 3 页：充足正文 + 空白图 B（首次 OCR 返回空，缓存 False）
            ("Page 3 has plenty of native text content", img_b, (64, 64)),
            # 第 4 页：稀疏正文 + 相同空白图 B（命中缓存 False -> 跳过图 OCR 但仍触发全页回退 OCR）
            ("P4", img_b, (64, 64)),
        ]
    )

    class _ColorAwareOcr:
        def __init__(self) -> None:
            self.calls = 0

        def predict(self, *, input: Any) -> list[SimpleNamespace]:
            self.calls += 1
            # 64x64 蓝色图 A 返回文本，64x64 近白色图 B 返回空，大尺寸整页渲染返回整页文本。
            height, width = input.shape[0], input.shape[1]
            if height > 100 or width > 100:
                return [SimpleNamespace(json={"res": {"rec_texts": ["整页回退文本"]}})]
            mean_blue = float(input[:, :, 2].mean())
            if mean_blue < 220.0:
                return [SimpleNamespace(json={"res": {"rec_texts": ["页眉徽标文字"]}})]
            return [SimpleNamespace(json={"res": {"rec_texts": []}})]

    # 禁用 PIL Image.save，硬性验证 PDF 嵌入式图元与整页回退 OCR 均彻底跳过 PNG 编解码往返。
    def _forbid_png_save(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("PDF OCR must not round-trip through PNG save")

    monkeypatch.setattr(Image.Image, "save", _forbid_png_save)

    ocr = _ColorAwareOcr()
    parser = LocalDocumentParser(min_pdf_chars=24, ocr_engine=ocr)
    result = asyncio.run(parser.parse("dedup.pdf", pdf_bytes))

    # 总计仅 3 次 OCR：第 1 页图 A + 第 3 页图 B + 第 4 页全页回退；
    # 第 2 页图 A 与第 4 页图 B 均命中跨页哈希缓存，第 2 页未误触发全页回退！
    assert ocr.calls == 3
    media_ids = [block.locator.media_id for block in result.blocks if block.locator.media_id]
    assert "pdf-page-1-image-1" in media_ids
    assert "pdf-page-2" not in media_ids
    assert "pdf-page-4" in media_ids


def test_pdf_parser_prefilters_tiny_images_with_get_px_size_and_closes_bitmap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_px_size 小于 32px 时应在 get_bitmap 之前短路，且非小图异常时仍必调用 bitmap.close。"""
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    bitmap_calls = 0
    bitmap_closed = 0

    class _TinyImageObj:
        def get_px_size(self) -> tuple[int, int]:
            return (16, 128)

        def get_bitmap(self) -> Any:
            nonlocal bitmap_calls
            bitmap_calls += 1
            raise AssertionError("tiny image must be filtered before get_bitmap()")

    class _FakeBitmap:
        def to_pil(self) -> Image.Image:
            return Image.new("RGB", (64, 64), "blue")

        def close(self) -> None:
            nonlocal bitmap_closed
            bitmap_closed += 1

    class _NormalImageObj:
        def get_px_size(self) -> tuple[int, int]:
            return (64, 64)

        def get_bitmap(self) -> _FakeBitmap:
            nonlocal bitmap_calls
            bitmap_calls += 1
            return _FakeBitmap()

    class _FakeTextPage:
        def get_text_range(self) -> str:
            return "Enough native text content on page"

    class _FakePage:
        def get_textpage(self) -> _FakeTextPage:
            return _FakeTextPage()

        def get_objects(self) -> list[Any]:
            return [_TinyImageObj(), _NormalImageObj()]

        def close(self) -> None:
            return None

    class _FakePdfDoc:
        def __init__(self, _content: bytes) -> None:
            self._pages = [_FakePage()]

        def __len__(self) -> int:
            return len(self._pages)

        def __getitem__(self, idx: int) -> _FakePage:
            return self._pages[idx]

        def close(self) -> None:
            return None

    class _FailingOcr:
        def predict(self, *, input: Any) -> list[Any]:
            del input
            raise RuntimeError("ocr_boom")

    monkeypatch.setattr(pdfium, "PdfDocument", _FakePdfDoc)
    parser = LocalDocumentParser(ocr_engine=_FailingOcr())
    with pytest.raises(RuntimeError, match="ocr_boom"):
        asyncio.run(parser.parse("prefilter.pdf", b"%PDF-fake"))

    # 16x128 装饰图未触发 get_bitmap，仅正常图调用 1 次且异常路径仍严格关闭 bitmap。
    assert bitmap_calls == 1
    assert bitmap_closed == 1
