"""配置和模型 Provider 查询用例。"""

from __future__ import annotations

from typing import Any

from domain.configuration import ProviderQueryPort, SettingsQueryPort


class ProviderQueryService:
    def __init__(self, port: ProviderQueryPort) -> None:
        self._port = port

    async def list_providers(self) -> list[dict[str, Any]]:
        return await self._port.list_providers()


class SettingsQueryService:
    def __init__(self, port: SettingsQueryPort) -> None:
        self._port = port

    async def get_settings(self) -> dict[str, Any]:
        return await self._port.get_settings()


__all__ = ["ProviderQueryService", "SettingsQueryService"]
