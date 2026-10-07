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


class SubprocessSandbox:
    def __init__(self, *, max_output_bytes: int = 1_000_000) -> None:
        self._max_output_bytes = max_output_bytes
        self._processes: set[asyncio.subprocess.Process] = set()

    async def execute(self, request: Any) -> Any:
        started = asyncio.get_running_loop().time()
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv,
                cwd=str(request.workspace),
                env=dict(request.env) if request.env else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._processes.add(process)
            stdout, stderr = await asyncio.wait_for(process.communicate(), request.timeout_seconds)
        except asyncio.TimeoutError:
            if "process" in locals():
                process.kill()
                await process.wait()
            return SimpleNamespace(status="timeout", exit_code=None, stdout="", stderr="sandbox_timeout", duration_ms=0)
        except OSError as exc:
            return SimpleNamespace(status="runner_unavailable", exit_code=None, stdout="", stderr=str(exc), duration_ms=0)
        finally:
            if "process" in locals():
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
        for process in tuple(self._processes):
            process.kill()
        self._processes.clear()

    def build_mcp_process(self, **kwargs: Any) -> Any:
        return SimpleNamespace(command=kwargs.get("command"), args=list(kwargs.get("args", ())), env=dict(kwargs.get("env") or {}))


__all__ = ["SubprocessSandbox"]
