"""Root Plan 版本切换与延期修订策略。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from domain.orchestration import (
    PlanRevision,
    ReviewAction,
    ReviewContext,
    ReviewDecision,
)

from .root_decision import PlanTaskDraft


def partition_plan_versions(
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """首轮只提交 V1，将显式声明的后续版本留给对应 Review 提交。

    版本号是执行边界：依赖只能指向相同或更早版本，后续版本必须等待
    前一版本完成后再进入已有 Revision 流程。
    """
    raw_tasks = plan.get("tasks")
    if not isinstance(raw_tasks, (list, tuple)) or not raw_tasks:
        raise ValueError("root_plan_tasks_invalid")
    has_explicit_versions = any(
        isinstance(task, Mapping) and "plan_version" in task
        for task in raw_tasks
    )

    versions: dict[str, int] = {}
    tasks_by_version: dict[int, list[dict[str, Any]]] = {}
    for raw_task in raw_tasks:
        if not isinstance(raw_task, Mapping):
            raise ValueError("root_plan_task_invalid")
        task_id = raw_task.get("task_id")
        version = raw_task.get("plan_version", 1)
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or task_id in versions
            or (has_explicit_versions and "plan_version" not in raw_task)
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise ValueError("root_plan_version_contract_invalid")
        versions[task_id] = version
        tasks_by_version.setdefault(version, []).append(dict(raw_task))

    version_numbers = sorted(tasks_by_version)
    if version_numbers != list(range(1, version_numbers[-1] + 1)):
        raise ValueError("root_plan_versions_must_be_contiguous")

    raw_edges = plan.get("edges", ())
    if not isinstance(raw_edges, (list, tuple)):
        raise ValueError("root_plan_edges_invalid")
    incoming: dict[str, list[str]] = {task_id: [] for task_id in versions}
    normalized_edges: list[tuple[str, str]] = []
    for edge in raw_edges:
        if not isinstance(edge, Mapping):
            raise ValueError("root_plan_edge_invalid")
        source = edge.get("from_task_id")
        target = edge.get("to_task_id")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in versions
            or target not in versions
            or versions[source] > versions[target]
        ):
            raise ValueError("root_plan_version_dependency_invalid")
        incoming[target].append(source)
        normalized_edges.append((source, target))

    raw_tasks_by_id = {str(task["task_id"]): task for task in raw_tasks}
    for task_id, raw_task in raw_tasks_by_id.items():
        dependencies = raw_task.get("depends_on", ())
        if not isinstance(dependencies, (list, tuple)) or any(
            not isinstance(dependency, str) or dependency not in versions
            for dependency in dependencies
        ):
            raise ValueError("root_plan_task_dependencies_invalid")
        if any(
            versions[dependency] > versions[task_id]
            for dependency in dependencies
        ):
            raise ValueError("root_plan_version_dependency_invalid")
        incoming[task_id].extend(dependencies)

    current_ids = {
        task_id for task_id, version in versions.items() if version == 1
    }
    current_tasks: list[dict[str, Any]] = []
    deferred: dict[str, list[dict[str, Any]]] = {}
    for version in version_numbers:
        for raw_task in tasks_by_version[version]:
            task_id = str(raw_task["task_id"])
            task = {
                key: value
                for key, value in raw_task.items()
                if key != "plan_version"
            }
            if version == 1:
                task.pop("depends_on", None)
                current_tasks.append(task)
            else:
                task["depends_on"] = list(dict.fromkeys(incoming[task_id]))
                deferred.setdefault(str(version), []).append(task)

    current_edges = {
        (source, target)
        for source, target in normalized_edges
        if source in current_ids and target in current_ids
    }
    for task in current_tasks:
        task_id = str(task["task_id"])
        current_edges.update(
            (source, task_id)
            for source in raw_tasks_by_id[task_id].get("depends_on", ())
            if source in current_ids
        )
    current_plan = dict(plan)
    current_plan["tasks"] = current_tasks
    current_plan["edges"] = [
        {"from_task_id": source, "to_task_id": target}
        for source, target in sorted(current_edges)
    ]
    return current_plan, deferred


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
    try:
        task = PlanTaskDraft.model_validate(item)
    except ValidationError:
        return None
    task_id = task.task_id
    scoped_dependencies: list[str] = []
    for dependency in task.depends_on:
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
    return {
        **task.model_dump(mode="json", exclude={"plan_version", "depends_on"}),
        "task_id": requested_to_scoped[task_id],
        "depends_on": list(dict.fromkeys(scoped_dependencies)),
    }
__all__ = ["deferred_version_review_decision", "partition_plan_versions"]
