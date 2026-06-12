"""FastAPI middleware for request-ID injection and structlog context binding.

Each request gets a unique request_id. If a session_id is present in
the request context, it's bound to the structlog context for the
duration of that request.
"""

from __future__ import annotations

import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from athena.logging_config import get_logger

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Injects request_id and binds session_id to structlog context.

    Adds X-Request-ID header to the response.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

        # Bind context for this request
        request.state.request_id = request_id

        # Process the request
        response = await call_next(request)

        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id

        return response
