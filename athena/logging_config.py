"""Structured logging configuration using structlog.

Produces JSON-formatted logs on stdout for consumption by Filebeat/Docker log driver.
All log events MUST include: event, level, timestamp.
Token-related logs MUST include token_usage sub-object.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog for JSON output on stdout.

    Args:
        log_level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure standard library logging to route through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            # Add log level
            structlog.stdlib.add_log_level,
            # Add logger name
            structlog.stdlib.add_logger_name,
            # Add timestamp in ISO 8601 UTC
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Format as JSON
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Set level on the root structlog logger
    logging.getLogger().setLevel(level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Logger name (typically __name__).

    Returns:
        A bound structlog logger.
    """
    return structlog.get_logger(name or "athena")


def bind_context(**kwargs: object) -> structlog.stdlib.BoundLogger:
    """Bind context variables to the current logger.

    Common keys: session_id, task_id, step, tool_name, mcp_server_id.

    Usage:
        logger = bind_context(session_id="sess_123", task_id="task_456")
        logger.info("subtask_executed", status="success")
    """
    return structlog.get_logger().bind(**kwargs)
