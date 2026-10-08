"""配置和 provider 查询端口。"""

from __future__ import annotations

from typing import Any, Protocol


class ProviderQueryPort(Protocol):
    async def list_providers(self) -> list[dict[str, Any]]: ...


class SettingsQueryPort(Protocol):
    async def get_settings(self) -> dict[str, Any]: ...


__all__ = ["ProviderQueryPort", "SettingsQueryPort"]
