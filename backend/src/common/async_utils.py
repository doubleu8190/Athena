"""抗二次取消的异步任务与工作线程收敛辅助函数。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def await_task_shielded[T](
    task: asyncio.Task[T],
    *,
    on_cancel: Callable[[], object] | None = None,
) -> asyncio.CancelledError | None:
    """在抗二次取消循环中等待后台任务收敛，返回期间捕获的最后一次外部 CancelledError。

    保持严格的取消与异常隔离：
    - 使用挂载在目标任务上的 done 回调驱动 asyncio.Event 等待，彻底规避 asyncio.shield
      在外层取消后若内层任务以异常结束时触发 ``exception in shielded future`` 告警；
    - done_event.wait() 仅会因当前调用方被取消而抛出 CancelledError，天然区分外部取消与
      被等待任务自身取消：仅当当前调用方收到外部取消时记录 cancelled 并触发 on_cancel；
    - 发生外部取消时，持续等待任务退出并主动消费 task.exception()，严禁让后台异常覆盖
      外层 CancelledError，避免遗留未检索任务异常告警。
    """
    if task.done():
        return None
    done_event = asyncio.Event()

    def _mark_done(_done: asyncio.Task[T]) -> None:
        done_event.set()

    task.add_done_callback(_mark_done)
    cancelled: asyncio.CancelledError | None = None
    try:
        while not task.done():
            try:
                await done_event.wait()
            except asyncio.CancelledError as error:
                cancelled = error
                if on_cancel is not None:
                    on_cancel()
    finally:
        # 无论正常退出还是异常跳出均清理回调，避免事件对象被目标任务长期引用。
        task.remove_done_callback(_mark_done)
    if cancelled is not None and not task.cancelled():
        task.exception()
    return cancelled


async def run_thread_shielded[T](
    func: Callable[..., T],
    *args: Any,
    name: str | None = None,
    on_cancel: Callable[[], object] | None = None,
    **kwargs: Any,
) -> T:
    """在工作线程执行阻塞函数，收到取消时等待线程收敛退出后再重抛外层 CancelledError。"""
    task: asyncio.Task[T] = asyncio.create_task(
        asyncio.to_thread(func, *args, **kwargs),
        name=name,
    )
    cancelled = await await_task_shielded(task, on_cancel=on_cancel)
    if cancelled is not None:
        raise cancelled
    return task.result()


__all__ = ["await_task_shielded", "run_thread_shielded"]
