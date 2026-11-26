"""知识检索降级、RRF邻接合并和上下文预算。"""

from __future__ import annotations

import asyncio
import math
from typing import cast

import pytest

from adapter.knowledge.repository import _retry_delay_seconds
from app.knowledge.service import KnowledgeSearchUnavailableError, KnowledgeService
from domain.knowledge.model import ChunkLocator, SearchHit
from domain.knowledge.ports import KnowledgeRepositoryPort


class _Tokenizer:
    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(text.split())


class _Counter:
    def count(self, text: str) -> int:
        return len(text)


class _Embedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024]


class _InvalidEmbedding(_Embedding):
    def __init__(self, value: float) -> None:
        self.value = value

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vector = [self.value] * 1024 if self.value == 0 else [0.1] * 1024
        if self.value != 0:
            vector[0] = self.value
        return [vector]


class _Repository:
    def __init__(self) -> None:
        self.keyword_hits: tuple[SearchHit, ...] = ()
        self.semantic_hits: tuple[SearchHit, ...] = ()
        self.keyword_error: Exception | None = None
        self.semantic_error: Exception | None = None
        self.keyword_calls = 0
        self.semantic_calls = 0
        self.incompatible_profile = False

    async def validate_search_scope(self, owner_id: str, base_ids: list[str]) -> None:
        assert owner_id == "owner"
        assert base_ids == ["base"]

    async def search_keyword(
        self, owner_id: str, base_ids: list[str], tokens: tuple[str, ...], limit: int
    ) -> tuple[SearchHit, ...]:
        self.keyword_calls += 1
        assert tokens == ("query",)
        if self.keyword_error:
            raise self.keyword_error
        return self.keyword_hits

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
        del min_score
        self.semantic_calls += 1
        assert len(vector) == 1024
        if self.semantic_error:
            raise self.semantic_error
        return self.semantic_hits

    async def has_incompatible_profile(
        self, owner_id: str, base_ids: list[str], profile_id: str
    ) -> bool:
        return self.incompatible_profile

    async def filter_active_hits(
        self, owner_id: str, hits: tuple[SearchHit, ...]
    ) -> tuple[SearchHit, ...]:
        del owner_id
        return hits


def _hit(chunk: str, ordinal: int, text: str, channel: str, rank: int) -> SearchHit:
    return SearchHit(
        chunk_id=chunk,
        document_id="doc",
        version_id="version",
        title="title",
        text=text,
        locators=(ChunkLocator(kind="text", start_line=ordinal + 1),),
        channels=(channel,),
        rank=float(rank),
        chunk_ordinal=ordinal,
        chunk_ids=(chunk,),
    )


def _service(repository: _Repository) -> KnowledgeService:
    return KnowledgeService(
        cast(KnowledgeRepositoryPort, repository),
        _Tokenizer(),
        _Embedding(),
        counter=_Counter(),
    )


def test_search_runs_channels_in_separate_calls_and_merges_adjacent_hits_with_budget() -> None:
    repository = _Repository()
    repository.keyword_hits = (
        _hit("chunk-1", 0, "abc", "keyword", 1),
        _hit("chunk-3", 3, "XYZ", "keyword", 2),
    )
    repository.semantic_hits = (_hit("chunk-2", 1, "def", "semantic", 1),)

    result = asyncio.run(_service(repository).search("owner", ["base"], "query", context_budget=8))

    assert repository.keyword_calls == repository.semantic_calls == 1
    assert result.channels == ("keyword", "semantic")
    assert result.hits[0].chunk_ids == ("chunk-1", "chunk-2")
    assert result.hits[0].text == "abc\ndef"
    assert sum(len(hit.text) for hit in result.hits) <= 8


def test_search_degrades_to_semantic_when_keyword_channel_fails() -> None:
    repository = _Repository()
    repository.keyword_error = RuntimeError("database unavailable")
    repository.semantic_hits = (_hit("chunk-1", 0, "proof", "semantic", 1),)

    result = asyncio.run(_service(repository).search("owner", ["base"], "query"))

    assert result.channels == ("semantic",)
    assert result.degraded_reasons == ("keyword_search_unavailable",)
    assert result.hits[0].chunk_id == "chunk-1"


def test_search_degrades_to_keyword_when_embedding_channel_fails() -> None:
    repository = _Repository()
    repository.semantic_error = RuntimeError("vector index unavailable")
    repository.keyword_hits = (_hit("chunk-1", 0, "proof", "keyword", 1),)

    result = asyncio.run(_service(repository).search("owner", ["base"], "query"))

    assert result.channels == ("keyword",)
    assert result.degraded_reasons == ("semantic_search_unavailable",)


def test_search_raises_unavailable_when_both_channels_fail() -> None:
    repository = _Repository()
    repository.keyword_error = RuntimeError("keyword failure")
    repository.semantic_error = RuntimeError("semantic failure")

    with pytest.raises(KnowledgeSearchUnavailableError):
        asyncio.run(_service(repository).search("owner", ["base"], "query"))


@pytest.mark.parametrize("invalid", [math.nan, math.inf, 0.0])
def test_invalid_semantic_vector_degrades_without_querying_database(invalid: float) -> None:
    repository = _Repository()
    service = KnowledgeService(
        cast(KnowledgeRepositoryPort, repository),
        _Tokenizer(),
        _InvalidEmbedding(invalid),
    )
    result = asyncio.run(service.search("owner", ["base"], "query"))
    assert repository.semantic_calls == 0
    assert result.channels == ("keyword",)
    assert "embedding_profile_mismatch" in result.degraded_reasons


def test_profile_incompatibility_is_reported_without_flagging_empty_scope() -> None:
    empty_repository = _Repository()
    empty_result = asyncio.run(_service(empty_repository).search("owner", ["base"], "query"))
    assert "semantic_profile_incompatible" not in empty_result.degraded_reasons

    incompatible_repository = _Repository()
    incompatible_repository.incompatible_profile = True
    result = asyncio.run(_service(incompatible_repository).search("owner", ["base"], "query"))
    assert result.channels == ("keyword",)
    assert "semantic_profile_incompatible" in result.degraded_reasons


@pytest.mark.parametrize(
    ("attempt", "base"),
    [(1, 5), (2, 30), (3, 120), (5, 120)],
)
def test_retry_delay_uses_exponential_backoff_with_jitter(attempt: int, base: int) -> None:
    assert _retry_delay_seconds(attempt, 0.8) == base * 0.8
    assert _retry_delay_seconds(attempt, 1.2) == base * 1.2
    assert base * 0.8 <= _retry_delay_seconds(attempt) <= base * 1.2


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("C++", "'C++'"),
        ("foo:bar", "'foo:bar'"),
        ("(测试)", "'(测试)'"),
        (r"C:\Users\test", r"'C:\\Users\\test'"),
        ("C:\\Users\\test\\", "'C:\\\\Users\\\\test\\\\'"),
        ("a'b\\c", "'a''b\\\\c'"),
    ],
)
def test_tsquery_token_escapes_backslashes_and_single_quotes(token: str, expected: str) -> None:
    """Knowledge 全文检索词元必须先转义反斜杠再转义单引号，防止破坏 tsquery 语法。"""
    from adapter.knowledge.repository import _tsquery_token

    assert _tsquery_token(token) == expected


def test_knowledge_service_passes_semantic_min_score_when_supported_and_validates_bounds() -> None:
    """支持 min_score 参数的仓储应收到相关性阈值，且非法阈值在构造期被拒绝。"""

    class _ThresholdRepository(_Repository):
        def __init__(self) -> None:
            super().__init__()
            self.received_min_score: float | None = None

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
            self.received_min_score = min_score
            return await super().search_semantic(owner_id, base_ids, vector, limit, profile_id)

    repo = _ThresholdRepository()
    repo.semantic_hits = (_hit("chunk-1", 0, "relevant", "semantic", 1),)
    service = KnowledgeService(
        cast(KnowledgeRepositoryPort, repo),
        _Tokenizer(),
        _Embedding(),
        counter=_Counter(),
        semantic_min_score=0.42,
    )

    result = asyncio.run(service.search("owner", ["base"], "query"))
    assert repo.received_min_score == pytest.approx(0.42)
    assert len(result.hits) == 1

    for invalid_score in (-0.01, 1.01, math.nan, math.inf):
        with pytest.raises(ValueError, match="knowledge_semantic_min_score_invalid"):
            KnowledgeService(
                cast(KnowledgeRepositoryPort, repo),
                _Tokenizer(),
                _Embedding(),
                semantic_min_score=invalid_score,
            )


def test_knowledge_repository_search_semantic_adds_distance_filter_for_min_score() -> None:
    """min_score > 0 时语义检索 SQL 应下推 cosine_distance <= 1 - min_score 过滤低相关噪声。"""
    from types import TracebackType
    from typing import Any, Self

    from sqlalchemy.dialects import postgresql

    from adapter.knowledge.repository import KnowledgeRepository

    compiled_sql: list[str] = []

    class _Session:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            del exc_type, exc, traceback

        async def execute(self, statement: Any) -> list[object]:
            compiled = str(
                statement.compile(
                    dialect=cast(Any, postgresql).dialect(),
                    compile_kwargs={"literal_binds": False},
                )
            )
            compiled_sql.append(compiled)
            return []

    async def scenario() -> None:
        repo = KnowledgeRepository(cast(Any, lambda: _Session()))
        await repo.search_semantic("owner", ["base"], [0.1] * 1024, 10, "prof", min_score=0.0)
        await repo.search_semantic("owner", ["base"], [0.1] * 1024, 10, "prof", min_score=0.35)

    asyncio.run(scenario())
    assert "<=>" in compiled_sql[0]
    assert "<=" not in compiled_sql[0].split("ORDER BY")[0]
    assert "<=" in compiled_sql[1].split("ORDER BY")[0]


def test_mixed_profile_min_score_empty_hit_keeps_semantic_channel_when_compatible_exists() -> None:
    """混合 profile 下因 min_score 合法过滤为空但存在兼容切片时，保留 semantic 通道并记降级。"""
    compatible_hit = _hit("chunk_compat", 0, "低相关性兼容切片", "semantic", 1)

    class _MixedProfileRepo(_Repository):
        def __init__(self, *, has_compatible_chunk: bool, keyword_fails: bool) -> None:
            super().__init__()
            self.incompatible_profile = True
            self.has_compatible_chunk = has_compatible_chunk
            self.keyword_fails = keyword_fails
            self.semantic_calls_log: list[tuple[int, float]] = []

        async def search_keyword(
            self,
            owner_id: str,
            base_ids: list[str],
            tokens: tuple[str, ...],
            limit: int,
        ) -> tuple[SearchHit, ...]:
            del owner_id, base_ids, tokens, limit
            if self.keyword_fails:
                raise RuntimeError("keyword_down")
            return (_hit("chunk_kw", 0, "关键词命中", "keyword", 1),)

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
            del owner_id, base_ids, vector, profile_id
            self.semantic_calls_log.append((limit, min_score))
            if min_score == 0.0 and self.has_compatible_chunk:
                return (compatible_hit,)
            return ()

    # 存在兼容切片但分数低于 0.35：即使 keyword 不可用，semantic 通道仍存活并只记降级。
    repo_with_compat = _MixedProfileRepo(has_compatible_chunk=True, keyword_fails=True)
    service_with_compat = KnowledgeService(
        cast(KnowledgeRepositoryPort, repo_with_compat),
        _Tokenizer(),
        _Embedding(),
        counter=_Counter(),
        semantic_min_score=0.35,
    )
    result = asyncio.run(service_with_compat.search("owner", ["base"], "无关查询"))
    assert result.hits == ()
    assert result.channels == ("semantic",)
    assert "semantic_profile_incompatible" in result.degraded_reasons
    assert repo_with_compat.semantic_calls_log == [(40, 0.35), (1, 0.0)]

    # 确实没有任何兼容切片：semantic 通道按不兼容降级排除，仅保留 keyword 通道。
    repo_no_compat = _MixedProfileRepo(has_compatible_chunk=False, keyword_fails=False)
    service_no_compat = KnowledgeService(
        cast(KnowledgeRepositoryPort, repo_no_compat),
        _Tokenizer(),
        _Embedding(),
        counter=_Counter(),
        semantic_min_score=0.35,
    )
    degraded_result = asyncio.run(service_no_compat.search("owner", ["base"], "查询"))
    assert degraded_result.channels == ("keyword",)
    assert "semantic_profile_incompatible" in degraded_result.degraded_reasons
    assert repo_no_compat.semantic_calls_log == [(40, 0.35), (1, 0.0)]


def test_jieba_tokenizer_warmup_is_idempotent_and_handles_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JiebaChineseTokenizer.warmup 须幂等初始化词典，且缺依赖时安全兼容不崩溃。"""
    import builtins
    import sys
    import threading
    from typing import Any

    from bootstrap.tool import QueryServiceBundle
    from infrastructure.knowledge.tokenizer import JiebaChineseTokenizer

    init_threads: list[int] = []

    class _FakeJieba:
        def initialize(self) -> None:
            init_threads.append(threading.get_ident())

        def cut_for_search(self, text: str) -> list[str]:
            return text.split()

    monkeypatch.setitem(sys.modules, "jieba", cast(Any, _FakeJieba()))
    tokenizer = JiebaChineseTokenizer()
    tokenizer.warmup()
    tokenizer.warmup()
    assert len(init_threads) == 1
    assert tokenizer.tokenize("企业知识库 v2.1") == ("企业知识库", "v2.1")

    # 通过 QueryServiceBundle.start() 异步预热时，应在工作线程而非事件循环主线程运行。
    worker_threads: list[int] = []
    main_thread_id = threading.get_ident()

    class _RecordingTokenizer:
        def warmup(self) -> None:
            worker_threads.append(threading.get_ident())

    async def start_bundle() -> None:
        bundle = QueryServiceBundle(
            knowledge=None,
            project_knowledge=None,
            knowledge_repository=None,
            knowledge_original_store=None,
            knowledge_enabled=True,
            _startables=(_RecordingTokenizer(),),
            _closables=(),
        )
        await bundle.start()

    asyncio.run(start_bundle())
    assert len(worker_threads) == 1
    assert worker_threads[0] != main_thread_id

    # 模拟 jieba 未安装：warmup 安全返回，tokenize 仍按契约抛缺失依赖错误。
    monkeypatch.delitem(sys.modules, "jieba", raising=False)
    real_import = builtins.__import__

    def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "jieba" or name.startswith("jieba."):
            raise ImportError("jieba unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    missing_tokenizer = JiebaChineseTokenizer()
    missing_tokenizer.warmup()
    with pytest.raises(RuntimeError, match="knowledge_tokenizer_dependency_missing"):
        missing_tokenizer.tokenize("测试查询")


def test_knowledge_service_search_runs_tokenizer_off_event_loop() -> None:
    """KnowledgeService.search 须在工作线程执行中文分词，不阻塞事件循环。"""
    import threading
    import time

    tokenize_threads: list[int] = []
    event_loop_thread_id = threading.get_ident()

    class _SlowThreadTrackingTokenizer:
        def tokenize(self, text: str) -> tuple[str, ...]:
            tokenize_threads.append(threading.get_ident())
            time.sleep(0.05)
            return tuple(text.split())

    async def scenario() -> None:
        repository = _Repository()
        repository.keyword_hits = (_hit("chunk-1", 0, "async tokenized", "keyword", 1),)
        service = KnowledgeService(
            cast(KnowledgeRepositoryPort, repository),
            _SlowThreadTrackingTokenizer(),
            _Embedding(),
            counter=_Counter(),
        )
        ticks = 0
        stop_ticker = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not stop_ticker.is_set():
                ticks += 1
                await asyncio.sleep(0.005)

        tick_task = asyncio.create_task(ticker())
        try:
            result = await service.search("owner", ["base"], "query")
        finally:
            stop_ticker.set()
            await tick_task

        assert result.hits[0].chunk_id == "chunk-1"
        assert len(tokenize_threads) == 1
        assert tokenize_threads[0] != event_loop_thread_id
        # 确认分词在工作线程等待期间事件循环保持响应。
        assert ticks >= 2

    asyncio.run(scenario())
