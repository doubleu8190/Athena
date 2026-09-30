"""Agent 图在节点之间传递的状态定义。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict


AgentExecutionStatus = Literal[
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "interrupted",
]

AgentPhase = Literal[
    "request_preparation",
    "task_understanding",
    "context_planning",
    "context_acquisition",
    "harness_preparation",
    "agent_loop",
    "orchestration",
    "response",
    "completed",
]

class AgentRequestState(TypedDict, total=False):
    """Fields produced while normalizing and persisting the user request."""

    session_id: str
    run_id: str
    message_id: str
    user_message: str
    attachment_ids: list[str]
    history: list[dict[str, Any]]
    requested_attachment_refs: list[dict[str, Any]]


class AgentUnderstandingState(TypedDict, total=False):
    """Task understanding output and clarification decision."""

    task_spec: dict[str, Any] | None
    task_understanding_source: Literal["fast_path", "llm", "fallback"] | None
    clarification_question: str | None


class AgentContextState(TypedDict, total=False):
    """Context planning and acquisition output."""

    context_plan: dict[str, Any] | None
    context_bundle: dict[str, Any] | None
    attachment_refs: list[dict[str, Any]]
    harness_messages: list[dict[str, Any]]


class AgentOrchestrationState(TypedDict, total=False):
    """Plan materialization and worker aggregation output."""

    plan_request: dict[str, Any] | None
    execution_plan: dict[str, Any] | None
    orchestration_result: dict[str, Any] | None


class AgentResponseState(TypedDict, total=False):
    """Final response projection exposed to callers."""

    result: dict[str, Any] | None
    error: str | None
    error_detail: dict[str, Any] | None
    harness_result: dict[str, Any] | None


class AgentExecutionContext(TypedDict, total=False):
    """Execution-only prompt and hierarchy context.

    Run/request identity and task context belong to ``AgentState``.  Keeping
    them here would create a second source of truth inside checkpoints.
    """

    parent_run_id: str | None
    system_prompt: str
    tool_names: list[str] | None


class AgentExecutionMessages(TypedDict, total=False):
    """Conversation and attachment data."""

    messages: list[dict[str, Any]]


class AgentExecutionTools(TypedDict, total=False):
    """Tool calls and their durable results."""

    pending_tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    tool_result_deltas: list[dict[str, Any]]
    tool_message_deltas: list[dict[str, Any]]
    approval_batch: dict[str, Any] | None
    approval_decisions: dict[str, str]
    approval_ids: dict[str, str]


class AgentExecutionBudget(TypedDict, total=False):
    """Turn and retry budget."""

    turn_count: int
    retry_count: int
    max_turns: int
    max_retries: int


class AgentExecutionOutput(TypedDict, total=False):
    """Generated content produced during the loop."""

    last_content: str
    final_content: str


class AgentExecutionStream(TypedDict, total=False):
    """Answer stream checkpoint cursor."""

    stream_started: bool
    stream_version: int
    stream_offset: int


class AgentExecutionLifecycle(TypedDict, total=False):
    """Execution lifecycle."""

    llm_result_status: Literal[
        "completed", "semantic_retry", "technical_failure", "interrupted"
    ]
    llm_retry_reason: str
    retry_feedback: str | None
    retryable: bool
    interrupted: bool
    status: AgentExecutionStatus
    route: str


class AgentExecutionRecoverableState(
    AgentExecutionContext,
    AgentExecutionMessages,
    AgentExecutionTools,
    AgentExecutionBudget,
    AgentExecutionOutput,
    AgentExecutionStream,
):
    """Values required to resume the loop after a checkpoint restore."""


class AgentExecutionDerivedState(AgentExecutionLifecycle):
    """Values that can be recomputed from the recoverable execution state."""


def split_execution_state(
    state: AgentExecutionState | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the two explicit execution state containers."""

    source = state or {}
    return dict(source.get("recoverable", {})), dict(source.get("derived", {}))


def derive_execution_state(
    recoverable: AgentExecutionRecoverableState | None,
    derived: AgentExecutionDerivedState | None = None,
) -> AgentExecutionDerivedState:
    """Compute lifecycle fields from recoverable execution inputs."""

    source = recoverable or {}
    previous = derived or {}
    interrupted = bool(previous.get("interrupted", False))
    if interrupted:
        status: AgentExecutionStatus = "interrupted"
    elif source.get("approval_batch") and not source.get("approval_decisions"):
        status = "waiting_approval"
    else:
        status = previous.get("status", "running")  # type: ignore[assignment]
    return {
        "status": status,
        "route": previous.get("route", "agent_loop"),
        "retryable": bool(previous.get("retryable", False)),
        "interrupted": interrupted,
        "llm_result_status": previous.get("llm_result_status", "completed"),
        "llm_retry_reason": previous.get("llm_retry_reason", "none"),
        "retry_feedback": previous.get("retry_feedback"),
    }


class AgentExecutionState(TypedDict, total=False):
    """Agent loop 的可检查点化执行状态。

    ``recoverable`` 和 ``derived`` 是两个独立的检查点容器。LLM、工具管理器和
    停止事件通过节点依赖及 ``RunnableConfig`` 注入，不写入检查点。
    """

    recoverable: AgentExecutionRecoverableState
    derived: AgentExecutionDerivedState


def merge_execution_state(
    current: AgentExecutionState | None,
    update: AgentExecutionState | None,
) -> AgentExecutionState:
    """合并 Agent loop 内部节点的局部状态更新。

    ``execution`` 是主图中的嵌套状态通道；两个子容器分别合并，避免节点
    回写无关字段。
    """

    current_recoverable, current_derived = split_execution_state(current)
    update_recoverable, update_derived = split_execution_state(update)
    recoverable = {**current_recoverable, **update_recoverable}
    derived = {**current_derived, **update_derived}
    for field in ("tool_result_deltas", "tool_message_deltas"):
        if field in update_recoverable:
            recoverable[field] = [
                *current_recoverable.get(field, []),
                *update_recoverable.get(field, []),
            ]
    return {"recoverable": recoverable, "derived": derived}


class AgentState(
    AgentRequestState,
    AgentUnderstandingState,
    AgentContextState,
    AgentOrchestrationState,
    AgentResponseState,
    total=False,
):
    """在图节点之间传递的 JSON 安全状态字段。

    ``request`` 是入口阶段状态；其余阶段容器由对应节点逐步补充。
    状态中的消息、附件和结果均使用 JSON 安全的字典结构，领域对象不应直接写入。
    """

    phase: AgentPhase
    request: AgentRequestState
    understanding: AgentUnderstandingState
    context: AgentContextState
    execution: Annotated[AgentExecutionState, merge_execution_state]
    orchestration: AgentOrchestrationState
    response: AgentResponseState
    tool_call: dict[str, Any]
