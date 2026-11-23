"""会话首轮自动生成标题的提示词与安全清洗/降级规则。"""

from __future__ import annotations

import re
import unicodedata

AUTO_TITLE_MAX_CHARS = 20
AUTO_TITLE_FALLBACK_MAX_CHARS = 24
AUTO_TITLE_TIMEOUT_SECONDS = 5.0
AUTO_TITLE_PROMPT_INPUT_MAX_CHARS = 2000

AUTO_TITLE_SYSTEM_PROMPT = (
    "请根据用户的首条消息生成一个简洁的中文对话标题，"
    "长度不超过 20 个字，仅输出标题正文，不要加引号、书名号、序号或任何解释。"
)

_WHITESPACE_PATTERN = re.compile(r"\s+")
_SURROUNDING_QUOTES = "\"'`“”‘’「」『』《》〈〉【】"
_TRAILING_PUNCTUATION = "。，、；：！？.,;:!?…—-"


def _strip_control_and_collapse_whitespace(text: str) -> str:
    """将换行与制表符折为空格、剔除其余 Cc 控制字符并折叠连续空白。"""
    cleaned_chars: list[str] = []
    for character in text:
        if character in ("\n", "\r", "\t"):
            cleaned_chars.append(" ")
        elif unicodedata.category(character) == "Cc":
            continue
        else:
            cleaned_chars.append(character)
    return _WHITESPACE_PATTERN.sub(" ", "".join(cleaned_chars)).strip()


def sanitize_generated_title(raw: str) -> str:
    """清洗模型生成的标题：剔除控制字符/换行、去常见引号与书名号及末尾标点，并截断至 20 字。"""
    cleaned = _strip_control_and_collapse_whitespace(raw)
    if not cleaned:
        return ""
    # 反复剥离外层引号、书名号、空白与末尾标点，直到收敛稳定（如 `"《标题》。"` -> `标题`）。
    while cleaned:
        updated = cleaned.strip()
        updated = updated.strip(_SURROUNDING_QUOTES).strip()
        updated = updated.rstrip(_TRAILING_PUNCTUATION).strip()
        if updated == cleaned:
            break
        cleaned = updated
    if not cleaned:
        return ""
    truncated = cleaned[:AUTO_TITLE_MAX_CHARS].strip()
    return truncated.rstrip(_TRAILING_PUNCTUATION).strip() or truncated


def fallback_title_from_message(text: str) -> str:
    """模型失败/超时/空输出时的兜底降级标题：折叠空白、剔除控制字符后截取前 24 字。"""
    cleaned = _strip_control_and_collapse_whitespace(text)
    if not cleaned:
        return ""
    return cleaned[:AUTO_TITLE_FALLBACK_MAX_CHARS].strip()


__all__ = [
    "AUTO_TITLE_FALLBACK_MAX_CHARS",
    "AUTO_TITLE_MAX_CHARS",
    "AUTO_TITLE_PROMPT_INPUT_MAX_CHARS",
    "AUTO_TITLE_SYSTEM_PROMPT",
    "AUTO_TITLE_TIMEOUT_SECONDS",
    "fallback_title_from_message",
    "sanitize_generated_title",
]
