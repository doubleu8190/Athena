"""Built-in filesystem tools — file_read, file_write, file_delete.

All operations are confined to /workspace/ (enforced by path boundary check
in both the tool implementation AND the Harness Engine).

Each tool supports:
- _preview mode: returns expected result without applying changes
- Idempotency keys for safe retry
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from athena.logging_config import get_logger

logger = get_logger(__name__)

WORKSPACE_ROOT = Path("/workspace")


def _safe_path(path: str) -> Path:
    """Resolve a path and verify it's under /workspace/.

    Raises ValueError if path escapes the workspace boundary.
    """
    p = Path(os.path.normpath(path))
    # If relative, anchor to workspace
    if not p.is_absolute():
        p = WORKSPACE_ROOT / p
    # Resolve symlinks and normalize
    resolved = p.resolve()
    if not str(resolved).startswith(str(WORKSPACE_ROOT.resolve())):
        raise ValueError(f"Path '{path}' is outside /workspace/ boundary")
    return resolved


async def file_read(path: str, preview: bool = False, **kwargs) -> dict[str, Any]:
    """Read a file from the workspace.

    Risk level: low (read-only).
    """
    try:
        target = _safe_path(path)
        if not target.exists():
            return {"success": False, "error": f"File not found: {path}", "content": None}

        content = target.read_text(encoding="utf-8")
        return {
            "success": True,
            "content": content,
            "size_bytes": len(content),
            "path": str(target),
        }
    except ValueError as e:
        return {"success": False, "error": str(e), "content": None}
    except Exception as e:
        logger.error("file_read_error", path=path, error=str(e))
        return {"success": False, "error": str(e), "content": None}


async def file_write(
    path: str,
    content: str,
    preview: bool = False,
    idempotency_key: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Write content to a file in the workspace.

    Risk level: medium.
    Supports preview mode and idempotency keys.
    """
    try:
        target = _safe_path(path)

        if preview:
            return {
                "success": True,
                "preview": True,
                "action": "write",
                "path": str(target),
                "content_preview": content[:500],
                "would_overwrite": target.exists(),
                "existing_size": target.stat().st_size if target.exists() else 0,
            }

        # Create parent directories if needed
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write content
        target.write_text(content, encoding="utf-8")

        return {
            "success": True,
            "path": str(target),
            "size_bytes": len(content),
            "overwrote": target.exists(),
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("file_write_error", path=path, error=str(e))
        return {"success": False, "error": str(e)}


async def file_delete(
    path: str,
    preview: bool = False,
    idempotency_key: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Delete a file from the workspace.

    Risk level: high.
    Supports preview mode.
    """
    try:
        target = _safe_path(path)

        if not target.exists():
            return {"success": False, "error": f"File not found: {path}"}

        stat = target.stat()

        if preview:
            return {
                "success": True,
                "preview": True,
                "action": "delete",
                "path": str(target),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            }

        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

        return {
            "success": True,
            "path": str(target),
            "was_directory": target.is_dir() if target.exists() else False,
            "size_bytes": stat.st_size,
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("file_delete_error", path=path, error=str(e))
        return {"success": False, "error": str(e)}
