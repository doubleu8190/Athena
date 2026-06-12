"""FastAPI dependencies — DB session, config, Redis, API key auth."""

from __future__ import annotations

import os
from typing import AsyncGenerator

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from athena.config import Config, get_config
from athena.models import get_session_maker
from athena.models.redis import get_redis_client
from athena.logging_config import get_logger

logger = get_logger(__name__)

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

def get_redis():
    """FastAPI dependency: get the Redis client."""
    config = get_config()
    return get_redis_client(config.redis_url)


# ── Auth ──────────────────────────────────────────────────────────────

async def verify_api_key(
    api_key: str | None = Security(api_key_header),
    config: Config = Depends(get_config_dep),
) -> str:
    """Verify the X-API-Key header against the configured admin key.

    Returns the validated API key on success.
    Raises HTTPException(401) on failure.
    """
    configured_key = config.admin_api_key
    if not configured_key:
        # No key configured — allow all (development mode)
        logger.warning("api_key_not_configured_allowing_all")
        return api_key or "dev-mode"

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail={
                "code": 40101,
                "message": "Missing API Key",
                "detail": "X-API-Key header is required",
                "data": None,
            },
        )

    if api_key != configured_key:
        raise HTTPException(
            status_code=401,
            detail={
                "code": 40101,
                "message": "Invalid API Key",
                "detail": "The provided API key is invalid",
                "data": None,
            },
        )

    return api_key
