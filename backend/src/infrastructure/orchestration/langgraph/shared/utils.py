"""Shared helpers for LangGraph adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def call(value: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """调用同步或异步依赖。"""
    result = value(*args, **kwargs)
    if isinstance(result, Awaitable):
        return await result
    return result


__all__ = ["call"]
