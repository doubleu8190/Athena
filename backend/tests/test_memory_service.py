"""记忆服务混合召回与写入验证。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from app.memory.service import MemoryService
from domain.memory.model import (
    AccessContext,
    Memory,
    MemorySource,
    RecallRequest,
    RecallSnapshot,
)

FloatList = list[float]


class FakeRepository:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.items: list[Memory] = [
            Memory(f"m{i}", "u", "p", content, "active", "explicit", 1, None, (), now, now)
            for i, content in enumerate(("项目数据库使用 PostgreSQL 17", "用户喜欢中文回答"))
        ]
        self.edit_calls = 0

    async def settings(self, _namespace: str) -> tuple[bool, bool]:
        return True, True

    async def search_keyword(
        self, _namespace: str, _scopes: tuple[str, ...], query: str, _limit: int
    ) -> list[tuple[Memory, float]]:
        return [(self.items[0], 0.7)] if "数据库" in query else []

    async def search_vector(
        self,
        _namespace: str,
        _scopes: tuple[str, ...],
        _vector: list[float],
        _profile: str,
        _limit: int,
    ) -> list[tuple[Memory, float]]:
        return [(self.items[1], 0.8)]

    async def get_many(
        self, ids: tuple[str, ...], _namespace: str, _scopes: tuple[str, ...]
    ) -> RecallSnapshot:
        return RecallSnapshot(True, {item.id: item for item in self.items if item.id in ids})

    async def update_settings(
        self, _namespace: str, _read_enabled: bool, _auto_extract_enabled: bool
    ) -> None:
        return None


class FakeEmbedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * 1024 for _ in texts]


class FakeReranker:
    async def score(self, _query: str, contents: list[str]) -> list[float]:
        return [0.9 if "PostgreSQL" in content else 0.8 for content in contents]


class FakeCounter:
    def count(self, text: str) -> int:
        return len(text)


def test_recall_fuses_keyword_and_vector_candidates() -> None:
    service = MemoryService(FakeRepository(), FakeEmbedding(), FakeReranker(), FakeCounter())
    result = asyncio.run(
        service.recall(
            RecallRequest(AccessContext("u", ("p",), ("p",)), ("p",), "项目数据库是什么")
        )
    )
    assert result.status == "ok"
    assert {hit.memory.id for hit in result.hits} == {"m0", "m1"}
    assert result.hits[0].memory.id == "m0"


def test_recall_rejects_scope_outside_access_context() -> None:
    service = MemoryService(FakeRepository(), FakeEmbedding(), FakeReranker(), FakeCounter())
    request = RecallRequest(AccessContext("u", ("p",), ()), ("other",), "问题")
    with pytest.raises(ValueError, match="memory_recall_invalid"):
        asyncio.run(service.recall(request))


def test_recall_marks_budget_truncation() -> None:
    service = MemoryService(FakeRepository(), FakeEmbedding(), FakeReranker(), FakeCounter())
    request = RecallRequest(
        AccessContext("u", ("p",), ("p",)), ("p",), "项目数据库是什么", token_budget=10
    )
    result = asyncio.run(service.recall(request))
    assert result.hits == ()
    assert result.truncated


def test_settings_default_auto_extract_enabled() -> None:
    class DefaultSettingsRepository(FakeRepository):
        async def settings(self, _namespace: str) -> tuple[bool, bool]:
            return True, True

    repository = DefaultSettingsRepository()
    assert asyncio.run(repository.settings("u")) == (True, True)


def test_reranker_failure_does_not_accept_unvalidated_keyword_candidate() -> None:
    class BrokenEmbedding:
        async def embed(self, _texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embedding unavailable")

    class BrokenReranker:
        async def score(self, _query: str, _contents: list[str]) -> list[float]:
            raise RuntimeError("model unavailable")

    service = MemoryService(FakeRepository(), BrokenEmbedding(), BrokenReranker(), FakeCounter())
    result = asyncio.run(
        service.recall(RecallRequest(AccessContext("u", ("p",), ()), ("p",), "项目数据库是什么"))
    )
    assert result.status == "degraded"
    assert result.hits == ()
    assert "reranker_failed" in result.degradation_reasons


def test_recall_stops_when_read_setting_is_disabled() -> None:
    class DisabledRepository(FakeRepository):
        async def settings(self, _namespace: str) -> tuple[bool, bool]:
            return False, True

    class BrokenEmbedding:
        async def embed(self, _texts: list[str]) -> list[list[float]]:
            raise AssertionError("disabled recall must stop before embedding")

    service = MemoryService(DisabledRepository(), BrokenEmbedding(), FakeReranker(), FakeCounter())
    result = asyncio.run(
        service.recall(RecallRequest(AccessContext("u", ("p",), ()), ("p",), "问题"))
    )
    assert result.status == "disabled"


def test_recall_maps_final_read_check_failure_to_unavailable() -> None:
    class BrokenFinalCheckRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__()
            self._settings_calls = 0

        async def settings(self, _namespace: str) -> tuple[bool, bool]:
            self._settings_calls += 1
            if self._settings_calls >= 2:
                raise RuntimeError("database unavailable")
            return True, True

    service = MemoryService(
        BrokenFinalCheckRepository(), FakeEmbedding(), FakeReranker(), FakeCounter()
    )
    result = asyncio.run(
        service.recall(RecallRequest(AccessContext("u", ("p",), ()), ("p",), "项目数据库是什么"))
    )
    assert result.status == "unavailable"
    assert result.degradation_reasons == ("final_check_failed",)


def test_vector_candidates_below_minimum_are_dropped_before_fusion() -> None:
    class LowVectorRepository(FakeRepository):
        async def search_keyword(
            self, _namespace: str, _scopes: tuple[str, ...], _query: str, _limit: int
        ) -> list[tuple[Memory, float]]:
            return []

        async def search_vector(
            self,
            _namespace: str,
            _scopes: tuple[str, ...],
            _vector: list[float],
            _profile: str,
            _limit: int,
        ) -> list[tuple[Memory, float]]:
            return [(self.items[0], 0.69)]

    service = MemoryService(LowVectorRepository(), FakeEmbedding(), FakeReranker(), FakeCounter())
    result = asyncio.run(
        service.recall(RecallRequest(AccessContext("u", ("p",), ()), ("p",), "项目数据库是什么"))
    )
    assert result.status == "ok"
    assert result.hits == ()


def test_recall_deduplicates_by_scope_merges_checked_sources_and_uses_latest() -> None:
    now = datetime.now(UTC)
    older = Memory(
        "m-old",
        "u",
        "p",
        "数据库 PostgreSQL 17",
        "active",
        "explicit",
        1,
        None,
        (MemorySource("message", "old-source"),),
        now,
        now,
    )
    newer = Memory(
        "m-new",
        "u",
        "p",
        " 数据库   ＰｏｓｔｇｒｅＳＱＬ 17 ",
        "active",
        "explicit",
        1,
        None,
        (MemorySource("message", "new-source"),),
        now,
        now + timedelta(microseconds=1),
    )
    other_scope = Memory(
        "m-other-scope",
        "u",
        "other-project",
        "数据库 PostgreSQL 17",
        "active",
        "explicit",
        1,
        None,
        (MemorySource("message", "other-scope-source"),),
        now,
        now,
    )

    class DuplicateRepository(FakeRepository):
        def __init__(self) -> None:
            self.items = [older, newer, other_scope]

        async def search_keyword(
            self, _namespace: str, _scopes: tuple[str, ...], _query: str, _limit: int
        ) -> list[tuple[Memory, float]]:
            return [(item, 0.7) for item in self.items]

        async def search_vector(
            self,
            _namespace: str,
            _scopes: tuple[str, ...],
            _vector: list[float],
            _profile: str,
            _limit: int,
        ) -> list[tuple[Memory, float]]:
            return [(item, 0.8) for item in self.items]

    service = MemoryService(DuplicateRepository(), FakeEmbedding(), FakeReranker(), FakeCounter())
    result = asyncio.run(
        service.recall(
            RecallRequest(
                AccessContext("u", ("p", "other-project"), ()),
                ("p", "other-project"),
                "数据库 PostgreSQL 17",
                limit=2,
            )
        )
    )
    assert len(result.hits) == 2
    personal = next(hit.memory for hit in result.hits if hit.memory.scope_key == "p")
    assert personal.id == "m-new"
    assert {source.reference for source in personal.sources} == {"old-source", "new-source"}


def test_bounded_inference_cancellation_keeps_real_thread_slot_until_done() -> None:
    from infrastructure.memory.models import _BoundedInference

    async def scenario() -> None:
        inference: _BoundedInference[str] = _BoundedInference(capacity=1, max_waiting=0)
        started = Event()
        finish = Event()

        def operation() -> str:
            started.set()
            finish.wait()
            raise RuntimeError("late inference failure")

        first = asyncio.create_task(inference.run(operation))
        assert await asyncio.to_thread(started.wait, 5)
        queued = asyncio.create_task(inference.run(lambda: "must not start"))
        await asyncio.sleep(0)
        queued.cancel()
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(RuntimeError, match="memory_inference_overloaded"):
            await inference.run(lambda: "should not start")
        closing = asyncio.create_task(inference.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        finish.set()
        await closing
        with pytest.raises(RuntimeError, match="memory_inference_closed"):
            await inference.run(lambda: "after close")

    asyncio.run(scenario())


def test_memory_repository_search_keyword_builds_safe_tsquery_for_special_chars() -> None:
    """Memory 关键词检索使用 to_tsvector + plainto_tsquery，不把特殊符号喂给 to_tsquery。"""
    import re
    from types import TracebackType
    from typing import Any, Self, cast

    from sqlalchemy.dialects import postgresql

    from adapter.memory.repository import MemoryRepository

    compiled_statements: list[str] = []

    class _Result:
        def unique(self) -> Self:
            return self

        def all(self) -> list[object]:
            return []

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

        async def get(self, model: object, key: object) -> None:
            del model, key
            return None

        async def execute(self, statement: Any) -> _Result:
            dialect = cast(Any, postgresql).dialect()
            compiled = str(
                statement.compile(
                    dialect=dialect,
                    compile_kwargs={"literal_binds": False},
                )
            )
            compiled_statements.append(compiled)
            return _Result()

    async def scenario() -> None:
        repo = MemoryRepository(cast(Any, lambda: _Session()))
        # 空白或空词元直接返回 []，不触发查询
        assert await repo.search_keyword("u", ("personal",), "   \t\n  ", 5) == []
        assert compiled_statements == []

        for special_query in ("C++", "foo:bar", "(测试)", r"C:\Users\test", "+++ () !"):
            rows = await repo.search_keyword("u", ("personal",), special_query, 5)
            assert rows == []
            sql = compiled_statements[-1]
            assert re.search(r"\bto_tsquery\(", sql) is None
            assert "to_tsvector(" in sql
            assert "plainto_tsquery(" in sql
            assert "nullif(" in sql.lower()
            assert "string_agg(" in sql

    asyncio.run(scenario())


def test_recall_cancels_both_channel_tasks_when_caller_cancels() -> None:
    """外部取消 recall() 时必须显式取消并回收 keyword_task 与 vector_task，防止后台任务泄漏。"""
    keyword_started = asyncio.Event()
    vector_started = asyncio.Event()
    keyword_cancelled = False
    vector_cancelled = False

    class _HangingRepository(FakeRepository):
        async def search_keyword(
            self, _namespace: str, _scopes: tuple[str, ...], _query: str, _limit: int
        ) -> list[tuple[Memory, float]]:
            nonlocal keyword_cancelled
            keyword_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                keyword_cancelled = True
                raise
            return []

    class _HangingEmbedding:
        async def embed(self, _texts: list[str]) -> list[list[float]]:
            nonlocal vector_cancelled
            vector_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                vector_cancelled = True
                raise
            return []

    async def scenario() -> None:
        service = MemoryService(
            _HangingRepository(), _HangingEmbedding(), FakeReranker(), FakeCounter()
        )
        task = asyncio.create_task(
            service.recall(RecallRequest(AccessContext("u", ("p",), ()), ("p",), "取消测试"))
        )
        await asyncio.wait_for(
            asyncio.gather(keyword_started.wait(), vector_started.wait()),
            timeout=2.0,
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert keyword_cancelled is True
        assert vector_cancelled is True

    asyncio.run(scenario())


def test_local_reranker_lazy_loads_off_event_loop_thread() -> None:
    """LocalReranker.score 首次懒加载必须在工作线程执行，不得同步阻塞事件循环。"""
    import threading
    from typing import Any

    from infrastructure.memory.models import LocalReranker

    main_thread = threading.current_thread()
    load_threads: list[threading.Thread] = []

    class _FakeCrossEncoder:
        def predict(self, pairs: list[list[str]]) -> list[float]:
            return [0.75 for _ in pairs]

    async def scenario() -> None:
        reranker = LocalReranker("fake-model", "rev-1", "cpu")

        def fake_load() -> None:
            load_threads.append(threading.current_thread())
            reranker._model = _FakeCrossEncoder()  # type: ignore[assignment]

        reranker._load = fake_load  # type: ignore[method-assign]
        try:
            scores: list[Any] = await reranker.score("q", ["a", "b"])
            assert scores == [0.75, 0.75]
            assert len(load_threads) == 1
            assert load_threads[0] is not main_thread
        finally:
            await reranker.aclose()

    asyncio.run(scenario())
