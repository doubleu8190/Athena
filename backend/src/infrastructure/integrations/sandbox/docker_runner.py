"""Docker-backed target sandbox with a subprocess fallback contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


class DockerSandbox:
    def __init__(self, settings) -> None:
        self.settings = settings
        self._processes: set[asyncio.subprocess.Process] = set()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self.close_all()

    async def execute(self, request):
        argv = [self.settings.sandbox_docker_binary, "run", "--rm", "--network", self.settings.sandbox_default_network]
        argv.extend(["--memory", f"{self.settings.sandbox_memory_mb}m", "--cpus", str(self.settings.sandbox_cpu_limit)])
        argv.extend(["-v", f"{request.workspace}:/workspace", "-w", "/workspace", request.image])
        argv.extend(request.argv)
        try:
            process = await asyncio.create_subprocess_exec(argv[0], *argv[1:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            self._processes.add(process)
            stdout, stderr = await asyncio.wait_for(process.communicate(), request.timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return SimpleNamespace(status="timeout", exit_code=None, stdout="", stderr="sandbox_timeout")
        except OSError as exc:
            return SimpleNamespace(status="runner_unavailable", exit_code=None, stdout="", stderr=str(exc))
        finally:
            if "process" in locals():
                self._processes.discard(process)
        return SimpleNamespace(
            status="success" if process.returncode == 0 else "failed",
            exit_code=process.returncode,
            stdout=stdout[: self.settings.sandbox_max_output_bytes].decode("utf-8", errors="replace"),
            stderr=stderr[: self.settings.sandbox_max_output_bytes].decode("utf-8", errors="replace"),
        )

    async def health(self) -> bool:
        process = await asyncio.create_subprocess_exec(self.settings.sandbox_docker_binary, "version", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await process.wait() == 0

    async def close_all(self) -> None:
        for process in tuple(self._processes):
            process.kill()
        self._processes.clear()

    def build_mcp_process(self, **kwargs):
        return SimpleNamespace(command=kwargs.get("command"), args=list(kwargs.get("args", ())), env=dict(kwargs.get("env") or {}))


__all__ = ["DockerSandbox"]
