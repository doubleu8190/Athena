"""后台 Worker 共享的停机信号运行骨架。"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress


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


__all__ = ["run_with_shutdown_signals"]
