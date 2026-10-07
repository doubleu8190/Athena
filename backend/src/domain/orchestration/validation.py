"""确定性的 Plan/Task/DAG 结构校验。"""

from __future__ import annotations

from collections.abc import Iterable

from .entities import Plan, Task


class PlanValidationError(ValueError):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("; ".join(item["message"] for item in errors))


class PlanValidator:
    def __init__(
        self,
        *,
        registered_worker_types: Iterable[str] = ("general",),
        legal_tools: Iterable[str] = (),
        max_parallelism: int | None = None,
    ) -> None:
        self._worker_types = set(registered_worker_types)
        self._legal_tools = set(legal_tools)
        self._max_parallelism = max_parallelism

    def validate(self, plan: Plan, tasks: Iterable[Task]) -> None:
        errors: list[dict[str, str]] = []
        task_list = list(tasks)
        task_ids = [task.task_id for task in task_list]
        if not plan.task_ids:
            errors.append({"code": "empty_plan", "message": "Plan must contain at least one task"})
        if len(plan.task_ids) != len(set(plan.task_ids)):
            errors.append({"code": "duplicate_node", "message": "task ID must be unique"})
        if len(task_ids) != len(set(task_ids)):
            errors.append({"code": "duplicate_task", "message": "task definition ID must be unique"})
        if set(plan.task_ids) != set(task_ids):
            errors.append({"code": "node_task_mismatch", "message": "Task definitions and Plan nodes must match exactly"})
        if plan.max_parallelism < 1 or plan.max_parallelism > 128 or (
            self._max_parallelism is not None and plan.max_parallelism > self._max_parallelism
        ):
            errors.append({"code": "invalid_max_parallelism", "message": "max_parallelism is invalid"})

        known = set(plan.task_ids)
        edges: set[tuple[str, str]] = set()
        for edge in plan.edges:
            pair = (edge.from_task_id, edge.to_task_id)
            if edge.from_task_id not in known or edge.to_task_id not in known:
                errors.append({"code": "unknown_edge_node", "message": f"edge references unknown task: {pair}"})
            if edge.from_task_id == edge.to_task_id:
                errors.append({"code": "self_dependency", "message": f"task cannot depend on itself: {edge.from_task_id}"})
            if pair in edges:
                errors.append({"code": "duplicate_edge", "message": f"duplicate edge: {pair}"})
            edges.add(pair)
        if not self._acyclic(known, edges):
            errors.append({"code": "cycle", "message": "Plan contains a cycle"})

        for task in task_list:
            if not task.title.strip():
                errors.append({"code": "invalid_title", "message": f"task title is required: {task.task_id}"})
            if not task.objective.strip():
                errors.append({"code": "invalid_objective", "message": f"task objective is required: {task.task_id}"})
            if task.worker_type not in self._worker_types:
                errors.append({"code": "unknown_worker_type", "message": f"worker type is not registered: {task.worker_type}"})
            if any(tool not in self._legal_tools for tool in task.allowed_tools):
                errors.append({"code": "unknown_tool", "message": f"task uses an unregistered tool: {task.task_id}"})
        if errors:
            raise PlanValidationError(errors)

    @staticmethod
    def _acyclic(nodes: set[str], edges: set[tuple[str, str]]) -> bool:
        indegree = {node: 0 for node in nodes}
        children = {node: [] for node in nodes}
        for source, target in edges:
            if source in nodes and target in nodes:
                indegree[target] += 1
                children[source].append(target)
        queue = sorted(node for node, degree in indegree.items() if degree == 0)
        processed = 0
        while queue:
            node = queue.pop(0)
            processed += 1
            for child in sorted(children[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
            queue.sort()
        return processed == len(nodes)

