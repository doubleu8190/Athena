"""Query adapters backed by the application configuration object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bootstrap.config import Settings


class ConfiguredSettingsQueries:
    """Expose provider and runtime settings through query-port methods."""

    def __init__(self, settings: "Settings") -> None:
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
            for item in self._settings.llm_providers
        ]

    async def get_settings(self) -> dict[str, Any]:
        """返回运行时设置的公开字段。"""
        return {
            "host": self._settings.host,
            "port": self._settings.port,
            "debug": self._settings.debug,
            "database_backend": self._settings.database_backend,
            "workers_enabled": self._settings.workers_enabled,
        }


__all__ = ["ConfiguredSettingsQueries"]
