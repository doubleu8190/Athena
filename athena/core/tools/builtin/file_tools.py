"""内置文件工具 — read_file / write_file / list_directory.

轻量级本地文件操作，使用 Native 模式直接执行。
集成 PathSecurityFilter 防止路径遍历和注入攻击。
"""

from __future__ import annotations

from pathlib import Path

from athena.core.security.path_filter import PathSecurityError, get_path_security_filter
from athena.utils.logging import get_logger

logger = get_logger(__name__)


def _safe_resolve_path(path: str, *, for_write: bool = False) -> Path:
    """使用 PathSecurityFilter 验证并解析路径.

    Args:
        path: 原始路径字符串
        for_write: 是否为写操作（启用写保护检查）

    Returns:
        解析后的安全 Path 对象

    Raises:
        PathSecurityError: 路径不安全
    """
    filter = get_path_security_filter()
    if for_write:
        return filter.validate_for_write(path)
    return filter.validate(path)


async def read_file(path: str, encoding: str = "utf-8") -> str:
    """读取文件内容.

    Args:
        path: 文件路径（经 PathSecurityFilter 验证）
        encoding: 文件编码，默认 utf-8

    Returns:
        文件文本内容
    """
    try:
        file_path = _safe_resolve_path(path)
    except PathSecurityError as e:
        logger.warning("file_tool_path_blocked", path=path, reason=e.reason)
        raise PermissionError(f"路径安全检查失败: {e.reason}") from e

    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if not file_path.is_file():
        raise IsADirectoryError(f"路径不是文件: {path}")
    return file_path.read_text(encoding=encoding)


async def write_file(path: str, content: str, encoding: str = "utf-8", append: bool = False) -> str:
    """写入文件内容.

    Args:
        path: 文件路径（经 PathSecurityFilter 验证）
        content: 要写入的内容
        encoding: 文件编码，默认 utf-8
        append: 是否追加写入，默认 False（覆盖）

    Returns:
        操作结果描述
    """
    try:
        file_path = _safe_resolve_path(path, for_write=True)
    except PathSecurityError as e:
        logger.warning("file_tool_path_blocked", path=path, reason=e.reason)
        raise PermissionError(f"路径安全检查失败: {e.reason}") from e

    file_path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with file_path.open("a", encoding=encoding) as f:
            f.write(content)
    else:
        file_path.write_text(content, encoding=encoding)
    size = file_path.stat().st_size
    return f"已{'追加' if append else '写入'} {path} ({size} bytes)"


async def list_directory(path: str = ".", include_hidden: bool = False) -> str:
    """列出目录内容.

    Args:
        path: 目录路径（经 PathSecurityFilter 验证），默认当前目录
        include_hidden: 是否包含隐藏文件（以 . 开头），默认 False

    Returns:
        目录条目列表（文件/目录标识 + 名称）
    """
    try:
        dir_path = _safe_resolve_path(path)
    except PathSecurityError as e:
        logger.warning("file_tool_path_blocked", path=path, reason=e.reason)
        raise PermissionError(f"路径安全检查失败: {e.reason}") from e

    if not dir_path.exists():
        raise FileNotFoundError(f"目录不存在: {path}")
    if not dir_path.is_dir():
        raise NotADirectoryError(f"路径不是目录: {path}")

    entries: list[str] = []
    for entry in sorted(dir_path.iterdir()):
        name = entry.name
        if not include_hidden and name.startswith("."):
            continue
        kind = "DIR " if entry.is_dir() else "FILE"
        size = entry.stat().st_size if entry.is_file() else 0
        entries.append(f"{kind} {name} ({size} bytes)" if entry.is_file() else f"{kind} {name}")

    return "\n".join(entries) if entries else "(空目录)"
