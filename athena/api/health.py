"""Health check endpoint."""

from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from athena.api.deps import get_db, get_redis
from athena.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Health check — verifies SQLite and Redis connectivity."""
    status = {"status": "healthy", "checks": {}}

    # Check SQLite
    try:
        await db.execute(text("SELECT 1"))
        status["checks"]["database"] = "ok"
    except Exception as e:
        status["checks"]["database"] = f"error: {e}"
        status["status"] = "degraded"

    # Check Redis
    try:
        await redis.ping()
        status["checks"]["redis"] = "ok"
    except Exception as e:
        status["checks"]["redis"] = f"error: {e}"
        status["status"] = "degraded"

    if status["status"] == "healthy":
        return status
    else:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=status)
