"""基于 contextlib.AsyncExitStack 的进程级资源逆序清理辅助。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from typing import Any


async def close_resources_lifo(
    cleanups: Sequence[tuple[str, Callable[[], Any]]],
    *,
    log: logging.Logger,
    reraise: bool = True,
    initial_error: BaseException | None = None,
    log_level: int = logging.ERROR,
    catch_base_exception: bool = True,
) -> None:
    """使用 AsyncExitStack 按注册逆序（LIFO）逐项释放资源。

    调用方按资源创建顺序传入 `(event_name, cleanup)` 列表，退出时按逆序执行；
    单项释放失败仅记录日志，不阻断后续资源清理。当 `reraise=True` 时，优先重抛
    `initial_error`，否则重抛首个清理阶段异常。
    """
    first_error: BaseException | None = initial_error
    caught_type: type[BaseException] = BaseException if catch_base_exception else Exception

    async def _guarded_close(event_name: str, cleanup: Callable[[], Any]) -> None:
        nonlocal first_error
        try:
            result = cleanup()
            if isinstance(result, Awaitable):
                await result
        except caught_type as error:
            log.log(
                log_level,
                "%s exception_type=%s",
                event_name,
                type(error).__name__,
            )
            if first_error is None:
                first_error = error

    async with AsyncExitStack() as stack:
        for event_name, cleanup in cleanups:
            stack.push_async_callback(_guarded_close, event_name, cleanup)
    if reraise and first_error is not None:
        raise first_error


__all__ = ["close_resources_lifo"]
