"""LLM 响应工具 — 从 BaseMessage 中安全提取文本内容."""

from __future__ import annotations

import json
import re
from typing import Any


def extract_message_text(response: Any) -> str:
    """从 LLM 响应（LangChain BaseMessage）中提取纯文本内容.

    兼容三种形态：
    - content 为 str: 直接返回
    - content 为 list[dict]: 多模态内容块，拼接其中的文本部分
    - content 为 None/缺失: 返回空字符串

    刻意不做 str(response) 兜底——消息对象的 repr 不是可用文本，
    静默返回 repr 会掩盖响应结构异常并污染下游数据（如成为检索查询）。
    空内容由调用方记录日志并决定回退策略，异常形态才可见、可调试。
    """
    content = getattr(response, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text") if isinstance(c, dict) else c for c in content]
        # 过滤空片段，避免非文本内容块（如图片）注入多余换行
        return "\n".join(p for p in parts if p)
    return ""


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（4 字符 ≈ 1 token）.

    project_memory 约束：使用 model.get_num_tokens()，缺失时回退到 len/4。
    此处采用回退方案，因为底层模型实例不一定暴露 get_num_tokens。
    """
    return len(text) // 4


def extract_json_from_llm_response(text: str) -> dict[str, Any] | None:
    """从 LLM 自由文本响应中提取第一个 JSON 对象.

    兼容 LLM 在 JSON 前后附加说明文字的常见情况。
    返回解析后的 dict，无有效 JSON 时返回 None。
    """
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
