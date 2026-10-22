"""Target-owned subprocess sandbox boundary.

Deployments can replace this implementation with a container runner through
the same target ``SandboxPort``. The default runner has bounded output and
timeouts and never invokes a shell.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from domain.integrations import SandboxPort
from infrastructure.process_control import (
    isolated_process_options,
    terminate_process_group,
)

class SubprocessSandbox(SandboxPort):
    def __init__(self, *, max_output_bytes: int = 1_000_000) -> None:
        self._max_output_bytes = max_output_bytes
        self._processes: set[asyncio.subprocess.Process] = set()

    async def execute(self, request: Any) -> Any:
        started = asyncio.get_running_loop().time()
        process = None
        communicate_task = None
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv,
                cwd=str(request.workspace),
                env=dict(request.env) if request.env else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **isolated_process_options(),
            )
            self._processes.add(process)
            communicate_task = asyncio.create_task(process.communicate())
            done, _pending = await asyncio.wait(
                {communicate_task}, timeout=request.timeout_seconds
            )
            if not done:
                await terminate_process_group(process)
                stdout, stderr = await communicate_task
                return SimpleNamespace(
                    status="timeout",
                    exit_code=None,
                    stdout=stdout[: self._max_output_bytes].decode(
                        "utf-8", errors="replace"
                    ),
                    stderr="sandbox_timeout",
                    duration_ms=(asyncio.get_running_loop().time() - started)
                    * 1000,
                )
            stdout, stderr = communicate_task.result()
        except asyncio.CancelledError:
            if process is not None:
                await terminate_process_group(process)
            if communicate_task is not None:
                await asyncio.gather(communicate_task, return_exceptions=True)
            raise
        except OSError as exc:
            return SimpleNamespace(status="runner_unavailable", exit_code=None, stdout="", stderr=str(exc), duration_ms=0)
        finally:
            if process is not None:
                self._processes.discard(process)
        output = stdout[: self._max_output_bytes]
        error = stderr[: max(0, self._max_output_bytes - len(output))]
        return SimpleNamespace(
            status="success" if process.returncode == 0 else "failed",
            exit_code=process.returncode,
            stdout=output.decode("utf-8", errors="replace"),
            stderr=error.decode("utf-8", errors="replace"),
            duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
        )

    async def health(self) -> bool:
        return True

    async def close_all(self) -> None:
        await asyncio.gather(
            *(
                terminate_process_group(process)
                for process in tuple(self._processes)
            ),
            return_exceptions=True,
        )
        self._processes.clear()

    def build_mcp_process(self, **kwargs: Any) -> Any:
        return SimpleNamespace(command=kwargs.get("command"), args=list(kwargs.get("args", ())), env=dict(kwargs.get("env") or {}))


__all__ = ["SubprocessSandbox"]
