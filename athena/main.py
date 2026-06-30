"""FastAPI application factory and entry point for Athena Core."""

from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from athena.config import Config, get_config, set_config
from athena.logging_config import get_logger, setup_logging

logger = get_logger(__name__)


# ── Add project root for MCP server subprocess ────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifespan: startup and shutdown hooks."""
    # ── Startup ────────────────────────────────────────────────────
    logger.info("athena_starting")

    # Initialize configuration (already a singleton via get_config())
    config = get_config()

    # Initialize database (already a singleton via get_engine())
    from athena.models.base import get_engine
    engine = get_engine(config.sqlite_db_path)

    # Verify DB connectivity
    try:
        import sqlalchemy as sa
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        logger.info("database_connected", path=config.sqlite_db_path)
    except Exception:
        logger.error("database_connection_failed", path=config.sqlite_db_path)

    # Initialize Redis (already a singleton via get_redis_client())
    from athena.models.redis import get_redis_client
    get_redis_client(config.redis_url)

    # Initialize GatewayManager (singleton)
    from athena.gateway.manager import GatewayManager, set_gateway_manager
    gateway_manager = GatewayManager(config)
    await gateway_manager.start()
    set_gateway_manager(gateway_manager)

    # Initialize MCP Client (singleton)
    from athena.mcp_client.client import MCPClient, set_mcp_client
    from athena.mcp_client.seed_loader import auto_register_builtin_servers

    # Warm ToolRegistry singleton so MCP server auto-registration finds it
    from athena.mcp_client.registry import get_tool_registry
    get_tool_registry()

    await auto_register_builtin_servers(config)

    mcp_client = MCPClient(config)
    await mcp_client.start()
    set_mcp_client(mcp_client)

    logger.info("athena_started")

    yield

    # ── Shutdown ───────────────────────────────────────────────────
    logger.info("athena_stopping")

    await mcp_client.stop()
    await gateway_manager.stop()

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

    # CORS — origins configurable via CORS_ORIGINS env var (default "*")
    cors_origins = cfg.cors_origins if hasattr(cfg, "cors_origins") else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    from athena.api.health import router as health_router
    from athena.api.im import router as im_router
    from athena.api.admin import router as admin_router
    from athena.api.device import router as device_router

    app.include_router(health_router, prefix="/api/v1")
    app.include_router(im_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1/admin")
    app.include_router(device_router, prefix="/api/v1")

    # Prometheus metrics — custom middleware + endpoint (no third-party instrumentator)
    if cfg.prometheus_enabled:
        from starlette.requests import Request
        from starlette.responses import Response
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        from athena.api.metrics import (
            athena_http_requests_total,
            athena_http_request_duration_seconds,
        )

        @app.get("/metrics", include_in_schema=False)
        async def metrics():
            return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

        @app.middleware("http")
        async def metrics_middleware(request: Request, call_next):
            if request.url.path == "/metrics":
                return await call_next(request)
            start = time.monotonic()
            response = await call_next(request)
            duration = time.monotonic() - start
            endpoint = request.url.path
            athena_http_requests_total.labels(
                method=request.method,
                endpoint=endpoint,
                status_code=str(response.status_code),
            ).inc()
            athena_http_request_duration_seconds.labels(
                method=request.method,
                endpoint=endpoint,
            ).observe(duration)
            return response

        logger.info("prometheus_enabled")

    return app


# Module-level app instance (used by uvicorn)
app = create_app()


def main():
    """Entry point for uvicorn."""
    import uvicorn
    uvicorn.run("athena.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
