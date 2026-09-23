"""Per-run workspace management with traversal and symlink protection."""

from __future__ import annotations

import os
from pathlib import Path


class WorkspaceError(ValueError):
    """A requested path is outside the run workspace."""


class WorkspaceManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, session_id: str, run_id: str) -> Path:
        self._validate_id(session_id)
        self._validate_id(run_id)
        workspace = self.root / session_id / run_id / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def resolve_cwd(self, workspace: Path, cwd: str | None) -> str:
        workspace = workspace.resolve()
        relative = Path(cwd or ".")
        if relative.is_absolute():
            raise WorkspaceError("cwd must be relative to the run workspace")
        resolved = (workspace / relative).resolve()
        if not resolved.is_relative_to(workspace):
            raise WorkspaceError("cwd escapes the run workspace")
        return "/workspace" + (
            "/" + resolved.relative_to(workspace).as_posix()
            if resolved != workspace
            else ""
        )

    def resolve_host_path(self, workspace: Path, relative: str) -> Path:
        workspace = workspace.resolve()
        candidate = (workspace / Path(relative)).resolve()
        if not candidate.is_relative_to(workspace):
            raise WorkspaceError("path escapes the run workspace")
        return candidate

    def cleanup(self, session_id: str, run_id: str) -> None:
        workspace = self.root / session_id / run_id
        if workspace.exists() and workspace.is_dir():
            for path in sorted(workspace.rglob("*"), reverse=True):
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    path.rmdir()
            workspace.rmdir()

    @staticmethod
    def _validate_id(value: str) -> None:
        if not value or value in {".", ".."} or os.sep in value or "/" in value:
            raise WorkspaceError("workspace identifiers must be simple path names")
