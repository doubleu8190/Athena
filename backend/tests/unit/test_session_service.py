from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.application.sessions import SessionNotFoundError, SessionService
from backend.src.domain.sessions import Session


class FakeSessionRepository:
    def __init__(self) -> None:
        self.items: dict[str, Session] = {}

    async def create(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def get(self, session_id: str) -> Session | None:
        return self.items.get(session_id)

    async def list_all(self) -> list[Session]:
        return list(self.items.values())

    async def save(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def delete(self, session_id: str) -> bool:
        return self.items.pop(session_id, None) is not None


@pytest.mark.asyncio
async def test_session_service_owns_create_and_rename_rules() -> None:
    repository = FakeSessionRepository()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    service = SessionService(
        repository,
        id_factory=lambda: "session-1",
        clock=lambda: now,
    )

    created = await service.create("  Project  ")
    renamed = await service.rename(created.id, " New title ")

    assert renamed.title == "New title"
    assert renamed.id == "session-1"


@pytest.mark.asyncio
async def test_session_service_maps_missing_session_to_domain_error() -> None:
    service = SessionService(
        FakeSessionRepository(),
        id_factory=lambda: "session-1",
    )

    with pytest.raises(SessionNotFoundError):
        await service.get("missing")
