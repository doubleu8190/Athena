"""Agent 图在节点之间传递的状态定义。"""

from __future__ import annotations

from typing import Any, Required, TypedDict


class AgentState(TypedDict, total=False):
    """在图节点之间传递的 JSON 安全状态字段。

    ``session_id`` 和 ``run_id`` 是必填字段；其余字段由不同节点按执行阶段逐步补充。
    状态中的消息、附件和结果均使用 JSON 安全的字典结构，领域对象不应直接写入。
    """

    session_id: Required[str]
    run_id: Required[str]
    command_id: str
    user_message: str
    system_prompt: str
    attachment_ids: list[str]
    continuation: dict[str, Any]
    memory_context: str
    history: list[dict[str, Any]]
    requested_attachment_refs: list[dict[str, Any]]
    user_message_id: str
    attachment_refs: list[dict[str, Any]]
    harness_messages: list[dict[str, Any]]
    result: dict[str, Any] | None
    error: str
