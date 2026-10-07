"""Plan DAG 的纯状态查询和依赖失败传播。"""

from __future__ import annotations

from collections.abc import Mapping

from .entities import FAILED_TASK_STATUSES, TERMINAL_TASK_STATUSES, Plan, TaskResult, TaskStatus


class PlanDAG:
    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.task_ids = tuple(sorted(plan.task_ids))
        self.dependencies: dict[str, set[str]] = {task_id: set() for task_id in self.task_ids}
        self.children: dict[str, set[str]] = {task_id: set() for task_id in self.task_ids}
        for edge in plan.edges:
            self.dependencies.setdefault(edge.to_task_id, set()).add(edge.from_task_id)
            self.children.setdefault(edge.from_task_id, set()).add(edge.to_task_id)

    def dependencies_of(self, task_id: str) -> set[str]:
        return set(self.dependencies[task_id])

    def children_of(self, task_id: str) -> set[str]:
        return set(self.children[task_id])

    def ready_tasks(self, states: Mapping[str, TaskStatus | str]) -> list[str]:
        return [
            task_id
            for task_id in self.task_ids
            if str(states.get(task_id)) == TaskStatus.PENDING.value
            and all(str(states.get(parent)) == TaskStatus.DONE.value for parent in self.dependencies[task_id])
        ]

    def propagate_dependency_failures(self, states: dict[str, TaskStatus | str]) -> list[str]:
        cancelled: list[str] = []
        changed = True
        while changed:
            changed = False
            for task_id in self.task_ids:
                if str(states.get(task_id)) != TaskStatus.PENDING.value:
                    continue
                if any(str(states.get(parent)) in {item.value for item in FAILED_TASK_STATUSES} for parent in self.dependencies[task_id]):
                    states[task_id] = TaskStatus.CANCELLED
                    cancelled.append(task_id)
                    changed = True
        return cancelled

    def all_terminal(self, states: Mapping[str, TaskStatus | str]) -> bool:
        terminal = {item.value for item in TERMINAL_TASK_STATUSES}
        return bool(self.task_ids) and all(str(states.get(task_id)) in terminal for task_id in self.task_ids)

    def upstream_results(self, task_id: str, results: Mapping[str, TaskResult]) -> dict[str, TaskResult]:
        return {parent: results[parent] for parent in sorted(self.dependencies[task_id]) if parent in results}

