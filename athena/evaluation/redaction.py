"""影子事件和人工审核负载的策略检查。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.evaluation.privacy import contains_sensitive_text, redact_text, safe_hash


@dataclass(frozen=True)
class RedactedRequest:
    """不包含原始查询正文的请求元数据。"""

    query_hash: str
    query_length: int
    scope: dict[str, Any]
    redacted_query: str | None = None


def redact_request(
    query: str, scope: dict[str, Any], *, salt: str, include_query: bool = False
) -> RedactedRequest:
    """生成可审计但不泄露查询正文的请求记录。

    参数：
        query: 原始查询文本。
        scope: 检索范围元数据。
        salt: 部署级哈希盐。
        include_query: 是否在返回值中保留已脱敏查询；默认不保留。

    返回值：
        包含查询哈希、长度和范围副本的脱敏请求。

    异常：
        ValueError: 查询包含禁止进入 shadow 存储的敏感文本。
    """
    if contains_sensitive_text(query) or _contains_sensitive_value(scope):
        raise ValueError(
            "request contains sensitive text and cannot enter shadow storage"
        )
    return RedactedRequest(
        safe_hash(query, salt),
        len(query),
        dict(scope),
        redact_text(query) if include_query else None,
    )


def _contains_sensitive_value(value: Any) -> bool:
    """递归检查范围元数据，防止邮箱、手机号和密钥随事件落盘。"""
    if isinstance(value, str):
        return contains_sensitive_text(value)
    if isinstance(value, dict):
        return any(
            _contains_sensitive_value(key) or _contains_sensitive_value(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_sensitive_value(item) for item in value)
    return False


def sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """校验事件不包含原始内容字段，并返回可写入的事件字典。

    异常：
        ValueError: 事件包含禁止的查询、内容或候选文本字段。
    """
    forbidden = {
        "query",
        "content",
        "document",
        "raw_query",
        "candidate_text",
        "api_key",
        "token",
        "secret",
        "password",
        "authorization",
    }

    def check(value: Any) -> None:
        if isinstance(value, dict):
            if any(str(key).lower() in forbidden for key in value):
                raise ValueError("shadow event contains forbidden content fields")
            for item in value.values():
                check(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                check(item)

    check(event)
    return event
