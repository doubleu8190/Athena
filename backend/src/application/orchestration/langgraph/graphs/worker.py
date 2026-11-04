"""Worker 应用工作流的 LangGraph 适配器。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Mapping

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from application.observability import (
    RunObservabilityService,
    WorkerNodeSpanAttributes,
    bind_span,
    reset_span,
)
from application.orchestration.protocols import WorkerWorkflowPort
from domain.orchestration.entities import TaskExecution
from shared.serialization import json_value

from .state import WorkerGraphState


def build_worker_graph(
    *,
    workflow: WorkerWorkflowPort,
    checkpointer: Any = None,
    observability: RunObservabilityService | None = None,
) -> Any:
    """构建执行单个持久化 Task 的 Worker 图。

    参数：
    - `workflow`（WorkerWorkflowPort）：负责执行业务步骤的应用工作流；约束：无额外运行时限制。
    - `checkpointer`（Any）：保存和恢复图执行状态的检查点依赖；约束：可省略，默认值为 `None`。
    - `observability`（RunObservabilityService | None）：写入接入方 Trace 的服务；
      外部 Worker 进程未接入 Athena Trace 时可省略。

    返回：
    - Any：已编译且可使用检查点恢复的 Worker 工作流图。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    graph = StateGraph(WorkerGraphState)
    handlers = {
        "prepare_worker": workflow.prepare,
        "worker_agent_loop": workflow.run,
        "approval_gate": workflow.approval_gate,
        "execute_tool_batch": workflow.execute_tool_batch,
        "finish_worker": workflow.finalize,
    }
    for name, handler in handlers.items():
        graph.add_node(name, _node(name, handler, observability))
    graph.add_edge(START, "prepare_worker")
    graph.add_edge("prepare_worker", "worker_agent_loop")
    graph.add_edge("worker_agent_loop", "approval_gate")
    graph.add_conditional_edges(
        "approval_gate",
        workflow.route_after_gate,
        {"execute_tool_batch": "execute_tool_batch", "finish_worker": "finish_worker"},
    )
    graph.add_conditional_edges(
        "execute_tool_batch",
        workflow.route_after_tools,
        {
            "worker_agent_loop": "worker_agent_loop",
            "request_worker_approval": "request_worker_approval",
            "finish_worker": "finish_worker",
        },
    )
    async def request_worker_approval(state: WorkerGraphState) -> dict[str, Any]:
        """中断当前 Worker 并等待审批决定后恢复原操作。

        参数：
        - `state`（WorkerGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：包含挂起审批请求及恢复信息的 Worker 图状态补丁。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return await _request_worker_approval(workflow, state)

    graph.add_node(
        "request_worker_approval",
        _node("request_worker_approval", request_worker_approval, observability),
    )
    graph.add_conditional_edges(
        "request_worker_approval",
        lambda state: (
            "execute_tool_batch"
            if state.get("result", {}).get("tool_calls")
            else "worker_agent_loop"
        ),
        {
            "execute_tool_batch": "execute_tool_batch",
            "worker_agent_loop": "worker_agent_loop",
        },
    )
    graph.add_edge("finish_worker", END)
    return graph.compile(checkpointer=checkpointer)


def _node(
    name: str,
    handler: Callable[[WorkerGraphState], Awaitable[dict[str, Any]]],
    observability: RunObservabilityService | None,
) -> Callable[[WorkerGraphState], Awaitable[dict[str, Any]]]:
    """包装 Worker 节点，将 LLM 和工具子 span 绑定到当前节点。

    参数：
    - `name`：LangGraph 节点名，同时作为 Trace span 名称。
    - `handler`：原 Worker 节点的异步处理函数。
    - `observability`：Athena Trace 服务；未接入时为 None。

    返回：
    - Callable：保持原节点返回和异常语义的异步包装函数。

    异常：
    - 原处理函数抛出的异常会在记录 failed span 后继续抛出。
    """

    async def wrapped(state: WorkerGraphState) -> dict[str, Any]:
        """执行节点并按完成、失败或挂起状态收尾 Trace。"""
        execution = state.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        execution_id = execution.get("execution_id")
        session_id = execution.get("session_id")
        run_id = execution.get("run_id")
        span = None
        token = None
        if (
            observability is not None
            and isinstance(execution_id, str)
            and isinstance(session_id, str)
            and isinstance(run_id, str)
        ):
            span = await observability.start_span(
                session_id=session_id,
                run_id=run_id,
                name=name,
                attributes=WorkerNodeSpanAttributes(node_name=name),
                parent_span_id=f"worker:{execution_id}",
            )
            token = bind_span(span.span_id)
        try:
            value = await handler(state)
        except GraphInterrupt:
            if observability is not None and span is not None:
                await observability.finish_span(
                    span.span_id, status="suspended"
                )
            raise
        except Exception as exc:
            if observability is not None and span is not None:
                await observability.finish_span(
                    span.span_id,
                    status="failed",
                    error={
                        "code": str(getattr(exc, "code", "worker_node_failed")),
                        "message": type(exc).__name__,
                    },
                )
            raise
        finally:
            if token is not None:
                reset_span(token)
        if observability is not None and span is not None:
            await observability.finish_span(span.span_id)
        return value or {}

    wrapped.__name__ = name
    return wrapped


async def _request_worker_approval(
    workflow: WorkerWorkflowPort,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """中断当前 Worker，并在 ResumeWorker 后执行原样冻结的操作。

    参数：
    - `workflow`（WorkerWorkflowPort）：提供审批后工具执行用例的应用工作流；约束：无额外运行时限制。
    - `state`（Mapping[str, Any]）：包含待审批操作的可恢复 Worker 状态；约束：无额外运行时限制。

    返回：
    - dict[str, Any]：恢复后写入图状态的工具结果。

    异常：
    - RuntimeError：校验条件 `not isinstance(approval, Mapping) or not approval` 成立时（异常说明：worker_approval_request_missing）。
    - 未列出的依赖异常按当前实现传播。
    """
    approval = state.get("pending_approval")
    if not isinstance(approval, Mapping) or not approval:
        raise RuntimeError("worker_approval_request_missing")
    decision = interrupt(dict(approval))
    return await workflow.complete_tool_approval(state, decision)


async def invoke_worker_graph(
    graph: Any,
    *,
    task_id: str,
    execution_generation: int,
    execution: TaskExecution,
    resume_value: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按 Task 和执行代次调用 Worker 图。

    参数：
    - `graph`（Any）：已编译或待执行的工作流图；约束：无额外运行时限制。
    - `task_id`（str）：目标 Worker 任务的稳定标识；约束：须通过方法内校验；失败条件见下方异常说明。
    - `execution_generation`（int）：目标执行代次，用于隔离 Worker 重试状态；约束：无额外运行时限制。
    - `execution`（TaskExecution）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
    - `resume_value`（Mapping[str, Any] | None）：图中断后用于恢复执行的持久化值；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

    返回：
    - dict[str, Any]：Worker 图执行后返回的状态字段映射。

    异常：
    - ValueError：校验条件 `not task_id` 成立时（异常说明：task_id is required）。
    - 未列出的依赖异常按当前实现传播。
    """
    if not task_id:
        raise ValueError("task_id is required")
    encoded = json_value(execution)
    state: WorkerGraphState = {
        "execution": encoded if isinstance(encoded, dict) else {"value": encoded}
    }
    thread_id = (
        encoded.get("worker_thread_id")
        if isinstance(encoded, Mapping)
        else None
    ) or f"task:{task_id}:exec:{execution_generation}"
    config = {"configurable": {"thread_id": str(thread_id)}}
    if resume_value is not None:
        current_execution = encoded if isinstance(encoded, Mapping) else {}
        checkpoint = await graph.aget_state(config=config)
        values = checkpoint.values
        saved_execution = (
            values.get("execution", {}) if isinstance(values, Mapping) else {}
        )
        merged_execution = (
            dict(saved_execution) if isinstance(saved_execution, Mapping) else {}
        )
        merged_execution.update(current_execution)
        for key in ("tool_results", "_worker_result_refs"):
            if key in saved_execution:
                merged_execution[key] = saved_execution[key]
        resume_payload = dict(resume_value)
        resume_payload["__worker_execution_update"] = merged_execution
        return await graph.ainvoke(
            Command(resume=resume_payload),
            config=config,
        )
    return await graph.ainvoke(state, config=config)


__all__ = ["build_worker_graph", "invoke_worker_graph"]
