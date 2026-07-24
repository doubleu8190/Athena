"""Health check endpoint."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from athena.logging_config import get_logger
from athena.models.base import get_redis, get_session_maker

logger = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> JSONResponse:
    """Health check — verifies SQLite and Redis connectivity."""
    status = {"status": "healthy", "checks": {}}

    # Check SQLite
    try:
        async with get_session_maker()() as db:
            await db.execute(text("SELECT 1"))
        status["checks"]["database"] = "ok"
    except Exception as e:
        status["checks"]["database"] = f"error: {e}"
        status["status"] = "degraded"

    # Check Redis
    try:
        redis = get_redis()
        await redis.ping()
        status["checks"]["redis"] = "ok"
    except Exception as e:
        status["checks"]["redis"] = f"error: {e}"
        status["status"] = "degraded"

    if status["status"] == "healthy":
        return JSONResponse(status_code=200, content=status)
    else:
        return JSONResponse(status_code=503, content=status)
