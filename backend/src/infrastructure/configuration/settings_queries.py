"""Query adapters backed by the application configuration object."""

from __future__ import annotations

from typing import Any

from domain.configuration import ProviderQueryPort, SettingsQueryPort
from .options import PublicConfiguration


class ConfiguredSettingsQueries(ProviderQueryPort, SettingsQueryPort):
    """Expose provider and runtime settings through query-port methods."""

    def __init__(self, settings: PublicConfiguration) -> None:
        self._settings = settings

    async def list_providers(self) -> list[dict[str, Any]]:
        """返回已配置模型的公开摘要，不暴露密钥内容。"""
        return [
            {
                "name": item.name,
                "provider": item.provider,
                "model": item.model,
                "base_url": item.base_url,
                "api_key_configured": bool(item.api_key),
                "api_key_masked": "***" if item.api_key else "",
                "temperature": item.temperature,
                "max_tokens": item.max_tokens,
            }
            for item in self._settings.providers
        ]

    async def get_settings(self) -> dict[str, Any]:
        """返回运行时设置的公开字段。"""
        return {
            "host": self._settings.runtime.host,
            "port": self._settings.runtime.port,
            "debug": self._settings.runtime.debug,
            "database_backend": self._settings.runtime.database_backend,
            "workers_enabled": self._settings.runtime.workers_enabled,
        }


__all__ = ["ConfiguredSettingsQueries"]
