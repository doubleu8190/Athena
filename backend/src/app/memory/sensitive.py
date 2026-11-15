"""长期记忆持久化边界的统一敏感文本检测。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

SENSITIVE_TEXT_ERROR_CODE = "memory_sensitive_content"

_SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])(?:sk|rk|pk)[-_][A-Za-z0-9_-]{12,}", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_|xox[baprs]-)[A-Za-z0-9_-]{12,}", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?<![A-Za-z0-9])"
        r"(?:password|passwd|pwd|token|secret|api[\s_-]*key|private[\s_-]*key|"
        r"id(?:\s*(?:number|no\.?))?|card(?:\s*(?:number|no\.?))?)"
        r"\s*(?:[:=：]|(?:is|equals?|was)\s+|(?:是|为|即)\s*)\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:密码|口令|令牌|密钥|私钥|身份证(?:号码|号)?|证件(?:号码|号)?|"
        r"银行卡(?:号码|号)?|卡号)\s*(?:[:=：]|是|为|即)\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])"
        r"(?:password|passwd|pwd|token|secret|api[\s_-]*key|private[\s_-]*key)"
        r"[_-](?:value[_-]?)?(?=[A-Za-z0-9+/=_-]*\d)[A-Za-z0-9+/=_-]{6,}",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![A-Za-z0-9])authorization\s*[:=]\s*(?:bearer|basic)\s+\S+", re.IGNORECASE),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
)


def contains_sensitive_text(value: str) -> bool:
    """判断一个即将持久化的文本是否包含明文凭据或高敏感标识。

    参数：
    - `value`：候选正文、证据、结构化文本或诊断理由。

    返回：
    - bool：规范化后命中任一固定敏感模式时返回 True。

    异常：
    - 无预期业务异常。
    """
    normalized = unicodedata.normalize("NFKC", value)
    return bool(normalized) and any(
        pattern.search(normalized) is not None for pattern in _SENSITIVE_PATTERNS
    )


def contains_sensitive_texts(values: Iterable[str]) -> bool:
    """判断任一同批持久化文本是否包含敏感明文。

    参数：
    - `values`：同一条候选或任务将持久化的全部自由文本字段。

    返回：
    - bool：任一非空文本命中固定敏感模式时返回 True，否则返回 False。

    异常：
    - 无预期业务异常。
    """
    return any(contains_sensitive_text(value) for value in values if value)


def raise_for_sensitive_texts(
    values: Iterable[str],
    *,
    error_code: str = SENSITIVE_TEXT_ERROR_CODE,
) -> None:
    """在持久化前拒绝任一同批文本中的敏感明文。

    参数：
    - `values`：同一条候选或任务将持久化的全部自由文本字段。
    - `error_code`：调用边界使用的稳定错误码。

    返回：
    - None：全部文本通过检测。

    异常：
    - ValueError：任一文本命中固定敏感模式；异常只携带稳定错误码，不记录原文。
    """
    if contains_sensitive_texts(values):
        raise ValueError(error_code)


__all__ = [
    "SENSITIVE_TEXT_ERROR_CODE",
    "contains_sensitive_text",
    "contains_sensitive_texts",
    "raise_for_sensitive_texts",
]
