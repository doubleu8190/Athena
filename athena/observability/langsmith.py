"""LangSmith tracing with a no-op fallback.

The application has its own durable event stream, so LangSmith is deliberately
optional.  A tracing failure must never change the result of an Agent run.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from uuid import UUID, uuid5

from athena.utils.logging import get_logger

logger = get_logger(__name__)
_TRACE_NAMESPACE = UUID("6f4f1c6b-7a8e-4cc7-b6af-0e5c1ed98d2b")
_current_run: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "athena_langsmith_run", default=None
)


def langsmith_enabled() -> bool:
    """Return whether explicit LangSmith tracing is configured."""

    return (
        os.getenv("LANGCHAIN_TRACING_V2", "").lower() in {"1", "true", "yes"}
        and bool(os.getenv("LANGCHAIN_API_KEY"))
    )


def _run_id(value: str) -> UUID:
    return uuid5(_TRACE_NAMESPACE, value)


def _metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    return {str(k): v for k, v in (metadata or {}).items() if v is not None}


def _new_run(
    *,
    name: str,
    run_type: str,
    inputs: dict[str, Any],
    run_id: str | None = None,
    parent: Any | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> Any | None:
    if not langsmith_enabled():
        return None
    try:
        from langsmith.run_trees import RunTree

        kwargs: dict[str, Any] = {
            "name": name,
            "run_type": run_type,
            "inputs": inputs,
            "tags": tags or [],
            "extra": {"metadata": _metadata(metadata)},
        }
        if parent is not None:
            if run_id:
                kwargs["run_id"] = _run_id(run_id)
            return parent.create_child(**kwargs)
        kwargs["project_name"] = os.getenv("LANGCHAIN_PROJECT", "athena")
        if run_id:
            kwargs["id"] = _run_id(run_id)
        return RunTree(**kwargs)
    except Exception:
        logger.warning("langsmith_trace_start_failed", exc_info=True)
        return None


async def _post(run: Any, *, recursive: bool = False) -> None:
    try:
        await asyncio.to_thread(run.post, exclude_child_runs=not recursive)
    except Exception:
        logger.warning("langsmith_trace_post_failed", exc_info=True)


@asynccontextmanager
async def start_trace(
    *,
    name: str,
    inputs: dict[str, Any],
    run_id: str,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> AsyncGenerator[Any | None, None]:
    """Create a root trace and post it together with all completed children."""

    run = _new_run(
        name=name,
        run_type="chain",
        inputs=inputs,
        run_id=run_id,
        metadata=metadata,
        tags=tags,
    )
    token = _current_run.set(run) if run is not None else None
    try:
        if run is None:
            yield None
        else:
            from langsmith.run_helpers import tracing_context

            with tracing_context(
                parent=run,
                project_name=os.getenv("LANGCHAIN_PROJECT", "athena"),
                enabled=True,
            ):
                yield run
    except BaseException as exc:
        if run is not None:
            run.end(error=str(exc))
        raise
    finally:
        if run is not None:
            if run.end_time is None:
                run.end(end_time=datetime.now(timezone.utc))
            await _post(run, recursive=True)
        if token is not None:
            _current_run.reset(token)


@asynccontextmanager
async def trace_span(
    *,
    name: str,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> AsyncGenerator[Any | None, None]:
    """Create a child span under the current trace, if one exists."""

    parent = _current_run.get()
    run = _new_run(
        name=name,
        run_type=run_type,
        inputs=inputs or {},
        run_id=run_id,
        parent=parent,
        metadata=metadata,
        tags=tags,
    )
    token = _current_run.set(run) if run is not None else None
    try:
        if run is None:
            yield None
        else:
            from langsmith.run_helpers import tracing_context

            with tracing_context(
                parent=run,
                project_name=os.getenv("LANGCHAIN_PROJECT", "athena"),
                enabled=True,
            ):
                yield run
    except BaseException as exc:
        if run is not None:
            run.end(error=str(exc))
        raise
    finally:
        if run is not None:
            if run.end_time is None:
                run.end()
        if token is not None:
            _current_run.reset(token)


def finish_span(
    run: Any | None,
    *,
    outputs: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Update a span without making tracing a required dependency."""

    if run is None:
        return
    try:
        run.end(outputs=outputs, error=error)
    except Exception:
        logger.warning("langsmith_trace_end_failed", exc_info=True)
