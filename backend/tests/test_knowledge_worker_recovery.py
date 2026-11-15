"""知识库消费者在数据库短暂故障后的恢复。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import anyio
import asyncpg  # type: ignore[import-untyped]
import pytest
from knowledge_worker import _consume, _is_recoverable_worker_error, _run_consumers
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, ProgrammingError

from app.knowledge.worker import KnowledgeWorker
from domain.knowledge.ports import ClaimedKnowledgeJob, KnowledgeJobRepository, ParsedDocument


class _FailingThenStoppingWorker:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self) -> bool:
        self.calls += 1
        if self.calls == 1:
            raise OperationalError("claim", {}, OSError("database offline"))
        if self.calls == 2:
            raise OSError("connection reset")
        if self.calls == 3:
            return True
        raise asyncio.CancelledError


def test_consumer_retries_transient_claim_and_persistence_failures() -> None:
    async def scenario() -> None:
        worker = _FailingThenStoppingWorker()
        delays: list[float] = []

        async def sleep(delay: float) -> None:
            delays.append(delay)

        with pytest.raises(asyncio.CancelledError):
            await _consume(cast(KnowledgeWorker, worker), sleep=sleep)
        assert worker.calls == 4
        assert len(delays) == 2

    anyio.run(scenario)


def test_same_worker_recovers_from_claim_and_fail_job_database_errors() -> None:
    async def scenario() -> None:
        class Repository:
            claims = 0
            failures = 0

            async def claim_job(
                self, *, lease_seconds: int, owner_concurrency: int = 2
            ) -> ClaimedKnowledgeJob | None:
                self.claims += 1
                if self.claims == 1:
                    raise OperationalError("claim", {}, OSError("connection lost"))
                if self.claims == 2:
                    return ClaimedKnowledgeJob(
                        "job_1",
                        "owner_1",
                        "version_1",
                        "doc_1",
                        "title",
                        None,
                        None,
                        None,
                        1,
                    )
                raise asyncio.CancelledError

            async def fail_job(
                self, job: ClaimedKnowledgeJob, *, error_code: str, retryable: bool
            ) -> None:
                self.failures += 1
                raise OSError("database unavailable while recording failure")

        class EmptyParser:
            def create(self) -> EmptyParser:
                return self

            async def parse(self, filename: str, content: bytes) -> ParsedDocument:
                raise AssertionError("source data is intentionally missing")

        class EmptyStore:
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

        class Embedding:
            async def embed(self, texts: list[str]) -> list[list[float]]:
                return []

        class Tokenizer:
            def tokenize(self, text: str) -> tuple[str, ...]:
                return (text,)

        class Counter:
            def count(self, text: str) -> int:
                return len(text)

        repository = Repository()
        worker = KnowledgeWorker(
            cast(KnowledgeJobRepository, repository),
            EmptyParser(),
            EmptyStore(),
            Embedding(),
            Tokenizer(),
            Counter(),
            isolate_parsing=False,
        )
        delays: list[float] = []

        async def sleep(delay: float) -> None:
            delays.append(delay)

        with pytest.raises(asyncio.CancelledError):
            await _consume(worker, sleep=sleep)
        assert repository.claims == 3
        assert repository.failures == 1
        assert len(delays) == 2

    anyio.run(scenario)


@pytest.mark.parametrize("sqlstate", ["08006", "57P01", "57P03", "53300"])
def test_asyncpg_connection_sqlstates_are_retryable(sqlstate: str) -> None:
    class DriverFailure(Exception):
        def __init__(self, state: str) -> None:
            super().__init__("database connection unavailable")
            self.sqlstate = state

    error = DBAPIError("query", {}, DriverFailure(sqlstate))
    assert _is_recoverable_worker_error(error)


def test_asyncpg_connection_exception_survives_sqlalchemy_dialect_translation() -> None:
    driver_error = asyncpg.exceptions.ConnectionDoesNotExistError("connection closed")
    dbapi = AsyncAdapt_asyncpg_dbapi(asyncpg)  # type: ignore[no-untyped-call]
    error_type = next(
        translated
        for driver_type, translated in dbapi._asyncpg_error_translate.items()
        if isinstance(driver_error, driver_type)
    )
    translated_error = error_type(str(driver_error), driver_error)
    sqlalchemy_error = DBAPIError("query", {}, translated_error)
    assert _is_recoverable_worker_error(sqlalchemy_error)


def test_programming_and_integrity_database_errors_are_not_retryable() -> None:
    assert not _is_recoverable_worker_error(
        ProgrammingError("query", {}, RuntimeError("invalid sql"))
    )
    assert not _is_recoverable_worker_error(
        IntegrityError("query", {}, RuntimeError("constraint violation"))
    )
    assert _is_recoverable_worker_error(
        DBAPIError("query", {}, RuntimeError("connection closed"), connection_invalidated=True)
    )


def test_consumer_propagates_cancellation_while_run_once_is_blocked() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class BlockedWorker:
            async def run_once(self) -> bool:
                started.set()
                await asyncio.Event().wait()
                return False

        task = asyncio.create_task(_consume(cast(KnowledgeWorker, BlockedWorker())))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(scenario)


def test_fatal_consumer_error_cancels_and_awaits_sibling() -> None:
    async def scenario() -> None:
        sibling_started = asyncio.Event()
        sibling_cancelled = asyncio.Event()

        class FatalWorker:
            async def run_once(self) -> bool:
                current = asyncio.current_task()
                assert current is not None
                if current.get_name().endswith("-1"):
                    await sibling_started.wait()
                    raise ValueError("fatal programming error")
                sibling_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    sibling_cancelled.set()
                    raise
                return False

        with pytest.raises(ExceptionGroup):
            await _run_consumers(cast(KnowledgeWorker, FatalWorker()), 2)
        assert sibling_cancelled.is_set()

    anyio.run(scenario)
