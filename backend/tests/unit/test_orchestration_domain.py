"""阶段 6 首个 Plan/Task/DAG 切片测试。"""

from __future__ import annotations

import pytest

from application.orchestration import OrchestrationService, PlanNotFoundError
from domain.orchestration import (
    Plan,
    PlanDAG,
    PlanEdge,
    PlanValidationError,
    PlanValidator,
    Task,
    TaskStatus,
)


def plan(*edges: tuple[str, str]) -> Plan:
    return Plan(
        plan_id="plan-1",
        goal="inspect",
        task_ids=("a", "b", "c"),
        edges=tuple(PlanEdge(source, target) for source, target in edges),
        max_parallelism=2,
    )


def tasks(*ids: str) -> list[Task]:
    return [Task(task_id=item, title=item, objective=f"do {item}") for item in ids]


def test_validator_accepts_plan_and_dag_is_deterministic() -> None:
    value = plan(("a", "c"), ("b", "c"))
    PlanValidator().validate(value, tasks("a", "b", "c"))
    dag = PlanDAG(value)
    assert dag.ready_tasks({"a": "pending", "b": "pending", "c": "pending"}) == ["a", "b"]
    assert dag.ready_tasks({"a": "done", "b": "done", "c": "pending"}) == ["c"]


def test_dag_propagates_dependency_failure_recursively() -> None:
    value = Plan(
        plan_id="plan-1",
        goal="",
        task_ids=("a", "b", "c"),
        edges=(PlanEdge("a", "b"), PlanEdge("b", "c")),
    )
    states = {"a": "failed", "b": "pending", "c": "pending"}
    assert PlanDAG(value).propagate_dependency_failures(states) == ["b", "c"]
    assert PlanDAG(value).all_terminal(states)


@pytest.mark.parametrize(
    "value, expected",
    [
        (plan(("a", "a")), "self_dependency"),
        (plan(("a", "b"), ("a", "b")), "duplicate_edge"),
        (plan(("a", "b"), ("b", "a")), "cycle"),
        (plan(("missing", "a")), "unknown_edge_node"),
    ],
)
def test_validator_rejects_invalid_graph(value: Plan, expected: str) -> None:
    with pytest.raises(PlanValidationError) as error:
        PlanValidator().validate(value, tasks("a", "b", "c"))
    assert expected in {item["code"] for item in error.value.errors}


def test_validator_enforces_worker_and_tool_boundaries() -> None:
    value = plan()
    values = [Task("a", "a", "a", allowed_tools=("write",), worker_type="special")]
    with pytest.raises(PlanValidationError) as error:
        PlanValidator().validate(value, values)
    codes = {item["code"] for item in error.value.errors}
    assert {"node_task_mismatch", "unknown_worker_type", "unknown_tool"} <= codes


@pytest.mark.asyncio
async def test_orchestration_service_validates_before_persisting() -> None:
    class Repository:
        def __init__(self):
            self.created = None

        async def create(self, value, task_values):
            self.created = (value, task_values)
            return value

        async def get(self, plan_id):
            return self.created if self.created and self.created[0].plan_id == plan_id else None

    repository = Repository()
    service = OrchestrationService(repository)
    value = plan(("a", "c"), ("b", "c"))
    await service.create_plan(value, tasks("a", "b", "c"))
    assert repository.created is not None

    with pytest.raises(PlanNotFoundError):
        await service.get_plan("missing")
