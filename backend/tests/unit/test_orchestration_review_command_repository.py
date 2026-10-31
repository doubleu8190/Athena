"""S2 明确停用旧 Round/DAG 命令入口。"""

from contextlib import asynccontextmanager

import pytest

from infrastructure.persistence.postgres.repositories.orchestration_command_repository import (
    PostgresOrchestrationCommandRepository,
)


class Session:
    def __init__(self) -> None:
        self.opened = False

    async def __aenter__(self):
        self.opened = True
        return self

    async def __aexit__(self, *_args):
        return False


def factory(session: Session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


@pytest.mark.asyncio
async def test_legacy_dag_plan_submission_fails_closed_during_worker_s2() -> None:
    session = Session()
    repository = PostgresOrchestrationCommandRepository(factory(session))

    with pytest.raises(
        RuntimeError,
        match="legacy_dag_plan_submission_disabled_during_worker_s2",
    ):
        await repository.submit_plan(None, None, [])

    assert session.opened is False


@pytest.mark.asyncio
async def test_legacy_round_review_decision_fails_closed_during_worker_s2() -> None:
    session = Session()
    repository = PostgresOrchestrationCommandRepository(factory(session))

    with pytest.raises(
        RuntimeError,
        match="legacy_round_review_disabled_during_worker_s2",
    ):
        await repository.apply_review_decision(None, None)

    assert session.opened is False
