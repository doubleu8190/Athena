from __future__ import annotations

from datetime import datetime, timezone

import pytest

from application.memory import SessionMemorySettingsService
from domain.memory import SessionMemorySettings
from domain.sessions import Session


class Sessions:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def get(self, session_id: str) -> Session | None:
        return self.session if self.session.id == session_id else None


class Settings:
    def __init__(self) -> None:
        self.value = SessionMemorySettings(session_id="session-1")

    async def get(self, session_id: str) -> SessionMemorySettings:
        return self.value

    async def save(self, settings: SessionMemorySettings) -> SessionMemorySettings:
        self.value = settings
        return settings


@pytest.fixture
def service_parts() -> tuple[SessionMemorySettingsService, Settings]:
    now = datetime.now(timezone.utc)
    session = Session.create(
        session_id="session-1",
        title="A chat",
        now=now,
        project_id="project-1",
    )
    settings = Settings()
    return SessionMemorySettingsService(Sessions(session), settings), settings


@pytest.mark.asyncio
async def test_get_uses_project_binding_from_session(
    service_parts: tuple[SessionMemorySettingsService, Settings],
) -> None:
    service, _ = service_parts

    result = await service.get("session-1")

    assert result.project_id == "project-1"


@pytest.mark.asyncio
async def test_update_preserves_project_binding_from_session(
    service_parts: tuple[SessionMemorySettingsService, Settings],
) -> None:
    service, settings = service_parts

    result = await service.update(
        "session-1", use_memories=False, generate_memories=True
    )

    assert result.project_id == "project-1"
    assert result.use_memories is False
    assert settings.value.project_id is None
