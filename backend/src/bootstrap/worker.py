"""后台 Worker 共享的瞬态故障判定、指数退避与停机信号运行骨架。"""

from __future__ import annotations

import asyncio
import random
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError


def is_recoverable_worker_error(error: Exception) -> bool:
    """判定是否为可恢复的瞬态数据库/网络连接错误，避免单次闪断打崩整个 TaskGroup。"""
    if isinstance(error, (OperationalError, InterfaceError, SqlAlchemyTimeoutError, OSError)):
        return True
    if not isinstance(error, DBAPIError):
        return False
    if error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    return isinstance(sqlstate, str) and (
        sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03", "53300"}
    )


def compute_worker_retry_delay(
    consecutive_failures: int,
    *,
    base_seconds: float = 0.5,
    max_seconds: float = 30.0,
) -> float:
    """计算有界全抖动指数退避延迟（秒），防止瞬态故障恢复期惊群重试。"""
    exponent = min(max(consecutive_failures - 1, 0), 6)
    ceiling = min(max_seconds, base_seconds * (2**exponent))
    return random.uniform(0.0, ceiling)


async def run_with_shutdown_signals(
    runner: Callable[[asyncio.Event], Awaitable[None]],
) -> None:
    """安装 SIGINT/SIGTERM 停机事件并运行协程，退出前严格解绑信号处理器。"""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:
            continue
        installed_signals.append(signal_name)
    try:
        await runner(stop_event)
    finally:
        for signal_name in installed_signals:
            with suppress(NotImplementedError):
                loop.remove_signal_handler(signal_name)


__all__ = [
    "compute_worker_retry_delay",
    "is_recoverable_worker_error",
    "run_with_shutdown_signals",
]
