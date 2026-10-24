"""Target-native built-in tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

from domain.tools import ToolExecutionPort, ToolExecutionRequest, ToolExecutionResult
from domain.integrations import SandboxPort
from infrastructure.process_control import (
    isolated_process_options,
    terminate_process_group,
)


class BuiltinToolExecutor(ToolExecutionPort):
    def __init__(
        self,
        workspace_root: Path,
        *,
        max_output_bytes: int = 1_000_000,
        sandbox: SandboxPort | None = None,
        sandbox_enabled: bool = False,
        sandbox_required: bool = False,
        sandbox_image: str = "python:3.12-slim",
        sandbox_max_timeout: int = 60,
        sandbox_workspace_root: Path | None = None,
    ) -> None:
        self._root = workspace_root.resolve()
        self._max_output_bytes = max_output_bytes
        self._sandbox = sandbox
        self._sandbox_enabled = sandbox_enabled or sandbox_required
        self._sandbox_required = sandbox_required
        self._sandbox_image = sandbox_image
        self._sandbox_max_timeout = sandbox_max_timeout
        self._sandbox_workspace_root = (
            sandbox_workspace_root.resolve()
            if sandbox_workspace_root is not None
            else self._root / "sandboxes"
        )
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, raw: str) -> Path:
        path = (self._root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        if path != self._root and self._root not in path.parents:
            raise PermissionError("path outside workspace")
        return path

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        started = time.monotonic()
        try:
            args = request.arguments
            if request.tool_name in {"read_local_file", "read_file"}:
                path = self._path(str(args["path"]))
                output = await asyncio.to_thread(
                    path.read_text, encoding=str(args.get("encoding", "utf-8"))
                )
            elif request.tool_name in {"write_file", "write_file_content"}:
                path = self._path(str(args["path"]))
                await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
                mode = "a" if args.get("append") else "w"
                content = str(args.get("content", ""))
                encoding = str(args.get("encoding", "utf-8"))
                await asyncio.to_thread(_write_text, path, mode, encoding, content)
                output = f"wrote {path.relative_to(self._root)}"
            elif request.tool_name in {"list_directory", "list_files"}:
                path = self._path(str(args.get("path", ".")))
                include_hidden = bool(args.get("include_hidden"))
                entries = await asyncio.to_thread(lambda: tuple(path.iterdir()))
                output = "\n".join(
                    sorted(item.name for item in entries
                           if include_hidden or not item.name.startswith("."))
                )
            elif request.tool_name in {"exec_shell", "shell"}:
                timeout = min(float(args.get("timeout", 60)), float(self._sandbox_max_timeout))
                command = str(args["command"])
                if self._sandbox_enabled:
                    if self._sandbox is None:
                        return ToolExecutionResult("sandbox_unavailable", error="sandbox_not_configured", duration_ms=(time.monotonic()-started)*1000)
                    workspace_key = hashlib.sha256(
                        f"{request.session_id}:{request.run_id}".encode("utf-8")
                    ).hexdigest()[:32]
                    workspace = self._sandbox_workspace_root / workspace_key
                    workspace.mkdir(parents=True, exist_ok=True)
                    try:
                        result = await self._sandbox.execute(SimpleNamespace(
                            workspace=workspace,
                            image=self._sandbox_image,
                            argv=("/bin/sh", "-lc", command),
                            timeout_seconds=timeout,
                        ))
                    except Exception as exc:
                        return ToolExecutionResult("sandbox_unavailable", error=str(exc), duration_ms=(time.monotonic()-started)*1000)
                    status = str(getattr(result, "status", "runner_unavailable"))
                    output = str(getattr(result, "stdout", "") or "")
                    stderr = str(getattr(result, "stderr", "") or "")
                    combined = output + stderr
                    if status == "success":
                        return ToolExecutionResult("success", output=combined[:self._max_output_bytes], duration_ms=(time.monotonic()-started)*1000)
                    if status == "timeout":
                        return ToolExecutionResult("timeout", output=combined[:self._max_output_bytes], error="sandbox_timeout", duration_ms=(time.monotonic()-started)*1000)
                    if status in {"runner_unavailable", "sandbox_unavailable"}:
                        return ToolExecutionResult("sandbox_unavailable", output=combined[:self._max_output_bytes], error=str(getattr(result, "stderr", "") or "sandbox_unavailable"), duration_ms=(time.monotonic()-started)*1000)
                    return ToolExecutionResult("failed", output=combined[:self._max_output_bytes], error=f"exit_code={getattr(result, 'exit_code', None)}", duration_ms=(time.monotonic()-started)*1000)
                process = await asyncio.create_subprocess_exec(
                    "/bin/sh",
                    "-lc",
                    command,
                    cwd=self._root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **isolated_process_options(),
                )
                communicate_task = asyncio.create_task(process.communicate())
                try:
                    done, _pending = await asyncio.wait(
                        {communicate_task}, timeout=timeout
                    )
                    if not done:
                        await terminate_process_group(process)
                        stdout, stderr = await communicate_task
                        return ToolExecutionResult(
                            "timeout",
                            output=(stdout + stderr).decode(
                                "utf-8", errors="replace"
                            )[: self._max_output_bytes],
                            error="tool_execution_timeout",
                            duration_ms=(time.monotonic() - started) * 1000,
                        )
                    stdout, stderr = communicate_task.result()
                except asyncio.CancelledError:
                    await terminate_process_group(process)
                    await asyncio.gather(
                        communicate_task, return_exceptions=True
                    )
                    raise
                output = (stdout + stderr).decode("utf-8", errors="replace")
                if process.returncode:
                    return ToolExecutionResult("failed", output=output[:self._max_output_bytes], error=f"exit_code={process.returncode}", duration_ms=(time.monotonic()-started)*1000)
            else:
                return ToolExecutionResult("unavailable", error=f"unknown tool: {request.tool_name}")
            return ToolExecutionResult("success", output=output[:self._max_output_bytes], duration_ms=(time.monotonic()-started)*1000)
        except Exception as exc:
            return ToolExecutionResult("failed", error=str(exc), duration_ms=(time.monotonic()-started)*1000)


NativeToolExecution = BuiltinToolExecutor


def _write_text(path: Path, mode: str, encoding: str, content: str) -> None:
    with path.open(mode, encoding=encoding) as stream:
        stream.write(content)

__all__ = ["BuiltinToolExecutor", "NativeToolExecution"]
