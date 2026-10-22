"""Process-group helpers for cancellable external commands."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any


def isolated_process_options() -> dict[str, bool]:
    """Start POSIX commands in a group that can be stopped as one unit."""
    return {"start_new_session": True} if os.name == "posix" else {}


async def terminate_process_group(
    process: Any, *, grace_seconds: float = 0.25
) -> None:
    """Terminate a command and its descendants, escalating after a short grace."""
    if process.returncode is not None:
        if os.name != "posix":
            return
        # The leader may have exited while a descendant still holds resources.
        _signal_process_group(process.pid, signal.SIGTERM)
        await asyncio.sleep(grace_seconds)
        _signal_process_group(process.pid, signal.SIGKILL)
        return

    if os.name == "posix":
        _signal_process_group(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        pass
    if os.name == "posix":
        _signal_process_group(process.pid, signal.SIGKILL)
    elif process.returncode is None:
        process.kill()
    if process.returncode is None:
        await process.wait()


def _signal_process_group(process_id: int, signal_number: int) -> None:
    try:
        os.killpg(process_id, signal_number)
    except ProcessLookupError:
        return


__all__ = ["isolated_process_options", "terminate_process_group"]
