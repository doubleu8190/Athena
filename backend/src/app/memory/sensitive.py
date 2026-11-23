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
        r"(?:密码|口令|令牌|密钥|私钥|身份证(?:号码|号)?|证件(?:号码|号)?|"
        r"银行卡(?:号码|号)?|卡号)\s*(?:[:=：]|是|为|即)\s*\S+",
        re.IGNORECASE,
    ),
    # 仅匹配明确高敏键的拼接形式，避免误杀 token_limit_128000 等普通技术标识。
    re.compile(
        r"(?<![A-Za-z0-9])"
        r"(?:password|passwd|pwd|secret|api[\s_-]*key|private[\s_-]*key|"
        r"(?:access|refresh|auth|bearer)[\s_-]*token)"
        r"[_-](?:value[_-]?)?(?=[A-Za-z0-9+/=_-]*\d)[A-Za-z0-9+/=_-]{6,}",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![A-Za-z0-9])authorization\s*[:=]\s*(?:bearer|basic)\s+\S+", re.IGNORECASE),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
)

# 仅对明确高敏英文键做赋值检查，放行裸 token（context token = 128000）与裸 id（用户 id 是 alice）。
# 值捕获组主动截断中文字符与中文标点，避免凭据后紧跟“（生产环境）”“请保密”时被整段吞并导致漏拦，
# 同时中文自然语言说明句（如“API key 是通过环境变量注入的”）因首字符为中文不会误匹配。
_ENGLISH_CREDENTIAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:password|passwd|pwd|secret|api[\s_-]*key|private[\s_-]*key|"
    r"(?:access|refresh|auth|bearer)[\s_-]*token|"
    r"id[\s_-]*(?:number|no\.?)|card[\s_-]*(?:number|no\.?))"
    r"\s*([:=：]|(?:is|equals?|was)\s+|(?:是|为|即)\s*)"
    r"\s*([^\s，。；！？、,;!?\)\]}\u3000-\u303f\u3400-\u9fff\uf900-\ufaff]+)",
    re.IGNORECASE,
)
_CONTIGUOUS_CARD_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d{13,19}(?![A-Za-z0-9])")
_SPACE_CARD_GROUPS_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d+(?: \d+)+(?![A-Za-z0-9])")
_HYPHEN_CARD_GROUPS_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d+(?:-\d+)+(?![A-Za-z0-9])")
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
_ASCII_ALNUM_PATTERN = re.compile(r"[A-Za-z0-9]")
_ENGLISH_PROSE_WORDS = frozenset(
    {
        "a",
        "an",
        "configured",
        "empty",
        "encrypted",
        "from",
        "hidden",
        "in",
        "injected",
        "loaded",
        "managed",
        "masked",
        "missing",
        "not",
        "optional",
        "provided",
        "redacted",
        "required",
        "rotated",
        "set",
        "stored",
        "the",
        "through",
        "unset",
        "used",
        "via",
    }
)


def _looks_like_credential_value(connector: str, raw_value: str) -> bool:
    """判断英文高敏键后的值是否像明文凭据/标识，排除自然语言说明句。"""
    candidate = raw_value.strip("\"'`()[]{}.,;!?“”‘’")
    if not candidate:
        return False
    # 纯中文或含中文解释短语（如“通过环境变量注入的”）不视为英文明文凭据。
    if _CJK_PATTERN.search(candidate):
        return False
    if not _ASCII_ALNUM_PATTERN.search(candidate):
        return False
    if connector.strip().lower() in {"is", "equal", "equals", "was"} and (
        candidate.lower() in _ENGLISH_PROSE_WORDS
    ):
        return False
    return True


def _has_payment_card_iin(digits: str) -> bool:
    """校验主流银行卡号段，排除时间戳（1...）、ISBN（978/979...）与年份订单号（202x...）。"""
    if digits.startswith(("3", "4", "5", "6")):
        return True
    # MasterCard 2-series 发卡号段为 222100-272099。
    return len(digits) >= 4 and 2221 <= int(digits[:4]) <= 2720


def _is_luhn_valid(digits: str) -> bool:
    """使用 Luhn 算法校验 13-19 位银行卡号候选，并排除全相同数字。"""
    if not (13 <= len(digits) <= 19) or not digits.isdigit():
        return False
    if len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, ch in enumerate(digits):
        value = int(ch)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _contains_grouped_luhn_card(text: str, pattern: re.Pattern[str], separator: str) -> bool:
    """按纯分隔符数字组边界枚举子切片做 IIN + Luhn 校验，避免前导日期/金额吞并真卡号。"""
    for match in pattern.finditer(text):
        groups = match.group(0).split(separator)
        for start in range(len(groups)):
            # 非末段长度需 >= 3，过滤类似日期（11-23）或 ISBN（0-13）的短段。
            if len(groups[start]) < 3:
                continue
            total_len = len(groups[start])
            for end in range(start + 1, len(groups)):
                group = groups[end]
                total_len += len(group)
                if total_len > 19:
                    break
                if 13 <= total_len <= 19:
                    digits = "".join(groups[start : end + 1])
                    if _has_payment_card_iin(digits) and _is_luhn_valid(digits):
                        return True
                if len(group) < 3:
                    # 短段仅允许作为候选末段，继续向后扩展将使其成为非末段，立即剪枝。
                    break
    return False


def _contains_luhn_card_number(text: str) -> bool:
    """分两步独立扫描连续数字与纯分隔数字组，执行发卡号段与 Luhn 算法校验。"""
    for match in _CONTIGUOUS_CARD_PATTERN.finditer(text):
        digits = match.group(0)
        if _has_payment_card_iin(digits) and _is_luhn_valid(digits):
            return True
    return _contains_grouped_luhn_card(
        text, _SPACE_CARD_GROUPS_PATTERN, " "
    ) or _contains_grouped_luhn_card(text, _HYPHEN_CARD_GROUPS_PATTERN, "-")


def _contains_english_credential(text: str) -> bool:
    """检查明确高敏英文键的赋值语境是否携带疑似明文凭据。"""
    for match in _ENGLISH_CREDENTIAL_PATTERN.finditer(text):
        if _looks_like_credential_value(match.group(1), match.group(2)):
            return True
    return False


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
    if not normalized:
        return False
    return (
        any(pattern.search(normalized) is not None for pattern in _SENSITIVE_PATTERNS)
        or _contains_english_credential(normalized)
        or _contains_luhn_card_number(normalized)
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
