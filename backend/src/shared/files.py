"""项目文件、系统临时文件和系统提示词的文本读写。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
import tempfile


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PROMPT_ROOT = _PROJECT_ROOT / "backend" / "prompt"
_PROMPT_PLACEHOLDER_PATTERN = re.compile(
    r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}"
)


def _resolve_file_path(directory: Path, file_path: str | Path) -> Path:
    """将相对文件路径解析到指定目录内，拒绝越界访问。

    Args:
        directory (Path): 文件所属目录。
        file_path (str | Path): 非空的相对文件路径，可包含子目录。
    Returns:
        Path: 解析符号链接后的绝对文件路径。
    Raises:
        ValueError: 路径为空、为绝对路径、指向所属目录或越出所属目录。
        OSError: 系统无法解析路径。
        RuntimeError: 路径存在符号链接循环。
    """
    relative_path = Path(file_path)
    if relative_path.is_absolute() or relative_path == Path("."):
        raise ValueError("文件路径必须是非空的相对路径")
    root = directory.resolve()
    path = (root / relative_path).resolve()
    # 同时检查解析后的符号链接，避免通过链接或 .. 访问目录外的文件。
    if path == root or not path.is_relative_to(root):
        raise ValueError("文件路径不能越出指定目录")
    return path


def read_project_file(file_path: str | Path, *, encoding: str = "utf-8") -> str:
    """读取 Athena 项目根目录下的文本文件，保留首尾空白。

    Args:
        file_path (str | Path): 相对于 Athena 根目录的文件路径，可包含子目录。
        encoding (str): 文本编码，默认为 UTF-8。
    Returns:
        str: 文件的完整文本内容。
    Raises:
        ValueError: 文件路径为空、为绝对路径、指向根目录或越出根目录。
        OSError: 文件不存在、不是普通文件、权限不足或路径无法解析。
        UnicodeError: 文件内容无法按指定编码解码。
        LookupError: 指定的编码不存在。
        RuntimeError: 路径存在符号链接循环。
    """
    return _resolve_file_path(_PROJECT_ROOT, file_path).read_text(encoding=encoding)


def read_temp_file(file_path: str | Path, *, encoding: str = "utf-8") -> str:
    """读取系统临时目录下的文本文件，保留首尾空白。

    Args:
        file_path (str | Path): 相对于 tempfile.gettempdir() 的文件路径。
        encoding (str): 文本编码，默认为 UTF-8。
    Returns:
        str: 文件的完整文本内容。
    Raises:
        ValueError: 文件路径为空、为绝对路径、指向临时目录或越出临时目录。
        OSError: 临时目录不可用、文件不存在、不是普通文件或权限不足。
        UnicodeError: 文件内容无法按指定编码解码。
        LookupError: 指定的编码不存在。
        RuntimeError: 路径存在符号链接循环。
    """
    path = _resolve_file_path(Path(tempfile.gettempdir()), file_path)
    return path.read_text(encoding=encoding)


def write_temp_file(
    file_path: str | Path, content: str, *, encoding: str = "utf-8"
) -> Path:
    """将文本写入系统临时目录，自动创建父目录并覆盖同名文件。

    Args:
        file_path (str | Path): 相对于 tempfile.gettempdir() 的文件路径。
        content (str): 要写入的完整文本内容。
        encoding (str): 文本编码，默认为 UTF-8。
    Returns:
        Path: 写入文件的绝对路径。
    Raises:
        ValueError: 文件路径为空、为绝对路径、指向临时目录或越出临时目录。
        OSError: 临时目录不可用、目录创建失败、权限不足或文件写入失败。
        UnicodeError: 文本无法按指定编码编码。
        LookupError: 指定的编码不存在。
        RuntimeError: 路径存在符号链接循环。
    """
    path = _resolve_file_path(Path(tempfile.gettempdir()), file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)
    return path


def read_system_prompt(file_name: str, *, encoding: str = "utf-8") -> str:
    """根据完整文件名读取 backend/prompt 下的系统提示词，保留首尾空白。

    Args:
        file_name (str): 包含扩展名的文件名，例如 root_decision.md，不含目录。
        encoding (str): 文本编码，默认为 UTF-8。
    Returns:
        str: 提示词文件的完整文本内容。
    Raises:
        ValueError: 文件名为空、包含目录、指向提示词目录或链接到目录外。
        OSError: 文件不存在、不是普通文件、权限不足或路径无法解析。
        UnicodeError: 提示词无法按指定编码解码。
        LookupError: 指定的编码不存在。
        RuntimeError: 路径存在符号链接循环。
    """
    if not file_name or Path(file_name).name != file_name:
        raise ValueError("提示词必须使用包含扩展名且不含目录的文件名")
    return _resolve_file_path(_PROMPT_ROOT, file_name).read_text(encoding=encoding)


def render_prompt_template(template: str, values: Mapping[str, str]) -> str:
    """替换提示词模板中指定的单层或双层大括号占位符。

    Args:
        template (str): 完整提示词模板；未提供值的占位符及 JSON 示例保持不变。
        values (Mapping[str, str]): 占位符名称到替换文本的映射；名称不含大括号。
    Returns:
        str: 将模板中已提供的占位符替换一次后的文本；替换内容不会再次展开。
    Raises:
        不主动抛出业务异常。
    """
    return _PROMPT_PLACEHOLDER_PATTERN.sub(
        lambda match: values.get(match.group(1) or match.group(2), match.group(0)),
        template,
    )


__all__ = [
    "read_project_file",
    "read_system_prompt",
    "read_temp_file",
    "render_prompt_template",
    "write_temp_file",
]
