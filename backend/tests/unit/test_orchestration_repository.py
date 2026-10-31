from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from domain.orchestration import (
    PlanStatus,
    TaskStatus,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresOrchestrationRepository,
)


class Result:
    def __init__(self, *, scalar=None, rows=(), rowcount=1):
        self._scalar = scalar
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class Session:
    def __init__(self):
        self.get_values = []
        self.execute_values = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args, **_kwargs):
        return self.get_values.pop(0) if self.get_values else None

    async def execute(self, _statement):
        return self.execute_values.pop(0) if self.execute_values else Result()


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


def task(task_id, *, status=TaskStatus.PENDING, generation=0, output=None, error=None):
    return SimpleNamespace(
        task_id=task_id,
        plan_id="p-1",
        run_id="r-1",
        title=task_id,
        objective=f"do {task_id}",
        input_json={},
        expected_output_json={},
        allowed_tools_json=[],
        worker_type="general",
        status=status.value,
        execution_generation=generation,
        timeout_seconds=3600.0,
        worker_thread_id=None,
        active_execution_id=None,
        output_json=output,
        error_json=error,
    )


@pytest.mark.asyncio
async def test_orchestration_repository_reads_plan_status():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    plan_row = SimpleNamespace(
        plan_id="p-1", status="created"
    )
    session.get_values = [plan_row]
    assert await repository.get_plan_status("p-1") is PlanStatus.CREATED


@pytest.mark.asyncio
async def test_legacy_round_review_context_is_disabled_during_worker_s2():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    with pytest.raises(
        RuntimeError, match="legacy_round_review_disabled_during_worker_s2"
    ):
        await repository.load_review_context("round-2")
    assert session.get_values == []
    assert session.execute_values == []


@pytest.mark.asyncio
async def test_legacy_review_does_not_read_stale_round_during_worker_s2():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    session.get_values = [
        SimpleNamespace(
            round_id="round-1",
            plan_id="p-1",
            status="completed",
            plan_version=1,
            terminal_task_count=1,
            expected_task_count=1,
        ),
        SimpleNamespace(
            status=PlanStatus.WAITING_REVIEW.value,
            active_round_id="round-2",
            current_version=2,
        ),
    ]

    with pytest.raises(
        RuntimeError, match="legacy_round_review_disabled_during_worker_s2"
    ):
        await repository.load_review_context("round-1")

    assert session.execute_values == []


@pytest.mark.asyncio
async def test_target_orchestration_repository_loads_task_results():
    session = Session()
    repository = PostgresOrchestrationRepository(factory(session))
    failed, pending = task("a", status=TaskStatus.FAILED), task("b")
    session.execute_values = [Result(rows=[failed, pending])]
    results = await repository.get_results("p-1")
    assert results["a"].status is TaskStatus.FAILED
