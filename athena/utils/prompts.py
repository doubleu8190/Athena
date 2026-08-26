"""提示词加载器 — 从 prompt/ 目录加载 markdown 文件.

所有 LLM 提示词统一存放在项目根目录的 prompt/ 文件夹中，
以 markdown 格式存储，便于非工程师编辑和版本管理。
加载器在首次访问时读取文件并缓存，避免重复 I/O。
"""

from __future__ import annotations

from pathlib import Path

# prompt/ 目录位于项目根目录（athena/ 的上一级）
_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"

_cache: dict[str, str] = {}


def get_prompt(name: str) -> str:
    """按名称加载提示词（带缓存）.

    参数：
        name: 提示词文件名（不含 .md 扩展名），如 "system"、"fact_extraction"。

    返回值：
        提示词文本（已 strip）。

    异常：
        FileNotFoundError: 对应的 .md 文件不存在。
    """
    if name not in _cache:
        path = _PROMPT_DIR / f"{name}.md"
        _cache[name] = path.read_text(encoding="utf-8").strip()
    return _cache[name]


def reload_prompts() -> None:
    """清空缓存（用于热重载或测试）."""
    _cache.clear()
