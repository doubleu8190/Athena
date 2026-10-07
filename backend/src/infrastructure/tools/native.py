"""Target-native built-in tool execution."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from domain.tools import ToolExecutionPort, ToolExecutionRequest, ToolExecutionResult


class NativeToolExecution(ToolExecutionPort):
    def __init__(self, workspace_root: Path, *, max_output_bytes: int = 1_000_000) -> None:
        self._root = workspace_root.resolve()
        self._max_output_bytes = max_output_bytes
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
                output = self._path(str(args["path"])).read_text(encoding=str(args.get("encoding", "utf-8")))
            elif request.tool_name in {"write_file", "write_file_content"}:
                path = self._path(str(args["path"])); path.parent.mkdir(parents=True, exist_ok=True)
                mode = "a" if args.get("append") else "w"
                path.open(mode, encoding=str(args.get("encoding", "utf-8"))).write(str(args.get("content", "")))
                output = f"wrote {path.relative_to(self._root)}"
            elif request.tool_name in {"list_directory", "list_files"}:
                output = "\n".join(sorted(item.name for item in self._path(str(args.get("path", "."))).iterdir() if args.get("include_hidden") or not item.name.startswith(".")))
            elif request.tool_name in {"exec_shell", "shell"}:
                process = await asyncio.create_subprocess_exec("/bin/sh", "-lc", str(args["command"]), cwd=self._root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=float(args.get("timeout", 60)))
                output = (stdout + stderr).decode("utf-8", errors="replace")
                if process.returncode:
                    return ToolExecutionResult("failed", output=output[:self._max_output_bytes], error=f"exit_code={process.returncode}", duration_ms=(time.monotonic()-started)*1000)
            else:
                return ToolExecutionResult("unavailable", error=f"unknown tool: {request.tool_name}")
            return ToolExecutionResult("success", output=output[:self._max_output_bytes], duration_ms=(time.monotonic()-started)*1000)
        except Exception as exc:
            return ToolExecutionResult("failed", error=str(exc), duration_ms=(time.monotonic()-started)*1000)


__all__ = ["NativeToolExecution"]
