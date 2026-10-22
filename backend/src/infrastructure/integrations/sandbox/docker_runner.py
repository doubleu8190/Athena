"""Docker-backed target sandbox with a subprocess fallback contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from domain.integrations import SandboxPort
from infrastructure.process_control import (
    isolated_process_options,
    terminate_process_group,
)
from .options import SandboxOptions


class DockerSandbox(SandboxPort):
    def __init__(self, options: SandboxOptions | object) -> None:
        self.options = options if isinstance(options, SandboxOptions) else SandboxOptions(
            docker_binary=options.sandbox_docker_binary,
            default_network=options.sandbox_default_network,
            max_output_bytes=options.sandbox_max_output_bytes,
            memory_mb=options.sandbox_memory_mb,
            cpu_limit=options.sandbox_cpu_limit,
            pids_limit=getattr(options, "sandbox_pids_limit", 128),
        )
        self._processes: set[asyncio.subprocess.Process] = set()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self.close_all()

    async def execute(self, request):
        argv = [self.options.docker_binary, "run", "--rm", "--network", self.options.default_network]
        argv.extend([
            "--memory", f"{self.options.memory_mb}m",
            "--cpus", str(self.options.cpu_limit),
            "--pids-limit", str(self.options.pids_limit),
        ])
        argv.extend(["-v", f"{request.workspace}:/workspace", "-w", "/workspace", request.image])
        argv.extend(request.argv)
        process = None
        communicate_task = None
        started = asyncio.get_running_loop().time()
        try:
            process = await asyncio.create_subprocess_exec(
                argv[0],
                *argv[1:],
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
                    stdout=stdout[: self.options.max_output_bytes].decode(
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
            return SimpleNamespace(status="runner_unavailable", exit_code=None, stdout="", stderr=str(exc))
        finally:
            if process is not None:
                self._processes.discard(process)
        return SimpleNamespace(
            status="success" if process.returncode == 0 else "failed",
            exit_code=process.returncode,
            stdout=stdout[: self.options.max_output_bytes].decode("utf-8", errors="replace"),
            stderr=stderr[: self.options.max_output_bytes].decode("utf-8", errors="replace"),
        )

    async def health(self) -> bool:
        process = await asyncio.create_subprocess_exec(self.options.docker_binary, "version", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await process.wait() == 0

    async def close_all(self) -> None:
        await asyncio.gather(
            *(
                terminate_process_group(process)
                for process in tuple(self._processes)
            ),
            return_exceptions=True,
        )
        self._processes.clear()

    def build_mcp_process(self, **kwargs):
        return SimpleNamespace(command=kwargs.get("command"), args=list(kwargs.get("args", ())), env=dict(kwargs.get("env") or {}))


__all__ = ["DockerSandbox"]
