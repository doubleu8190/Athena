"""Agent 图在节点之间传递的状态定义。"""

from __future__ import annotations

from typing import Any, Literal, Required, TypedDict


class FileProcessResult(TypedDict):
    """单个附件在统一 Run 中的最终处理结果。"""

    message_id: str
    attachment_id: str
    status: Literal["ready", "failed"]
    error: str | None
    chunk_count: int | None


class AgentState(TypedDict, total=False):
    """在图节点之间传递的 JSON 安全状态字段。

    ``session_id`` 和 ``run_id`` 是必填字段；其余字段由不同节点按执行阶段逐步补充。
    状态中的消息、附件和结果均使用 JSON 安全的字典结构，领域对象不应直接写入。
    """

    session_id: str
    run_id: str
    user_message: str
    attachment_ids: list[str]
    message_id: str
    file_results: list[FileProcessResult]
    files_ready: bool
    file_error: str | None
    memory_context: str
    memory_request: dict[str, Any] | None
    history: list[dict[str, Any]]
    requested_attachment_refs: list[dict[str, Any]]
    user_message_id: str
    attachment_refs: list[dict[str, Any]]
    harness_messages: list[dict[str, Any]]
    result: dict[str, Any] | None
    error: str
