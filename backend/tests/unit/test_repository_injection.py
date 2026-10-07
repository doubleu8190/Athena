"""新 PostgreSQL repository 的依赖注入测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from domain.sessions import Session, SessionStatus
from infrastructure.persistence.postgres.repositories import (
    PostgresSessionRepository,
)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, value) -> None:
        self.added.append(value)

    def begin(self) -> _Transaction:
        return _Transaction()


@pytest.mark.asyncio
async def test_session_repository_uses_injected_session_factory() -> None:
    """repository 不得从旧 Database 或全局 engine 取连接。"""
    fake = _FakeSession()

    @asynccontextmanager
    async def session_factory():
        yield fake

    repository = PostgresSessionRepository(session_factory)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session = Session(
        id="session-1",
        title="Title",
        status=SessionStatus.IDLE,
        run_id=None,
        created_at=now,
        updated_at=now,
    )

    result = await repository.create(session)

    assert result == session
    assert fake.added[0].id == "session-1"
