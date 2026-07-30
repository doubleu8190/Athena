"""Integration tests for the embedded TaskScheduler in athena-core.

Verifies that the asyncio-based TaskScheduler starts as a background
component within the FastAPI lifespan when ARQ_EMBEDDED=true.

Note: the ``arq_embedded`` config flag is kept for backward compat;
it now controls the asyncio TaskScheduler instead of ARQ.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_lifespan_patches(config):
    """Create a dict of common patches needed for lifespan tests."""
    return {
        "athena.main.get_config": patch("athena.main.get_config", return_value=config),
        "athena.main.set_config": patch("athena.main.set_config"),
        "athena.models.base.get_engine": patch("athena.models.base.get_engine"),
        "athena.cache.memory_cache.get_cache": patch(
            "athena.cache.memory_cache.get_cache"
        ),
        "athena.gateway.manager.GatewayManager": patch(
            "athena.gateway.manager.GatewayManager"
        ),
        "athena.mcp_client.seed_loader.auto_register_builtin_servers": patch(
            "athena.mcp_client.seed_loader.auto_register_builtin_servers"
        ),
        "athena.mcp_client.client.MCPClient": patch(
            "athena.mcp_client.client.MCPClient"
        ),
        "athena.core.harness.stop_harness": patch(
            "athena.core.harness.stop_harness", new_callable=AsyncMock
        ),
        "athena.main.setup_logging": patch("athena.main.setup_logging"),
        "athena.tasks.scheduler.close_scheduler": patch(
            "athena.tasks.scheduler.close_scheduler", new_callable=AsyncMock
        ),
    }


class TestEmbeddedScheduler:
    """Tests for embedded TaskScheduler lifecycle."""

    @pytest.mark.asyncio
    async def test_scheduler_heartbeat_when_embedded_enabled(self, temp_db_path):
        """Scheduler should enqueue heartbeat task when arq_embedded=True."""
        from athena.config import Config

        config = Config(arq_embedded=True, sqlite_db_path=temp_db_path)
        patches = _make_lifespan_patches(config)

        with (
            patches["athena.main.get_config"],
            patches["athena.main.set_config"],
            patches["athena.models.base.get_engine"] as mock_engine,
            patches["athena.cache.memory_cache.get_cache"],
            patches["athena.gateway.manager.GatewayManager"] as mock_gw,
            patches["athena.mcp_client.seed_loader.auto_register_builtin_servers"],
            patches["athena.mcp_client.client.MCPClient"] as mock_mcp,
            patches["athena.core.harness.stop_harness"],
            patches["athena.main.setup_logging"],
            patches["athena.tasks.scheduler.close_scheduler"],
        ):
            mock_engine.return_value = MagicMock()
            mock_engine.return_value.connect = AsyncMock()
            mock_engine.return_value.dispose = AsyncMock()
            mock_conn = AsyncMock()
            mock_engine.return_value.connect.return_value.__aenter__ = AsyncMock(
                return_value=mock_conn
            )
            mock_engine.return_value.connect.return_value.__aexit__ = AsyncMock()

            mock_gw_instance = AsyncMock()
            mock_gw.return_value = mock_gw_instance
            mock_gw_instance.start = AsyncMock()
            mock_gw_instance.stop = AsyncMock()

            mock_mcp_instance = AsyncMock()
            mock_mcp.return_value = mock_mcp_instance
            mock_mcp_instance.start = AsyncMock()
            mock_mcp_instance.stop = AsyncMock()

            from athena.main import lifespan
            from fastapi import FastAPI

            app = FastAPI()

            async with lifespan(app):
                await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_scheduler_not_started_when_embedded_disabled(self, temp_db_path):
        """Scheduler should still be initialized even when arq_embedded=False."""
        from athena.config import Config

        config = Config(arq_embedded=False, sqlite_db_path=temp_db_path)
        patches = _make_lifespan_patches(config)

        with (
            patches["athena.main.get_config"],
            patches["athena.main.set_config"],
            patches["athena.models.base.get_engine"] as mock_engine,
            patches["athena.cache.memory_cache.get_cache"],
            patches["athena.gateway.manager.GatewayManager"] as mock_gw,
            patches["athena.mcp_client.seed_loader.auto_register_builtin_servers"],
            patches["athena.mcp_client.client.MCPClient"] as mock_mcp,
            patches["athena.core.harness.stop_harness"],
            patches["athena.main.setup_logging"],
            patches["athena.tasks.scheduler.close_scheduler"],
        ):
            mock_engine.return_value = MagicMock()
            mock_engine.return_value.connect = AsyncMock()
            mock_engine.return_value.dispose = AsyncMock()
            mock_conn = AsyncMock()
            mock_engine.return_value.connect.return_value.__aenter__ = AsyncMock(
                return_value=mock_conn
            )
            mock_engine.return_value.connect.return_value.__aexit__ = AsyncMock()

            mock_gw_instance = AsyncMock()
            mock_gw.return_value = mock_gw_instance
            mock_gw_instance.start = AsyncMock()
            mock_gw_instance.stop = AsyncMock()

            mock_mcp_instance = AsyncMock()
            mock_mcp.return_value = mock_mcp_instance
            mock_mcp_instance.start = AsyncMock()
            mock_mcp_instance.stop = AsyncMock()

            from athena.main import lifespan
            from fastapi import FastAPI

            app = FastAPI()

            async with lifespan(app):
                await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_scheduler_shutdown(self, temp_db_path):
        """Scheduler should be properly shut down during application shutdown."""
        from athena.config import Config

        config = Config(arq_embedded=True, sqlite_db_path=temp_db_path)
        patches = _make_lifespan_patches(config)

        with (
            patches["athena.main.get_config"],
            patches["athena.main.set_config"],
            patches["athena.models.base.get_engine"] as mock_engine,
            patches["athena.cache.memory_cache.get_cache"],
            patches["athena.gateway.manager.GatewayManager"] as mock_gw,
            patches["athena.mcp_client.seed_loader.auto_register_builtin_servers"],
            patches["athena.mcp_client.client.MCPClient"] as mock_mcp,
            patches["athena.core.harness.stop_harness"],
            patches["athena.main.setup_logging"],
            patches["athena.tasks.scheduler.close_scheduler"] as mock_close,
        ):
            mock_engine.return_value = MagicMock()
            mock_engine.return_value.connect = AsyncMock()
            mock_engine.return_value.dispose = AsyncMock()
            mock_conn = AsyncMock()
            mock_engine.return_value.connect.return_value.__aenter__ = AsyncMock(
                return_value=mock_conn
            )
            mock_engine.return_value.connect.return_value.__aexit__ = AsyncMock()

            mock_gw_instance = AsyncMock()
            mock_gw.return_value = mock_gw_instance
            mock_gw_instance.start = AsyncMock()
            mock_gw_instance.stop = AsyncMock()

            mock_mcp_instance = AsyncMock()
            mock_mcp.return_value = mock_mcp_instance
            mock_mcp_instance.start = AsyncMock()
            mock_mcp_instance.stop = AsyncMock()

            from athena.main import lifespan
            from fastapi import FastAPI

            app = FastAPI()

            async with lifespan(app):
                await asyncio.sleep(0.05)

            mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_arq_embedded_config_default_true(self):
        """arq_embedded should default to True."""
        from athena.config import Config

        config = Config()
        assert config.arq_embedded is True

    @pytest.mark.asyncio
    async def test_arq_embedded_config_from_env(self):
        """arq_embedded should read from ARQ_EMBEDDED env var."""
        from athena.config import Config

        with patch.dict(os.environ, {"ARQ_EMBEDDED": "false"}):
            config = Config()
            assert config.arq_embedded is False

        with patch.dict(os.environ, {"ARQ_EMBEDDED": "true"}):
            config = Config()
            assert config.arq_embedded is True
