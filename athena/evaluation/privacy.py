"""脱敏和无内容报告序列化。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, is_dataclass
from typing import Any

_SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)")


def contains_sensitive_text(value: str) -> bool:
    """判断文本是否包含密钥、邮箱或电话号码等敏感内容。"""
    return bool(
        _SECRET_RE.search(value) or _EMAIL_RE.search(value) or _PHONE_RE.search(value)
    )


def redact_text(value: str) -> str:
    """将文本中的密钥、邮箱和电话号码替换为占位符。"""
    value = _SECRET_RE.sub("[REDACTED_SECRET]", value)
    value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    return _PHONE_RE.sub("[REDACTED_PHONE]", value)


def safe_hash(value: str, salt: str) -> str:
    """使用盐计算文本的 SHA-256 摘要。

    参数：
        value: 原始文本。
        salt: 部署级哈希盐。

    返回值：
        小写十六进制摘要。
    """
    return hashlib.sha256(f"{salt}\x00{value}".encode("utf-8")).hexdigest()


def safe_report(value: Any, *, salt: str = "evaluation") -> Any:
    """序列化元数据，同时排除查询或内容类字段。"""
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        hidden = {
            "query",
            "content",
            "document",
            "raw_query",
            "quote",
            "candidate_text",
            "api_key",
            "token",
            "secret",
            "password",
            "authorization",
        }
        return {
            key: safe_report(item, salt=salt)
            for key, item in value.items()
            if key.lower() not in hidden
        }
    if isinstance(value, (list, tuple)):
        return [safe_report(item, salt=salt) for item in value]
    if isinstance(value, str):
        return redact_text(value) if contains_sensitive_text(value) else value
    return value
