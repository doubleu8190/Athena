"""Shared helpers for LangGraph adapters."""

from __future__ import annotations

from collections.abc import Callable
import asyncio
import inspect
from typing import Any


async def call(value: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """调用同步或异步依赖。"""
    if inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(getattr(value, "__call__", None)):
        result = value(*args, **kwargs)
    else:
        result = await asyncio.to_thread(value, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


__all__ = ["call"]
