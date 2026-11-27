"""纯 stdlib 相对 POSIX 路径安全校验与符号链接穿越防御。"""

from __future__ import annotations

import stat
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath


def safe_relative_posix_parts(
    path: str,
    *,
    allow_trailing_slash: bool = False,
) -> tuple[str, ...]:
    """校验资源路径为纯相对 POSIX 路径。

    拒绝空串、空字节、控制字符、绝对路径、反斜杠、Windows 盘符及 ``//``/``.``/``..`` 段；
    当 ``allow_trailing_slash=True`` 时仅允许剥离单个尾部斜杠（用于 ZIP 目录项）。
    """
    if not isinstance(path, str) or not path:
        raise ValueError("safe_path_invalid")
    normalized = path[:-1] if allow_trailing_slash and path.endswith("/") else path
    if (
        not normalized
        or "\x00" in normalized
        or "\\" in normalized
        or normalized.startswith("/")
        or (len(normalized) >= 2 and normalized[1] == ":")
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized)
    ):
        raise ValueError("safe_path_invalid")
    parts = tuple(normalized.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("safe_path_invalid")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or posix.as_posix() != normalized:
        raise ValueError("safe_path_invalid")
    return parts


def reject_existing_symlink_components(
    base_root: Path,
    parts: Sequence[str],
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> Path:
    """逐级检查已存在路径段禁止为符号链接，返回拼接后的目标路径。"""
    current = base_root
    for part in parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            # 尚未创建的中间目录或目标文件允许放行，后续写入仍受祖先非 symlink 与父目录解析约束。
            continue
        except OSError as error:
            raise error_factory("safe_path_stat_failed") from error
        if stat.S_ISLNK(current_stat.st_mode):
            raise error_factory("safe_path_symlink_not_allowed")
    return current


def resolve_safe_child_path(base_root: Path, relative_path: str) -> Path:
    """解析相对路径并校验目标仍在 ``base_root`` 子树内且沿途已存在段均非符号链接。"""
    parts = safe_relative_posix_parts(relative_path)
    resolved_root = base_root.resolve(strict=True)
    candidate = reject_existing_symlink_components(resolved_root, parts)
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate == resolved_root or not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError("safe_path_outside_root")
    return candidate


__all__ = [
    "reject_existing_symlink_components",
    "resolve_safe_child_path",
    "safe_relative_posix_parts",
]
