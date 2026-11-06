"""记忆服务混合召回与写入验证。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from app.memory.service import MemoryService
from domain.memory.model import (
    AccessContext,
    AddMemory,
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
        self.seen_request_ids: list[str] = []
        self.edit_calls = 0

    async def settings(self, _namespace: str) -> tuple[bool, bool]:
        return True, True

    async def get_by_request(self, _request: AddMemory) -> Memory | None:
        self.seen_request_ids.append(_request.request_id)
        return None

    async def add(self, request: AddMemory, _embedding: list[float], _profile: str) -> Memory:
        return self.items[0]

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

    async def list(
        self, _access: AccessContext, _scope: str, _limit: int, _offset: int
    ) -> tuple[Memory, ...]:
        return tuple(self.items)

    async def delete(self, _access: AccessContext, _memory_id: str, _version: int) -> bool:
        return True

    async def edit(
        self,
        _access: AccessContext,
        _memory_id: str,
        _content: str,
        _embedding: FloatList,
        _profile: str,
        _expected_version: int,
    ) -> bool:
        self.edit_calls += 1
        return True

    async def confirm(
        self, _access: AccessContext, _memory_id: str, _expected_version: int
    ) -> bool:
        return True

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


def test_add_rejects_wrong_embedding_dimension() -> None:
    class BadEmbedding:
        async def embed(self, _texts: list[str]) -> list[list[float]]:
            return [[1.0]]

    service = MemoryService(FakeRepository(), BadEmbedding(), FakeReranker(), FakeCounter())
    with pytest.raises(ValueError, match="memory_embedding_invalid"):
        asyncio.run(
            service.add(AddMemory(AccessContext("u", ("p",), ("p",)), "p", "内容", "request-1"))
        )


@pytest.mark.parametrize("request_id", ["", "   ", "\t\n"])
def test_add_rejects_empty_request_id(request_id: str) -> None:
    service = MemoryService(FakeRepository(), FakeEmbedding(), FakeReranker(), FakeCounter())
    with pytest.raises(ValueError, match="memory_request_invalid"):
        asyncio.run(
            service.add(AddMemory(AccessContext("u", ("p",), ("p",)), "p", "内容", request_id))
        )


def test_add_strips_request_id_before_idempotency_lookup_and_write() -> None:
    repository = FakeRepository()
    service = MemoryService(repository, FakeEmbedding(), FakeReranker(), FakeCounter())
    asyncio.run(
        service.add(AddMemory(AccessContext("u", ("p",), ("p",)), "p", "内容", "  request-1  "))
    )
    assert repository.seen_request_ids == ["request-1"]


def test_edit_rejects_zero_vector_without_changing_repository() -> None:
    class ZeroEmbedding:
        async def embed(self, _texts: list[str]) -> list[list[float]]:
            return [[0.0] * 1024]

    repository = FakeRepository()
    original_content = repository.items[0].content
    service = MemoryService(repository, ZeroEmbedding(), FakeReranker(), FakeCounter())
    with pytest.raises(ValueError, match="memory_embedding_invalid"):
        asyncio.run(
            service.edit(
                AccessContext("u", (), ("p",)),
                repository.items[0].id,
                "替换正文",
                expected_version=1,
            )
        )
    assert repository.edit_calls == 0
    assert repository.items[0].content == original_content


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


def test_add_idempotent_replay_skips_embedding() -> None:
    now = datetime.now(UTC)
    existing = Memory("m1", "u", "p", "内容", "active", "explicit", 1, None, (), now, now)

    class ExistingRepository(FakeRepository):
        async def get_by_request(self, _request: AddMemory) -> Memory:
            return existing

    class BrokenEmbedding:
        async def embed(self, _texts: list[str]) -> list[list[float]]:
            raise AssertionError("idempotent replay must not embed")

    service = MemoryService(ExistingRepository(), BrokenEmbedding(), FakeReranker(), FakeCounter())
    result = asyncio.run(
        service.add(AddMemory(AccessContext("u", ("p",), ("p",)), "p", "内容", "r1"))
    )
    assert result is existing


def test_list_requires_access_read_scope() -> None:
    service = MemoryService(FakeRepository(), FakeEmbedding(), FakeReranker(), FakeCounter())
    access = AccessContext("u", (), ("p",))
    with pytest.raises(ValueError, match="memory_scope_forbidden"):
        asyncio.run(service.list(access, "p"))


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
        async def get_many(
            self, _ids: tuple[str, ...], _namespace: str, _scopes: tuple[str, ...]
        ) -> RecallSnapshot:
            raise RuntimeError("database unavailable")

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


def test_recall_does_not_merge_sources_from_stale_duplicate_version() -> None:
    now = datetime.now(UTC)
    candidate = Memory(
        "m-stale",
        "u",
        "p",
        "PostgreSQL 17",
        "active",
        "explicit",
        1,
        None,
        (MemorySource("message", "stale-source"),),
        now,
        now,
    )
    current = Memory(
        "m-stale",
        "u",
        "p",
        "PostgreSQL 17",
        "active",
        "explicit",
        2,
        None,
        (MemorySource("message", "current-source"),),
        now,
        now + timedelta(seconds=1),
    )
    valid_duplicate = Memory(
        "m-valid",
        "u",
        "p",
        "PostgreSQL 17",
        "active",
        "explicit",
        1,
        None,
        (MemorySource("message", "valid-source"),),
        now,
        now,
    )

    class StaleRepository(FakeRepository):
        async def search_keyword(
            self, _namespace: str, _scopes: tuple[str, ...], _query: str, _limit: int
        ) -> list[tuple[Memory, float]]:
            return [(candidate, 0.8), (valid_duplicate, 0.7)]

        async def search_vector(
            self,
            _namespace: str,
            _scopes: tuple[str, ...],
            _vector: list[float],
            _profile: str,
            _limit: int,
        ) -> list[tuple[Memory, float]]:
            return []

        async def get_many(
            self, ids: tuple[str, ...], _namespace: str, _scopes: tuple[str, ...]
        ) -> RecallSnapshot:
            rows = {item.id: item for item in (current, valid_duplicate) if item.id in ids}
            return RecallSnapshot(True, rows)

    service = MemoryService(StaleRepository(), FakeEmbedding(), FakeReranker(), FakeCounter())
    result = asyncio.run(
        service.recall(RecallRequest(AccessContext("u", ("p",), ()), ("p",), "PostgreSQL 17"))
    )
    assert len(result.hits) == 1
    assert result.hits[0].memory.id == "m-valid"
    assert {source.reference for source in result.hits[0].memory.sources} == {"valid-source"}


def test_extracted_add_checks_auto_extract_setting_before_embedding() -> None:
    class DisabledRepository(FakeRepository):
        async def settings(self, _namespace: str) -> tuple[bool, bool]:
            return True, False

    class BrokenEmbedding:
        async def embed(self, _texts: list[str]) -> list[list[float]]:
            raise AssertionError("disabled extraction must stop before embedding")

    service = MemoryService(DisabledRepository(), BrokenEmbedding(), FakeReranker(), FakeCounter())
    with pytest.raises(ValueError, match="memory_auto_extract_disabled"):
        asyncio.run(
            service.add(
                AddMemory(
                    AccessContext("u", (), ("p",)),
                    "p",
                    "候选记忆",
                    "candidate-1",
                    origin="extracted",
                    status="pending",
                )
            )
        )


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
