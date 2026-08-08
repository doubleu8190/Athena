"""日志配置 — 基于 structlog 的结构化日志."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(debug: bool = False) -> None:
    """配置 structlog，在应用启动时调用一次."""
    level = logging.DEBUG if debug else logging.INFO

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.set_exc_info,
            structlog.processors.StackInfoRenderer(),
            # colors=isatty():仅当输出到真实终端时着色，
            # 重定向到文件(如 start.sh >> logs/backend.log)时输出纯文本，
            # 避免日志文件被 ANSI 颜色转义序列污染。
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取结构化日志记录器."""
    return structlog.get_logger(name)
