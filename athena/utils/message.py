"""消息类型转换工具 — 消息模型/dict ↔ BaseMessage 双向转换.

集中管理转换逻辑，避免各模块重复实现。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from athena.models import Message
from athena.utils.ids import generate_time_id


def dict_to_message(m: Message | dict[str, Any]) -> BaseMessage:
    """消息模型/字典 → LangChain Message.

    将原始 id 存入 metadata["message_id"]，确保 message_to_dict 可还原。
    传入 Message 模型时先归一化为 dict（role/timestamp 序列化为字符串）。
    """
    if isinstance(m, Message):
        m = m.model_dump(mode="json")
    role = m.get("role", "user")
    content = m.get("content", "")
    tool_calls = m.get("tool_calls") or []
    tool_call_id = m.get("tool_call_id")

    meta: dict[str, Any] = {}
    if m.get("id"):
        meta["message_id"] = m["id"]

    if role == "user":
        return HumanMessage(content=content, metadata=meta)
    if role == "system":
        return SystemMessage(content=content, metadata=meta)
    if role == "assistant":
        if tool_calls:
            lc_tcs = [
                {
                    "id": tc.get("id", generate_time_id()),
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}) or tc.get("arguments", {}) or {},
                    "type": "tool_call",
                }
                for tc in tool_calls
            ]
            return AIMessage(content=content, tool_calls=lc_tcs, metadata=meta)
        return AIMessage(content=content, metadata=meta)
    if role == "tool":
        return ToolMessage(content=content, tool_call_id=tool_call_id or "", metadata=meta)
    return HumanMessage(content=content, metadata=meta)


def message_to_dict(m: BaseMessage) -> dict[str, Any]:
    """LangChain Message → 字典.

    从 metadata["message_id"] 还原原始消息 id。
    """
    role_map = {
        HumanMessage: "user",
        SystemMessage: "system",
        AIMessage: "assistant",
        ToolMessage: "tool",
    }
    role = "user"
    for cls, r in role_map.items():
        if isinstance(m, cls):
            role = r
            break

    d: dict[str, Any] = {"role": role, "content": getattr(m, "content", "")}

    # 还原原始消息 id
    meta = getattr(m, "metadata", None) or {}
    if meta.get("message_id"):
        d["id"] = meta["message_id"]

    if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
        d["tool_calls"] = [
            {"id": tc.get("id", ""), "name": tc.get("name", ""), "args": tc.get("args", {})}
            for tc in m.tool_calls
        ]
    if isinstance(m, ToolMessage):
        d["tool_call_id"] = getattr(m, "tool_call_id", "")
    return d


def dicts_to_messages(messages: Sequence[Message | dict[str, Any]]) -> list[BaseMessage]:
    """批量转换：消息模型/字典列表 → BaseMessage 列表."""
    return [dict_to_message(m) for m in messages]


def messages_to_dicts(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """批量转换：BaseMessage 列表 → 字典列表."""
    return [message_to_dict(m) for m in messages]
