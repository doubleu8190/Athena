"""Integration tests for the embedded ARQ worker in athena-core.

Verifies that the ARQ worker starts as a background asyncio task within
the FastAPI lifespan when ARQ_EMBEDDED=true, and is properly cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _inject_mock_arq_module():
    """Inject a mock arq module into sys.modules if not already installed."""
    if "arq" not in sys.modules:
        mock_arq = MagicMock()
        mock_arq.run_worker = AsyncMock()
        sys.modules["arq"] = mock_arq
        sys.modules["arq.connections"] = MagicMock()
    return sys.modules["arq"]


def _make_lifespan_patches(config, mock_run_worker):
    """Create a dict of common patches needed for lifespan tests."""
    return {
        "athena.main.get_config": patch("athena.main.get_config", return_value=config),
        "athena.main.set_config": patch("athena.main.set_config"),
        "athena.models.base.get_engine": patch("athena.models.base.get_engine"),
        "athena.models.redis.get_redis_client": patch(
            "athena.models.redis.get_redis_client"
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
    }


class TestEmbeddedArqWorker:
    """Tests for embedded ARQ worker lifecycle."""

    @pytest.mark.asyncio
    async def test_arq_worker_starts_when_embedded_enabled(self, temp_db_path):
        """ARQ worker background task should be created when arq_embedded=True."""
        from athena.config import Config

        mock_arq = _inject_mock_arq_module()
        mock_run_worker = AsyncMock()
        mock_arq.run_worker = mock_run_worker

        config = Config(arq_embedded=True, sqlite_db_path=temp_db_path)
        patches = _make_lifespan_patches(config, mock_run_worker)

        with (
            patches["athena.main.get_config"],
            patches["athena.main.set_config"],
            patches["athena.models.base.get_engine"] as mock_engine,
            patches["athena.models.redis.get_redis_client"],
            patches["athena.gateway.manager.GatewayManager"] as mock_gw,
            patches["athena.mcp_client.seed_loader.auto_register_builtin_servers"],
            patches["athena.mcp_client.client.MCPClient"] as mock_mcp,
            patches["athena.core.harness.stop_harness"],
            patches["athena.main.setup_logging"],
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
                mock_run_worker.assert_called_once()

    @pytest.mark.asyncio
    async def test_arq_worker_not_started_when_embedded_disabled(self, temp_db_path):
        """ARQ worker should NOT start when arq_embedded=False."""
        from athena.config import Config

        mock_arq = _inject_mock_arq_module()
        mock_run_worker = AsyncMock()
        mock_arq.run_worker = mock_run_worker

        config = Config(arq_embedded=False, sqlite_db_path=temp_db_path)
        patches = _make_lifespan_patches(config, mock_run_worker)

        with (
            patches["athena.main.get_config"],
            patches["athena.main.set_config"],
            patches["athena.models.base.get_engine"] as mock_engine,
            patches["athena.models.redis.get_redis_client"],
            patches["athena.gateway.manager.GatewayManager"] as mock_gw,
            patches["athena.mcp_client.seed_loader.auto_register_builtin_servers"],
            patches["athena.mcp_client.client.MCPClient"] as mock_mcp,
            patches["athena.core.harness.stop_harness"],
            patches["athena.main.setup_logging"],
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
                mock_run_worker.assert_not_called()

    @pytest.mark.asyncio
    async def test_arq_worker_cancelled_on_shutdown(self, temp_db_path):
        """ARQ worker background task should be cancelled during shutdown."""
        from athena.config import Config

        worker_started = asyncio.Event()
        worker_cancelled = asyncio.Event()

        async def mock_run_worker_side_effect(*args, **kwargs):
            worker_started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                worker_cancelled.set()
                raise

        mock_arq = _inject_mock_arq_module()
        mock_run_worker = AsyncMock(side_effect=mock_run_worker_side_effect)
        mock_arq.run_worker = mock_run_worker

        config = Config(arq_embedded=True, sqlite_db_path=temp_db_path)
        patches = _make_lifespan_patches(config, mock_run_worker)

        with (
            patches["athena.main.get_config"],
            patches["athena.main.set_config"],
            patches["athena.models.base.get_engine"] as mock_engine,
            patches["athena.models.redis.get_redis_client"],
            patches["athena.gateway.manager.GatewayManager"] as mock_gw,
            patches["athena.mcp_client.seed_loader.auto_register_builtin_servers"],
            patches["athena.mcp_client.client.MCPClient"] as mock_mcp,
            patches["athena.core.harness.stop_harness"],
            patches["athena.main.setup_logging"],
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
                await asyncio.wait_for(worker_started.wait(), timeout=1.0)

            assert worker_cancelled.is_set()

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
