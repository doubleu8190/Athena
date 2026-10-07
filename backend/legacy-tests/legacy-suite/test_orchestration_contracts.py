"""Tests for the target Root/Worker contracts and tool policy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from athena.planning import PlanDAG, PlanDocument, PlanValidator
from athena.planning.models import TaskDefinition
from athena.runtime.orchestration import ToolPolicy


def _task(task_id: str) -> TaskDefinition:
    return TaskDefinition(
        task_id=task_id,
        title="检查模块",
        objective="返回模块检查结果",
        expected_output={"type": "object"},
    )


def test_plan_requires_nodes_and_task_definitions_to_match() -> None:
    plan = PlanDocument(nodes=[{"task_id": "task-1"}, {"task_id": "task-2"}])
    PlanValidator().validate(plan, [_task("task-1"), _task("task-2")], max_parallelism=2)

    with pytest.raises(ValueError, match="must match exactly"):
        PlanValidator().validate(plan, [_task("task-1")], max_parallelism=1)


def test_plan_rejects_duplicate_edges_and_cycles() -> None:
    plan = PlanDocument(
        nodes=[{"task_id": "a"}, {"task_id": "b"}],
        edges=[{"from": "a", "to": "b"}, {"from": "a", "to": "b"}],
    )
    with pytest.raises(ValueError, match="duplicate edge"):
        PlanValidator().validate(plan, [_task("a"), _task("b")])

    cyclic = PlanDocument(
        nodes=[{"task_id": "a"}, {"task_id": "b"}],
        edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    )
    with pytest.raises(ValueError, match="cycle"):
        PlanValidator().validate(cyclic, [_task("a"), _task("b")])


def test_worker_policy_removes_delegation_tools() -> None:
    policy = ToolPolicy.for_worker(
        ["read_local_file", "spawn_sub_agent", "spawn_parallel_agents"]
    )

    assert policy.permits("read_local_file")
    assert not policy.permits("spawn_sub_agent")
    assert not policy.permits("spawn_parallel_agents")


def test_plan_dag_ready_tasks_are_deterministic() -> None:
    plan = PlanDocument(
        nodes=[{"task_id": "a"}, {"task_id": "b"}, {"task_id": "c"}],
        edges=[{"from": "a", "to": "c"}, {"from": "b", "to": "c"}],
    )
    dag = PlanDAG(plan)
    assert dag.ready_tasks({"a": "pending", "b": "pending", "c": "pending"}) == ["a", "b"]


def test_task_definition_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TaskDefinition(task_id="task-1", title="x", objective="y", depends_on=["task-0"])
