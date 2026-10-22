"""Root Plan 版本切换与延期修订策略。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from domain.orchestration import (
    PlanRevision,
    ReviewAction,
    ReviewContext,
    ReviewDecision,
)


def deferred_version_review_decision(
    context: ReviewContext, state: Mapping[str, Any]
) -> ReviewDecision | None:
    """当前版本完成 Review 后，将已声明的下一版本转换为确定性修订。"""
    raw_tasks = _next_version_tasks(context, state)
    identity = _existing_plan_identity(context, state)
    if raw_tasks is None or identity is None:
        return None
    existing_ids, run_id = identity
    new_tasks = _scope_deferred_tasks(raw_tasks, existing_ids, run_id)
    if new_tasks is None:
        return None
    return ReviewDecision(
        action=ReviewAction.REPLAN,
        reason=(
            f"User-requested Plan Version {context.plan_version + 1} is due after "
            "completion and Root Review of the current version."
        ),
        revision=PlanRevision(
            base_version=context.plan_version,
            keep_task_ids=existing_ids,
            cancel_task_ids=(),
            new_tasks=tuple(new_tasks),
        ),
    )


def _next_version_tasks(
    context: ReviewContext, state: Mapping[str, Any]
) -> list[Any] | tuple[Any, ...] | None:
    """仅在当前版本全部完成后返回用户声明的下一版任务。"""
    deferred = state.get("deferred_plan_versions")
    if not isinstance(deferred, Mapping):
        return None
    raw_tasks = deferred.get(str(context.plan_version + 1))
    if not isinstance(raw_tasks, (list, tuple)) or not raw_tasks:
        return None
    results = (*context.previous_results_summary, *context.round_results)
    if context.remaining_tasks or not results:
        return None
    if any(item.get("status") != "completed" for item in results):
        return None
    return raw_tasks


def _existing_plan_identity(
    context: ReviewContext, state: Mapping[str, Any]
) -> tuple[tuple[str, ...], str] | None:
    """读取版本修订需要保留的任务 ID 与当前 Run 身份。"""
    plan = state.get("plan", {})
    request = state.get("request", {})
    dag_nodes = context.full_dag_summary.get("nodes", ())
    dag_task_ids = (
        [
            node.get("task_id")
            for node in dag_nodes
            if isinstance(node, Mapping) and isinstance(node.get("task_id"), str)
        ]
        if isinstance(dag_nodes, (list, tuple))
        else []
    )
    existing_ids = dag_task_ids or (
        plan.get("task_ids", ()) if isinstance(plan, Mapping) else ()
    )
    run_id = request.get("run_id") if isinstance(request, Mapping) else None
    if (
        not isinstance(existing_ids, (list, tuple))
        or not existing_ids
        or any(not isinstance(task_id, str) for task_id in existing_ids)
        or len(existing_ids) != len(set(existing_ids))
        or not isinstance(run_id, str)
        or not run_id
    ):
        return None
    return tuple(existing_ids), run_id


def _scope_deferred_tasks(
    raw_tasks: list[Any] | tuple[Any, ...],
    existing_ids: tuple[str, ...],
    run_id: str,
) -> list[dict[str, Any]] | None:
    """校验下一版任务及依赖，并绑定当前 Run 的任务 ID。"""
    new_task_ids = {
        item.get("task_id")
        for item in raw_tasks
        if isinstance(item, Mapping) and isinstance(item.get("task_id"), str)
    }
    requested_to_scoped = {
        task_id: f"{task_id}:{run_id}" for task_id in new_task_ids
    }
    scoped_new_ids = set(requested_to_scoped.values())
    existing_id_set = set(existing_ids)
    if (
        len(new_task_ids) != len(raw_tasks)
        or existing_id_set & new_task_ids
        or existing_id_set & scoped_new_ids
    ):
        return None
    tasks: list[dict[str, Any]] = []
    # 新版本任务只能依赖既有任务或同版本任务。
    for item in raw_tasks:
        task = _scope_deferred_task(
            item,
            existing_id_set=existing_id_set,
            requested_to_scoped=requested_to_scoped,
            scoped_new_ids=scoped_new_ids,
            run_id=run_id,
        )
        if task is None:
            return None
        tasks.append(task)
    return tasks


def _scope_deferred_task(
    item: Any,
    *,
    existing_id_set: set[str],
    requested_to_scoped: dict[str, str],
    scoped_new_ids: set[str],
    run_id: str,
) -> dict[str, Any] | None:
    """验证并规范化一个延期任务及其依赖。"""
    if not isinstance(item, Mapping):
        return None
    task_id = item.get("task_id")
    title = item.get("title")
    objective = item.get("objective", item.get("instruction"))
    dependencies = item.get("depends_on", ())
    if (
        not isinstance(task_id, str)
        or not task_id.strip()
        or not isinstance(title, str)
        or not isinstance(objective, str)
        or not isinstance(dependencies, (list, tuple))
        or any(not isinstance(dependency, str) for dependency in dependencies)
    ):
        return None
    scoped_dependencies: list[str] = []
    for dependency in dependencies:
        scoped = f"{dependency}:{run_id}"
        if dependency in existing_id_set:
            scoped_dependencies.append(dependency)
        elif scoped in existing_id_set:
            scoped_dependencies.append(scoped)
        elif dependency in requested_to_scoped:
            scoped_dependencies.append(requested_to_scoped[dependency])
        elif dependency in scoped_new_ids:
            scoped_dependencies.append(dependency)
        else:
            return None
        if scoped_dependencies[-1] == requested_to_scoped[task_id]:
            return None
    input_value = item.get("input", {})
    expected_output = item.get("expected_output", {})
    tools = item.get("allowed_tools", ())
    if not isinstance(input_value, Mapping):
        input_value = {"value": input_value}
    if not isinstance(expected_output, Mapping):
        expected_output = {"value": expected_output}
    if not isinstance(tools, (list, tuple)) or any(
        not isinstance(tool, str) for tool in tools
    ):
        return None
    return {
        "task_id": task_id,
        "title": title,
        "objective": objective,
        "input": dict(input_value),
        "expected_output": dict(expected_output),
        "allowed_tools": list(tools),
        "worker_type": str(item.get("worker_type") or "general"),
        "timeout_seconds": item.get("timeout_seconds", 3600.0),
        "depends_on": list(dict.fromkeys(scoped_dependencies)),
    }
__all__ = ["deferred_version_review_decision"]
