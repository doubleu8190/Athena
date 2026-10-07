"""Application startup and shutdown orchestration."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from typing import Any


async def _start(resource: Any) -> None:
    """Start one resource using the smallest supported lifecycle protocol."""
    callback = getattr(resource, "start", None)
    if callback is not None:
        result = callback()
        if result is not None:
            await result
        return
    enter = getattr(resource, "__aenter__", None)
    if enter is not None:
        await enter()


async def _stop(resource: Any) -> None:
    """Stop one resource using the matching lifecycle protocol."""
    callback = getattr(resource, "stop", None)
    if callback is not None:
        result = callback()
        if result is not None:
            await result
        return
    exit_callback = getattr(resource, "__aexit__", None)
    if exit_callback is not None:
        await exit_callback(None, None, None)


@asynccontextmanager
async def lifespan(app: Any, resources: Iterable[Any] = ()) -> AsyncGenerator[None]:
    """Start resources in declaration order and always stop them in reverse.

    If startup fails halfway through, resources that already started are
    cleaned up before the original exception is re-raised.
    """
    started: list[Any] = []
    try:
        for resource in resources:
            await _start(resource)
            started.append(resource)
        app.state.startup_complete = True
        yield
    except BaseException:
        app.state.startup_complete = False
        for resource in reversed(started):
            await _stop(resource)
        raise
    else:
        for resource in reversed(started):
            await _stop(resource)
        app.state.startup_complete = False


__all__ = ["lifespan"]
