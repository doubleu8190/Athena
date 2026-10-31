from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from infrastructure.persistence.postgres.repositories.orchestration_event_repository import (
    PostgresOrchestrationEventRepository,
)


class Session:
    def __init__(self) -> None:
        self.opened = False

    async def __aenter__(self):
        self.opened = True
        return self

    async def __aexit__(self, *_args):
        return False

def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


@pytest.mark.asyncio
async def test_legacy_round_terminal_events_are_disabled_during_worker_s2():
    session = Session()
    repository = PostgresOrchestrationEventRepository(factory(session))

    with pytest.raises(
        RuntimeError,
        match="legacy_worker_terminal_event_disabled_during_worker_s2",
    ):
        await repository.apply_worker_terminal_event(None)

    assert session.opened is False
