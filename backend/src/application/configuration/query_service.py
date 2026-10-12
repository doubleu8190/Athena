"""配置和模型 Provider 查询用例。"""

from __future__ import annotations

from typing import Any

from domain.configuration import ProviderQueryPort, SettingsQueryPort
from application.common.responses import ProviderResponse, SettingsResponse


class ProviderQueryService:
    def __init__(self, port: ProviderQueryPort) -> None:
        self._port = port

    async def list_providers(self) -> list[ProviderResponse]:
        return [ProviderResponse(**value) for value in await self._port.list_providers()]


class SettingsQueryService:
    def __init__(self, port: SettingsQueryPort) -> None:
        self._port = port

    async def get_settings(self) -> SettingsResponse:
        return SettingsResponse(dict(await self._port.get_settings()))


__all__ = ["ProviderQueryService", "SettingsQueryService"]
