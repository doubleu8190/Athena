"""Built-in filesystem tools — file_read, file_write, file_delete, file_search.

All paths must be absolute.  Path access control is enforced by the Harness
Engine's path_permission rules (see ``_check_path_boundary``).

Each tool supports idempotency keys for safe retry.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athena.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class FileReadResult:
    """Result of a file read operation."""
    success: bool
    content: str | None = None
    size_bytes: int = 0
    path: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        if not self.success:
            return {"success": False, "error": self.error, "content": None}
        return {
            "success": True,
            "content": self.content,
            "size_bytes": self.size_bytes,
            "path": self.path,
        }


@dataclass
class FileWriteResult:
    """Result of a file write operation."""
    success: bool
    path: str = ""
    size_bytes: int = 0
    overwrote: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        if not self.success:
            return {"success": False, "error": self.error}
        return {
            "success": True,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "overwrote": self.overwrote,
        }


@dataclass
class FileDeleteResult:
    """Result of a file delete operation."""
    success: bool
    path: str = ""
    was_directory: bool = False
    size_bytes: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        if not self.success:
            return {"success": False, "error": self.error}
        return {
            "success": True,
            "path": self.path,
            "was_directory": self.was_directory,
            "size_bytes": self.size_bytes,
        }


@dataclass
class FileSearchResult:
    """Result of a file search operation."""
    success: bool
    matches: list[str] = field(default_factory=list)
    count: int = 0
    search_root: str = ""
    pattern: str = ""
    match_type: str = ""
    truncated: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        if not self.success:
            return {"success": False, "error": self.error, "matches": []}
        return {
            "success": True,
            "matches": self.matches,
            "count": self.count,
            "search_root": self.search_root,
            "pattern": self.pattern,
            "match_type": self.match_type,
            "truncated": self.truncated,
        }

def _safe_path(path: str) -> Path:
    """Resolve a path and verify it's absolute.

    Raises ValueError if the path is not absolute.
    """
    p = Path(os.path.normpath(path))
    if not p.is_absolute():
        raise ValueError(f"Path must be absolute, got: {path}")
    # Resolve symlinks and normalize
    return p.resolve()


async def file_read(path: str, **kwargs: Any) -> FileReadResult:
    """Read a file from the workspace.

    Risk level: low (read-only).
    """
    try:
        target = _safe_path(path)
        if not target.exists():
            return FileReadResult(success=False, error=f"File not found: {path}")

        content = target.read_text(encoding="utf-8")
        return FileReadResult(
            success=True,
            content=content,
            size_bytes=len(content),
            path=str(target),
        )
    except ValueError as e:
        return FileReadResult(success=False, error=str(e))
    except Exception as e:
        logger.error("file_read_error", path=path, error=str(e))
        return FileReadResult(success=False, error=str(e))


async def file_write(
    path: str,
    content: str,
    idempotency_key: str | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> FileWriteResult:
    """Write content to a file in the workspace.

    Risk level: medium.
    Supports idempotency keys.
    """
    try:
        target = _safe_path(path)

        # Create parent directories if needed
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write content
        target.write_text(content, encoding="utf-8")

        return FileWriteResult(
            success=True,
            path=str(target),
            size_bytes=len(content),
            overwrote=target.exists(),
        )
    except ValueError as e:
        return FileWriteResult(success=False, error=str(e))
    except Exception as e:
        logger.error("file_write_error", path=path, error=str(e))
        return FileWriteResult(success=False, error=str(e))


async def file_delete(
    path: str,
    idempotency_key: str | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> FileDeleteResult:
    """Delete a file from the workspace.

    Risk level: high.
    """
    try:
        target = _safe_path(path)

        if not target.exists():
            return FileDeleteResult(success=False, error=f"File not found: {path}")

        stat = target.stat()

        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

        return FileDeleteResult(
            success=True,
            path=str(target),
            was_directory=target.is_dir() if target.exists() else False,
            size_bytes=stat.st_size,
        )
    except ValueError as e:
        return FileDeleteResult(success=False, error=str(e))
    except Exception as e:
        logger.error("file_delete_error", path=path, error=str(e))
        return FileDeleteResult(success=False, error=str(e))


async def file_search(
    pattern: str,
    path: str = "/",
    recursive: bool = True,
    match_type: str = "name",
    max_results: int = 50,
    **kwargs: Any,  # noqa: ANN401
) -> FileSearchResult:
    """Search for files under an absolute directory path.

    Args:
        pattern: Search pattern. For name searches, a glob pattern (e.g. ``*.py``, ``test_*``).
                 For content searches, a substring or regex to match inside files.
        path: Absolute base directory to search from.
        recursive: If True, search subdirectories recursively.
        match_type: ``"name"`` for filename glob matching, ``"content"`` for searching
                    inside file contents.
        max_results: Maximum number of results to return.

    Risk level: low (read-only).

    Returns:
        dict with ``matches`` (list of absolute file paths) and ``count``.
    """
    try:
        target = _safe_path(path)
        if not target.exists():
            return FileSearchResult(success=False, error=f"Path not found: {path}")
        if not target.is_dir():
            return FileSearchResult(success=False, error=f"Path is not a directory: {path}")

        matches: list[str] = []

        if match_type == "name":
            # Glob-based filename matching
            iterator = target.rglob(pattern) if recursive else target.glob(pattern)
            for p in iterator:
                if p.is_file():
                    matches.append(str(p))
                    if len(matches) >= max_results:
                        break

        elif match_type == "content":
            # Content search: walk files and grep inside them
            if pattern.startswith("/") and pattern.endswith("/"):
                # Regex mode: pattern like "/error|warn/i"
                import re
                flags = re.IGNORECASE if pattern.endswith("/i") else 0
                regex_str = pattern.strip("/").rstrip("/i")
                try:
                    compiled = re.compile(regex_str, flags)
                except re.error as e:
                    return FileSearchResult(success=False, error=f"Invalid regex: {e}")

                def content_match(file_path: Path) -> bool:
                    try:
                        return bool(compiled.search(file_path.read_text(encoding="utf-8")))
                    except Exception:
                        return False
            else:
                # Plain substring match
                def content_match(file_path: Path) -> bool:
                    try:
                        return pattern in file_path.read_text(encoding="utf-8")
                    except Exception:
                        return False

            iterator = target.rglob("*") if recursive else target.glob("*")
            for p in iterator:
                if p.is_file() and content_match(p):
                    matches.append(str(p))
                    if len(matches) >= max_results:
                        break

        else:
            return FileSearchResult(success=False, error=f"Unknown match_type: {match_type}")

        return FileSearchResult(
            success=True,
            matches=matches,
            count=len(matches),
            search_root=str(target),
            pattern=pattern,
            match_type=match_type,
            truncated=len(matches) >= max_results,
        )

    except ValueError as e:
        return FileSearchResult(success=False, error=str(e))
    except Exception as e:
        logger.error("file_search_error", pattern=pattern, path=path, error=str(e))
        return FileSearchResult(success=False, error=str(e))
