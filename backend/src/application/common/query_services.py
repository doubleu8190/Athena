"""配置、记忆和检索的查询用例。"""

from __future__ import annotations

from typing import Any

from domain.common.query_ports import (
    MemoryQueryPort,
    ProviderQueryPort,
    RetrievalQueryPort,
    SettingsQueryPort,
)


class ProviderQueryService:
    def __init__(self, port: ProviderQueryPort) -> None:
        self._port = port

    async def list(self) -> list[dict[str, Any]]:
        return await self._port.list_providers()


class SettingsQueryService:
    def __init__(self, port: SettingsQueryPort) -> None:
        self._port = port

    async def get(self) -> dict[str, Any]:
        return await self._port.get_settings()


class MemoryQueryService:
    def __init__(self, port: MemoryQueryPort) -> None:
        self._port = port

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._port.search(**kwargs)

    async def list(self, **kwargs: Any) -> dict[str, Any]:
        return await self._port.list(**kwargs)

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        return await self._port.get(memory_id)

    async def revisions(self, memory_id: str) -> list[dict[str, Any]]:
        return await self._port.revisions(memory_id)


class RetrievalQueryService:
    def __init__(self, port: RetrievalQueryPort) -> None:
        self._port = port

    async def list_runs(self, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        return await self._port.list_runs(**filters)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._port.get_run(run_id)
