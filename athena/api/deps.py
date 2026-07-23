"""FastAPI dependencies — DB session, config, Redis, API key auth."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from athena.mcp_client.client import MCPClient

from athena.config import Config, get_config
from athena.models import get_session_maker
from athena.models.redis import get_redis_client

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ── Config ────────────────────────────────────────────────────────────

def get_config_dep() -> Config:
    """FastAPI dependency: get the application configuration."""
    return get_config()


# ── Database ──────────────────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yield an async SQLAlchemy session."""
    config = get_config()
    session_maker = get_session_maker(config.sqlite_db_path)
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


# ── Redis ─────────────────────────────────────────────────────────────

def get_redis() -> aioredis.Redis:
    """FastAPI dependency: get the Redis client."""
    config = get_config()
    return get_redis_client(config.redis_url)


# ── Auth ──────────────────────────────────────────────────────────────

async def verify_api_key(
    api_key: str | None = Security(api_key_header),
    config: Config = Depends(get_config_dep),
) -> str:
    """Admin API key verification — currently a no-op (LAN-only deployment).

    When Athena is deployed on a public network, re-add key validation here.
    All admin endpoints already pass through this dependency.
    """
    return "lan-mode"


# ── MCP Client ──────────────────────────────────────────────────────────

def get_mcp_client_dep() -> "MCPClient":
    """FastAPI dependency: get the singleton MCPClient."""
    from athena.mcp_client.client import get_mcp_client
    return get_mcp_client()
