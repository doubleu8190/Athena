"""Root Plan 解析、领域转换与作用域校验。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from domain.orchestration import (
    Plan,
    PlanEdge,
    Task,
)

from .serialization import as_dict


def plan_dict(
    plan: Plan | Mapping[str, Any], tasks: list[Task] | None = None
) -> dict[str, Any]:
    """将 Plan 和 Task 转换成图状态使用的 JSON 字典。"""
    if isinstance(plan, Plan):
        value = {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "task_ids": list(plan.task_ids),
            "edges": [asdict(edge) for edge in plan.edges],
            "max_parallelism": plan.max_parallelism,
            "session_id": plan.session_id,
            "run_id": plan.run_id,
        }
    else:
        value = as_dict(plan)
    if tasks is not None:
        value["tasks"] = [as_dict(task) for task in tasks]
    value.setdefault(
        "task_ids", [item.get("task_id") for item in value.get("tasks", [])]
    )
    value.setdefault("edges", [])
    return as_dict(value)


def plan_generation_failure() -> dict[str, Any]:
    """构造计划草稿缺失或为空时的稳定失败状态。"""
    return {
        "response": {
            "content": "无法生成可执行计划。",
            "error": {
                "code": "plan_generation_failed",
                "message": "Root Agent did not return a structured plan.",
            },
        },
        "status": "failed",
    }


def _coerce_plan_edge(value: Any) -> PlanEdge:
    """把对象或二元数组形式的边转换成 PlanEdge。"""
    if isinstance(value, Mapping):
        source = value.get("from_task_id") or value.get("from") or ""
        target = value.get("to_task_id") or value.get("to") or ""
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        source, target = value
    else:
        raise ValueError("plan edges must be objects or two-item arrays")
    return PlanEdge(str(source), str(target))


def coerce_plan(
    value: Any, *, request: Mapping[str, Any], task: Mapping[str, Any]
) -> tuple[Plan, list[Task]]:
    """将 Agent 计划草稿转换成领域 Plan 和 Task 列表。
    Raises:
        ValueError: 计划边或数值字段无法转换时。
    """
    original_goal = str(
        request.get("user_message") or task.get("goal") or ""
    ).strip()
    if isinstance(value, tuple) and len(value) == 2:
        value, raw_tasks = value
    else:
        raw_tasks = None
    if isinstance(value, Plan):
        plan = Plan(
            plan_id=value.plan_id,
            goal=original_goal or value.goal,
            task_ids=value.task_ids,
            edges=value.edges,
            max_parallelism=value.max_parallelism,
            status=value.status,
            session_id=value.session_id,
            run_id=value.run_id,
        )
        raw_tasks = raw_tasks or []
    else:
        payload = as_dict(value)
        raw_tasks = raw_tasks or payload.get("tasks", [])
        task_ids = tuple(str(item) for item in payload.get("task_ids", []))
        if not task_ids:
            task_ids = tuple(
                str(item.get("task_id")) for item in raw_tasks if item.get("task_id")
            )
        edges = tuple(_coerce_plan_edge(item) for item in payload.get("edges", []))
        plan = Plan(
            plan_id=str(payload.get("plan_id") or f"plan:{request.get('run_id', '')}"),
            goal=original_goal or str(payload.get("goal") or ""),
            task_ids=task_ids,
            edges=edges,
            max_parallelism=max(1, int(payload.get("max_parallelism", 1))),
            session_id=request.get("session_id"),
            run_id=request.get("run_id"),
        )
    tasks: list[Task] = []
    for item in raw_tasks or []:
        if isinstance(item, Task):
            tasks.append(item)
            continue
        payload = as_dict(item)
        raw_input = payload.get("input", {})
        raw_expected = payload.get("expected_output", {})
        tasks.append(
            Task(
                task_id=str(payload.get("task_id", "")),
                title=str(payload.get("title", payload.get("objective", "Task"))),
                objective=str(payload.get("objective", "")),
                input=(
                    raw_input
                    if isinstance(raw_input, Mapping)
                    else {"value": raw_input}
                ),
                expected_output=(
                    raw_expected
                    if isinstance(raw_expected, Mapping)
                    else {"value": raw_expected}
                ),
                allowed_tools=tuple(
                    dict.fromkeys(
                        str(name).strip()
                        for name in payload.get("allowed_tools", [])
                        if str(name).strip()
                    )
                )
                if isinstance(payload.get("allowed_tools", []), (list, tuple, set))
                else (),
                worker_type="general",
                timeout_seconds=payload.get("timeout_seconds", 3600.0),
            )
        )
    return plan, tasks


def scope_execution_plan(
    plan: Plan,
    tasks: list[Task],
    *,
    task_spec: Mapping[str, Any],
    run_id: str,
    worker_tools: set[str],
) -> tuple[Plan, list[Task]]:
    """把计划中的 ID 和工具权限约束到当前 Run 的可用范围。
    Args:
        plan: 从 Root 输出解析出的计划。
        tasks: 计划中的 Task 定义。
        task_spec: 请求理解阶段生成的任务约束。
        run_id: 当前 Run ID。
        worker_tools: 当前允许 Worker 使用的工具名称。
    Returns:
        计划与任务 ID 一致、且工具已通过白名单过滤的定义。
    Raises:
        ValueError: 用户显式允许但当前不可用的 Worker 工具。
    """
    explicit_tools = task_spec.get("allowed_tools")
    explicit_tool_set = (
        set(explicit_tools)
        if isinstance(explicit_tools, (list, tuple, set, frozenset))
        else None
    )
    if explicit_tool_set is not None:
        unavailable_tools = explicit_tool_set - worker_tools
        if unavailable_tools:
            raise ValueError(
                "explicit_worker_tools_unavailable: "
                + ", ".join(sorted(unavailable_tools))
            )
    # Task ID 在数据库中全局唯一；模型生成的 ID 必须绑定到当前 Run。
    task_id_map = {
        item.task_id: f"{item.task_id}:{run_id}" if run_id else item.task_id
        for item in tasks
    }
    scoped_tasks = [
        _scope_task_tools(
            item,
            task_id=task_id_map[item.task_id],
            explicit_tool_set=explicit_tool_set,
            worker_tools=worker_tools,
        )
        for item in tasks
    ]
    scoped_task_ids = {item.task_id for item in scoped_tasks}
    scoped_edges = tuple(
        PlanEdge(
            task_id_map.get(edge.from_task_id, edge.from_task_id),
            task_id_map.get(edge.to_task_id, edge.to_task_id),
        )
        for edge in plan.edges
        if task_id_map.get(edge.from_task_id, edge.from_task_id)
        in scoped_task_ids
        and task_id_map.get(edge.to_task_id, edge.to_task_id) in scoped_task_ids
    )
    scoped_plan_id = plan.plan_id
    if run_id and not scoped_plan_id.endswith(f":{run_id}"):
        scoped_plan_id = f"{scoped_plan_id}:{run_id}"
    return (
        Plan(
            plan_id=scoped_plan_id,
            goal=plan.goal,
            task_ids=tuple(item.task_id for item in scoped_tasks),
            edges=scoped_edges,
            max_parallelism=plan.max_parallelism,
            status=plan.status,
            session_id=plan.session_id,
            run_id=run_id or plan.run_id,
        ),
        scoped_tasks,
    )


def _scope_task_tools(
    task: Task,
    *,
    task_id: str,
    explicit_tool_set: set[str] | None,
    worker_tools: set[str],
) -> Task:
    """限制单个 Task 的工具清单并绑定作用域化 ID。"""
    requested_tools = (
        *task.allowed_tools,
        *(
            tool_name
            for tool_name in explicit_tool_set or ()
            if tool_name in task.input
        ),
    )
    allowed_tools = tuple(
        dict.fromkeys(
            name
            for name in requested_tools
            if name in worker_tools
            and (explicit_tool_set is None or name in explicit_tool_set)
        )
    )
    return Task(
        task_id=task_id,
        title=task.title,
        objective=task.objective,
        input=task.input,
        expected_output=task.expected_output,
        allowed_tools=allowed_tools,
        worker_type="general",
        timeout_seconds=task.timeout_seconds,
    )
__all__ = [
    "coerce_plan",
    "plan_dict",
    "plan_generation_failure",
    "scope_execution_plan",
]
