"""FastAPI application factory and entry point for Athena Core."""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from athena.config import Config, get_config, set_config
from athena.logging_config import get_logger, setup_logging

logger = get_logger(__name__)


# ── Add project root for MCP server subprocess ────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown hooks."""
    # ── Startup ────────────────────────────────────────────────────
    logger.info("athena_starting")

    # Initialize configuration
    config = get_config()
    app.state.config = config

    # Initialize database (ensure tables exist via Alembic — migrations
    # are run at container entrypoint; here we just verify connectivity)
    from athena.models.base import get_engine
    engine = get_engine(config.sqlite_db_path)
    app.state.db_engine = engine

    # Verify DB connectivity
    try:
        import sqlalchemy as sa
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        logger.info("database_connected", path=config.sqlite_db_path)
    except Exception:
        logger.error("database_connection_failed", path=config.sqlite_db_path)

    # Initialize Redis
    from athena.models.redis import get_redis_client
    redis_client = get_redis_client(config.redis_url)
    app.state.redis = redis_client

    # Initialize GatewayManager
    from athena.gateway.manager import GatewayManager
    gateway_manager = GatewayManager(config)
    await gateway_manager.start()
    app.state.gateway_manager = gateway_manager

    # Initialize MCP Client
    from athena.mcp_client.client import MCPClient
    from athena.mcp_client.registry import ToolRegistry
    from athena.mcp_client.seed_loader import seed_mcp_servers

    app.state.tool_registry = ToolRegistry()
    await seed_mcp_servers(config)

    mcp_client = MCPClient(config, app.state.tool_registry)
    await mcp_client.start()
    app.state.mcp_client = mcp_client

    logger.info("athena_started")

    yield

    # ── Shutdown ───────────────────────────────────────────────────
    logger.info("athena_stopping")

    if hasattr(app.state, "mcp_client"):
        await app.state.mcp_client.stop()
    if hasattr(app.state, "gateway_manager"):
        await app.state.gateway_manager.stop()

    await engine.dispose()
    logger.info("athena_stopped")


def create_app(config: Config | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: Optional Config instance (uses singleton if not provided).

    Returns:
        Configured FastAPI application.
    """
    if config is not None:
        set_config(config)
    cfg = config or get_config()

    setup_logging(cfg.log_level)

    app = FastAPI(
        title="Athena",
        description="Personal AI Assistant",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — permissive for local/development use
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Prometheus metrics — must be added before app starts
    if cfg.prometheus_enabled:
        instrumentator = Instrumentator()
        instrumentator.instrument(app).expose(app, endpoint="/metrics")
        logger.info("prometheus_enabled")

    # Register routes
    from athena.api.health import router as health_router
    from athena.api.im import router as im_router
    from athena.api.admin import router as admin_router
    from athena.api.device import router as device_router

    app.include_router(health_router, prefix="/api/v1")
    app.include_router(im_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(device_router, prefix="/api/v1")

    # Production mode: serve frontend static files (SPA fallback)
    frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
    if frontend_dist.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
        logger.info("frontend_static_mounted", path=str(frontend_dist))

    return app


# Module-level app instance (used by uvicorn)
app = create_app()


def main():
    """Entry point for uvicorn."""
    import uvicorn
    uvicorn.run("athena.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
