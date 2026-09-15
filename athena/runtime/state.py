"""Agent 图在节点之间传递的状态定义。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict


class FileProcessResult(TypedDict):
    """单个附件在统一 Run 中的最终处理结果。"""

    message_id: str
    attachment_id: str
    status: Literal["ready", "failed"]
    error: str | None
    chunk_count: int | None


AgentExecutionStatus = Literal[
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "interrupted",
]


class AgentExecutionContext(TypedDict, total=False):
    """Run identity and prompt context."""

    session_id: str
    run_id: str
    message_id: str
    parent_run_id: str | None
    user_message: str
    memory_context: str
    system_prompt: str
    tool_names: list[str] | None


class AgentExecutionMessages(TypedDict, total=False):
    """Conversation and attachment data."""

    messages: list[dict[str, Any]]
    attachment_refs: list[dict[str, Any]]


class AgentExecutionTools(TypedDict, total=False):
    """Tool calls and their durable results."""

    pending_tool_calls: list[dict[str, Any]]
    pending_approvals: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]


class AgentExecutionBudget(TypedDict, total=False):
    """Turn and retry budget."""

    turn_count: int
    retry_count: int
    max_turns: int
    max_retries: int


class AgentExecutionOutput(TypedDict, total=False):
    """Generated content and final serialized result."""

    last_content: str
    final_content: str
    harness_result: dict[str, Any]
    plan_request: dict[str, Any] | None


class AgentExecutionStream(TypedDict, total=False):
    """Answer stream checkpoint cursor."""

    stream_started: bool
    stream_version: int
    stream_offset: int


class AgentExecutionLifecycle(TypedDict, total=False):
    """Execution lifecycle."""

    error: str | None
    interrupted: bool
    status: AgentExecutionStatus
    route: str


class AgentExecutionState(
    AgentExecutionContext,
    AgentExecutionMessages,
    AgentExecutionTools,
    AgentExecutionBudget,
    AgentExecutionOutput,
    AgentExecutionStream,
    AgentExecutionLifecycle,
):
    """Agent loop 的扁平、可检查点化执行状态。

    字段按职责由基类分组，但组合后的运行时值仍是一个普通字典。该状态
    只包含 JSON 安全值；LLM、工具管理器和停止事件通过节点依赖及
    ``RunnableConfig`` 注入，不写入检查点。
    """


def merge_execution_state(
    current: AgentExecutionState | None,
    update: AgentExecutionState | None,
) -> AgentExecutionState:
    """合并 Agent loop 内部节点的局部状态更新。

    ``execution`` 是主图中的嵌套状态通道。LangGraph 默认会用节点返回值
    替换整个字典；该 reducer 改为按字段合并，使每个内部节点仅声明自己
    实际改变的状态，避免回写无关字段。
    """

    merged = {**(current or {}), **(update or {})}
    # 清理 thinking 状态迁移前写入的旧 checkpoint 字段。thinking 现在只
    # 通过 durable events 传输，不能因恢复旧 checkpoint 而重新进入状态。
    merged.pop("thinking_chunk_id", None)
    merged.pop("thinking_content", None)
    return merged


class AgentState(TypedDict, total=False):
    """在图节点之间传递的 JSON 安全状态字段。

    ``session_id`` 和 ``run_id`` 是必填字段；其余字段由不同节点按执行阶段逐步补充。
    状态中的消息、附件和结果均使用 JSON 安全的字典结构，领域对象不应直接写入。
    """

    session_id: str
    run_id: str
    message_id: str
    user_message: str
    attachment_ids: list[str]
    requested_attachment_refs: list[dict[str, Any]]
    file_results: list[FileProcessResult]
    memory_context: str
    memory_request: dict[str, Any] | None
    history: list[dict[str, Any]]
    attachment_refs: list[dict[str, Any]]
    harness_messages: list[dict[str, Any]]
    execution: Annotated[AgentExecutionState, merge_execution_state]
    harness_result: dict[str, Any] | None
    planning_decision: dict[str, Any] | None
    plan_request: dict[str, Any] | None
    execution_plan: dict[str, Any] | None
    orchestration_result: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None
