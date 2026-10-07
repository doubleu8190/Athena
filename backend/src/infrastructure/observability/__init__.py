"""Target observability hooks with optional LangSmith integration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any


@asynccontextmanager
async def trace_run(name: str, *, metadata: dict[str, Any] | None = None):
    """Provide a no-op-safe tracing context for application services."""
    yield {"name": name, "metadata": metadata or {}}


__all__ = ["trace_run"]
