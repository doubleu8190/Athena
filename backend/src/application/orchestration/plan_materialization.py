"""Root Plan 草稿到领域对象的转换与工具范围校验。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from application.runs.request_contracts import RootRequestState
from domain.orchestration import Plan, PlanEdge, Task
from application.orchestration.root_decision import PlanDraft

from .serialization import as_dict


def plan_dict(plan: Plan, tasks: list[Task]) -> dict[str, Any]:
    """将已提交的 Plan 与 Task 转为 checkpoint 可保存的字典。"""
    return {
        "plan_id": plan.plan_id,
        "goal": plan.goal,
        "task_ids": list(plan.task_ids),
        "edges": [asdict(edge) for edge in plan.edges],
        "max_parallelism": plan.max_parallelism,
        "session_id": plan.session_id,
        "run_id": plan.run_id,
        "tasks": [as_dict(task) for task in tasks],
    }


def coerce_plan(
    draft: PlanDraft, *, request: RootRequestState
) -> tuple[Plan, list[Task]]:
    """将契约已验证的草稿转换为领域 Plan 和 Task。
    Args:
        draft: 通过 RootDecision 校验的计划草稿。
        request: 当前 Run 请求，用于绑定原始目标和领域身份。
    Returns:
        尚未作用域化、可继续验证的领域 Plan 与任务列表。
    Raises:
        ValueError: 当前 Run 身份或草稿中的重复任务 ID 无效。
    """
    run_id = request.run_id
    if not run_id:
        raise ValueError("run_id is required for plan materialization")
    task_ids = tuple(task.task_id for task in draft.tasks)
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("root_plan_task_ids_must_be_unique")
    plan = Plan(
        plan_id=f"plan:{run_id}",
        goal=request.user_message or draft.goal,
        task_ids=task_ids,
        edges=tuple(
            PlanEdge(edge.from_task_id, edge.to_task_id)
            for edge in draft.edges
        ),
        max_parallelism=draft.max_parallelism,
        session_id=request.session_id,
        run_id=run_id,
    )
    tasks = [
        Task(
            task_id=task.task_id,
            title=task.title,
            objective=task.objective,
            input=dict(task.input),
            expected_output=dict(task.expected_output),
            allowed_tools=tuple(task.allowed_tools),
        )
        for task in draft.tasks
    ]
    return plan, tasks


def scope_execution_plan(
    plan: Plan,
    tasks: list[Task],
    *,
    run_id: str,
    worker_tools: set[str],
) -> tuple[Plan, list[Task]]:
    """绑定本次 Run 的任务身份并拒绝系统未提供的工具。
    Args:
        plan: 已从具体草稿转换的领域计划。
        tasks: 计划中的任务定义。
        run_id: 当前 Run 身份。
        worker_tools: 系统提供的 Worker 工具名集合。
    Returns:
        任务 ID 和计划边均绑定当前 Run 的领域对象。
    Raises:
        ValueError: 任务标识重复、依赖不存在或工具不在系统能力目录中。
    """
    if not run_id:
        raise ValueError("run_id is required for plan scoping")
    task_ids = tuple(task.task_id for task in tasks)
    if not task_ids or len(task_ids) != len(set(task_ids)):
        raise ValueError("root_plan_task_ids_must_be_unique")
    task_id_map = {task_id: f"{task_id}:{run_id}" for task_id in task_ids}
    unknown_edges = [
        edge
        for edge in plan.edges
        if edge.from_task_id not in task_id_map
        or edge.to_task_id not in task_id_map
    ]
    if unknown_edges:
        raise ValueError("root_plan_edge_references_unknown_task")

    unavailable_tools = sorted(
        {
            tool_name
            for task in tasks
            for tool_name in task.allowed_tools
            if tool_name not in worker_tools
        }
    )
    if unavailable_tools:
        raise ValueError(
            "root_plan_requests_unavailable_tools: "
            + ", ".join(unavailable_tools)
        )

    scoped_tasks = [
        Task(
            task_id=task_id_map[task.task_id],
            title=task.title,
            objective=task.objective,
            input=task.input,
            expected_output=task.expected_output,
            allowed_tools=tuple(dict.fromkeys(task.allowed_tools)),
            worker_type="general",
            timeout_seconds=task.timeout_seconds,
        )
        for task in tasks
    ]
    scoped_plan_id = plan.plan_id
    if not scoped_plan_id.endswith(f":{run_id}"):
        scoped_plan_id = f"{scoped_plan_id}:{run_id}"
    scoped_plan = Plan(
        plan_id=scoped_plan_id,
        goal=plan.goal,
        task_ids=tuple(task.task_id for task in scoped_tasks),
        edges=tuple(
            PlanEdge(
                task_id_map[edge.from_task_id],
                task_id_map[edge.to_task_id],
            )
            for edge in plan.edges
        ),
        max_parallelism=plan.max_parallelism,
        status=plan.status,
        session_id=plan.session_id,
        run_id=run_id,
    )
    return scoped_plan, scoped_tasks


__all__ = ["coerce_plan", "plan_dict", "scope_execution_plan"]
