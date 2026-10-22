"""Structlog facade integrated with the application's standard log handlers."""

from __future__ import annotations

import structlog
from structlog import stdlib


structlog.configure(
    processors=[
        stdlib.filter_by_level,
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(ensure_ascii=False),
    ],
    wrapper_class=stdlib.BoundLogger,
    logger_factory=stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)


def get_logger(name: str) -> stdlib.BoundLogger:
    """Return a structured logger while honoring the process logging setup."""
    return stdlib.get_logger(name)


__all__ = ["get_logger"]
