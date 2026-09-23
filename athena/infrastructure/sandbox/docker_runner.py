"""Lightweight Docker-backed runner.

The runner owns only containers created by Athena. User/model input is passed as
container arguments and never interpolated into a host shell command.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from athena.config.settings import Settings
from athena.core.sandbox.models import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    MCPProcessSpec,
    SandboxRunHandle,
    SandboxRunRequest,
)
from athena.core.sandbox.ports import SandboxRunner
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class DockerRunnerError(RuntimeError):
    pass


class DockerRunner(SandboxRunner):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._binary = settings.sandbox_docker_binary
        self._runs: dict[str, SandboxRunHandle] = {}
        self._mcp_specs: dict[str, MCPProcessSpec] = {}

    async def health(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=self._docker_env(),
            )
            await proc.communicate()
            return proc.returncode == 0
        except (OSError, asyncio.CancelledError):
            return False

    async def ensure_run(self, request: SandboxRunRequest) -> SandboxRunHandle:
        image = self.resolve_image(request.image)
        existing = self._runs.get(request.run_id)
        if existing is not None:
            return existing
        name = self._name("run", request.run_id)
        args = self._base_args(
            name=name,
            workspace=request.workspace,
            network_policy=request.network_policy,
        )
        args.extend([image, "sleep", "infinity"])
        container_id = await self._check_output("create", *args)
        container_id = container_id.strip().splitlines()[-1]
        try:
            await self._check_output("start", container_id)
        except Exception:
            await self._best_effort_remove(container_id)
            raise
        handle = SandboxRunHandle(
            run_id=request.run_id,
            container_id=container_id,
            workspace=request.workspace,
            image=image,
            network_policy=request.network_policy,
        )
        self._runs[request.run_id] = handle
        return handle

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        handle = self._runs.get(request.run_id)
        if handle is None:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                exit_code=None,
                stdout="",
                stderr="run container is not initialized",
                duration_ms=0,
                error_code="sandbox_run_missing",
            )
        args = [
            "exec",
            "--workdir",
            request.cwd,
        ]
        for key, value in request.env.items():
            if "=" in key or "\x00" in key or "\x00" in value:
                return self._result(
                    ExecutionStatus.FAILED,
                    started,
                    error="invalid_execution_environment",
                )
            args.extend(["--env", f"{key}={value}"])
        args.extend([handle.container_id, *request.argv])
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._docker_env(),
            )
        except OSError as exc:
            return self._result(
                ExecutionStatus.RUNNER_UNAVAILABLE, started, error=str(exc)
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=request.timeout_seconds
            )
        except asyncio.TimeoutError:
            await self._best_effort_remove(handle.container_id)
            self._runs.pop(request.run_id, None)
            return self._result(
                ExecutionStatus.TIMEOUT, started, error="sandbox_timeout"
            )
        except asyncio.CancelledError:
            await self._best_effort_remove(handle.container_id)
            self._runs.pop(request.run_id, None)
            raise

        output_truncated = len(stdout) + len(stderr) > request.max_output_bytes
        remaining = request.max_output_bytes
        stdout = stdout[:remaining]
        remaining = max(0, remaining - len(stdout))
        stderr = stderr[:remaining]
        status = (
            ExecutionStatus.SUCCESS if proc.returncode == 0 else ExecutionStatus.FAILED
        )
        if proc.returncode in {137, 143} and not stdout and not stderr:
            status = ExecutionStatus.OOM if proc.returncode == 137 else status
        return self._result(
            status,
            started,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            output_truncated=output_truncated,
        )

    def build_mcp_process(
        self,
        *,
        server_name: str,
        image: str,
        command: list[str],
        args: list[str],
        workspace: Path,
        env: dict[str, str] | None = None,
        network_policy: str = "none",
    ) -> MCPProcessSpec:
        image = self.resolve_image(image)
        name = self._name("mcp", server_name)
        docker_args = [
            "run",
            "--rm",
            "-i",
            *self._base_args(
                name=name, workspace=workspace, network_policy=network_policy
            ),
        ]
        for key, value in (env or {}).items():
            if "=" in key or "\x00" in key or "\x00" in value:
                raise DockerRunnerError("invalid MCP environment variable")
            docker_args.extend(["--env", f"{key}={value}"])
        docker_args.extend([image, *command, *args])
        spec = MCPProcessSpec(
            command=self._binary,
            args=docker_args,
            env=self._docker_env(),
            container_name=name,
        )
        self._mcp_specs[server_name] = spec
        return spec

    async def close_run(self, run_id: str) -> None:
        handle = self._runs.pop(run_id, None)
        if handle is not None:
            await self._best_effort_remove(handle.container_id)

    async def close_all(self) -> None:
        for run_id in list(self._runs):
            await self.close_run(run_id)
        self._mcp_specs.clear()

    def validate_image(self, image: str) -> None:
        if not image or any(ch.isspace() for ch in image) or "\x00" in image:
            raise DockerRunnerError("invalid sandbox image")
        if self._settings.sandbox_require_digest and "@sha256:" not in image:
            raise DockerRunnerError("sandbox image must use a sha256 digest")
        allowlist = self._settings.sandbox_image_allowlist
        if allowlist and image not in allowlist:
            raise DockerRunnerError("sandbox image is not allowlisted")

    def resolve_image(self, image_id_or_image: str) -> str:
        image = self._settings.sandbox_mcp_images.get(
            image_id_or_image, image_id_or_image
        )
        self.validate_image(image)
        return image

    def resolve_mcp_image(self, image_id: str) -> str:
        image = self._settings.sandbox_mcp_images.get(image_id)
        if not image:
            raise DockerRunnerError("sandbox MCP image is not registered")
        return self.resolve_image(image)

    async def _check_output(self, *args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._docker_env(),
            )
            stdout, stderr = await proc.communicate()
        except OSError as exc:
            raise DockerRunnerError("sandbox_unavailable") from exc
        if proc.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise DockerRunnerError(message or "docker command failed")
        return stdout.decode("utf-8", errors="replace")

    async def _best_effort_remove(self, container_id: str) -> None:
        try:
            await self._check_output("rm", "-f", container_id)
        except Exception as exc:
            logger.warning("sandbox_container_cleanup_failed", error=str(exc))

    def _base_args(
        self, *, name: str, workspace: Path, network_policy: str
    ) -> list[str]:
        if network_policy != "none":
            raise DockerRunnerError(f"unsupported network policy: {network_policy}")
        args = [
            "--name",
            name,
            "--label",
            "com.athena.owner=true",
            "--label",
            f"com.athena.container={name}",
            "--init",
            "--read-only",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "1000:1000",
            "--workdir",
            "/workspace",
            "--pids-limit",
            str(self._settings.sandbox_pids_limit),
            "--cpus",
            str(self._settings.sandbox_cpu_limit),
            "--memory",
            f"{self._settings.sandbox_memory_mb}m",
            "--memory-swap",
            f"{self._settings.sandbox_memory_mb}m",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            f"type=bind,source={workspace.resolve()},target=/workspace",
        ]
        return args

    def _docker_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", "")}
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT"):
            if value := os.environ.get(key):
                env[key] = value
        for key in ("HOME", "DOCKER_CONFIG"):
            if value := os.environ.get(key):
                env[key] = value
        if extra:
            env.update(extra)
        return env

    @staticmethod
    def _name(prefix: str, value: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)
        return f"athena-{prefix}-{safe[:48]}"

    @staticmethod
    def _result(
        status: ExecutionStatus,
        started: float,
        *,
        exit_code: int | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
        output_truncated: bool = False,
        error: str | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            status=status,
            exit_code=exit_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            duration_ms=(time.monotonic() - started) * 1000,
            output_truncated=output_truncated,
            error_code=error,
        )
