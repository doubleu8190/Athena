"""内置文件工具 — read_file / write_file / list_directory / 文件分析工具.

轻量级本地文件操作，使用 Native 模式直接执行。
集成 PathSecurityFilter 防止路径遍历和注入攻击。

文件分析工具集（用于 Agent 侦察和精准读取）：
- get_file_info: 返回文件元数据（大小、编码、行数、文件类型）
- read_file_section: 读取指定文件从 start_line 开始的 limit 行
- search_in_file: 在文件中执行正则匹配，返回匹配行及其行号
- read_full_file: 仅当文件大小 < 50KB 时返回全量内容
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from athena.core.security.path_filter import PathSecurityError, get_path_security_filter
from athena.utils.logging import get_logger

logger = get_logger(__name__)

# 文件大小阈值（50KB）
_FILE_SIZE_THRESHOLD = 50 * 1024

# read_file_section 最大行数限制
_MAX_SECTION_LINES = 200


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


# ---------------------------------------------------------------------------
# 文件分析工具集 — 用于 Agent 侦察和精准读取
# ---------------------------------------------------------------------------


async def get_file_info(path: str) -> dict:
    """返回文件的元数据（大小、编码、行数、文件类型）.

    Agent 首先调用此工具判断文件大小，决定下一步策略（直接读还是分段搜）。

    Args:
        path: 文件路径（经 PathSecurityFilter 验证）

    Returns:
        包含文件元数据的字典：
        - size_kb: 文件大小（KB）
        - line_count: 文件行数
        - is_binary: 是否为二进制文件
        - file_type: 文件类型（扩展名）
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

    stat = file_path.stat()
    size_bytes = stat.st_size
    size_kb = round(size_bytes / 1024, 2)

    # 检测是否为二进制文件
    is_binary = _is_binary_file(file_path)

    # 计算行数（仅对文本文件有效）
    line_count = 0
    if not is_binary:
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as f:
                line_count = sum(1 for _ in f)
        except Exception:
            line_count = -1

    # 获取文件类型
    file_type = file_path.suffix.lower() or "unknown"

    return {
        "size_kb": size_kb,
        "line_count": line_count,
        "is_binary": is_binary,
        "file_type": file_type,
    }


def _is_binary_file(file_path: Path, sample_size: int = 8192) -> bool:
    """检测文件是否为二进制文件.

    通过读取文件前 sample_size 字节检查是否包含空字节。
    """
    try:
        with file_path.open("rb") as f:
            chunk = f.read(sample_size)
            return b"\x00" in chunk
    except Exception:
        return True


async def read_file_section(
    path: str, start_line: int, limit: int = 100
) -> str:
    """读取指定文件从 start_line 开始的 limit 行.

    当 Agent 知道要查具体哪个函数或报错行时，窄读局部代码。

    Args:
        path: 文件路径（经 PathSecurityFilter 验证）
        start_line: 起始行号（从 1 开始）
        limit: 读取行数（最大 200）

    Returns:
        指定范围的文件内容

    Raises:
        ValueError: 参数无效
    """
    if start_line < 1:
        raise ValueError("start_line 必须大于 0")
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    if limit > _MAX_SECTION_LINES:
        raise ValueError(f"limit 不能超过 {_MAX_SECTION_LINES}")

    try:
        file_path = _safe_resolve_path(path)
    except PathSecurityError as e:
        logger.warning("file_tool_path_blocked", path=path, reason=e.reason)
        raise PermissionError(f"路径安全检查失败: {e.reason}") from e

    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if not file_path.is_file():
        raise IsADirectoryError(f"路径不是文件: {path}")

    # 检测二进制文件
    if _is_binary_file(file_path):
        raise ValueError("无法读取二进制文件的内容")

    lines: list[str] = []
    try:
        with file_path.open("r", encoding="utf-8", errors="ignore") as f:
            # 跳过 start_line 之前的行
            for i, line in enumerate(f, 1):
                if i < start_line:
                    continue
                if i >= start_line + limit:
                    break
                lines.append(line.rstrip("\n"))
    except Exception as e:
        raise IOError(f"读取文件失败: {e}") from e

    if not lines:
        return f"(文件在第 {start_line} 行后无内容)"

    # 添加行号前缀
    numbered_lines = [
        f"{start_line + i}: {line}" for i, line in enumerate(lines)
    ]
    return "\n".join(numbered_lines)


async def search_in_file(path: str, pattern: str) -> str:
    """在文件中执行正则匹配，返回匹配行及其行号.

    当 Agent 想找"哪里定义了某函数"或"哪里抛出某异常"时，先搜再读。

    Args:
        path: 文件路径（经 PathSecurityFilter 验证）
        pattern: 正则表达式模式

    Returns:
        JSON 格式的匹配结果列表：[{"line": 42, "content": "def login():"}, ...]
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

    # 检测二进制文件
    if _is_binary_file(file_path):
        raise ValueError("无法在二进制文件中搜索")

    # 编译正则表达式
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"无效的正则表达式: {e}") from e

    matches: list[dict] = []
    try:
        with file_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line_num, line in enumerate(f, 1):
                if regex.search(line):
                    matches.append({
                        "line": line_num,
                        "content": line.rstrip("\n"),
                    })
    except Exception as e:
        raise IOError(f"搜索文件失败: {e}") from e

    return json.dumps(matches, ensure_ascii=False)


async def read_full_file(path: str, encoding: str = "utf-8") -> str:
    """读取文件全量内容（带大小阀门）.

    仅当文件大小 < 50KB 时，才返回全量内容；
    否则返回错误提示，建议使用 search_in_file 定位后分段读取。

    处理小脚本时的快捷方式。

    Args:
        path: 文件路径（经 PathSecurityFilter 验证）
        encoding: 文件编码，默认 utf-8

    Returns:
        文件文本内容

    Raises:
        ValueError: 文件过大（> 50KB）
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

    # 检测二进制文件
    if _is_binary_file(file_path):
        raise ValueError("无法读取二进制文件的内容")

    # 大小阀门检查
    size_bytes = file_path.stat().st_size
    if size_bytes > _FILE_SIZE_THRESHOLD:
        size_kb = round(size_bytes / 1024, 2)
        raise ValueError(
            f"文件过大（{size_kb}KB > 50KB），请使用 search_in_file 定位后分段读取"
        )

    return file_path.read_text(encoding=encoding)
