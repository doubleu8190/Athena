"""中心化 Multi-Agent 契约与权限策略测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from athena.infrastructure.sqlite.database import Database
from athena.runtime.orchestration import (
    ExecutionPlan,
    TaskSpec,
    TaskStatus,
    ToolPolicy,
    WorkerResult,
)
from athena.runtime.orchestration.contracts import ensure_task_transition


def _task(task_id: str = "task-1", plan_id: str = "plan-1") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        plan_id=plan_id,
        title="检查模块",
        objective="返回模块检查结果",
        expected_output={"type": "object"},
    )


def test_v1_plan_accepts_independent_tasks() -> None:
    plan = ExecutionPlan(
        plan_id="plan-1",
        root_run_id="run-1",
        goal="并行检查",
        tasks=[_task("task-1"), _task("task-2")],
    )

    assert [task.task_id for task in plan.tasks] == ["task-1", "task-2"]


def test_v1_plan_rejects_duplicate_task_ids() -> None:
    with pytest.raises(ValidationError, match="task_id must be unique"):
        ExecutionPlan(
            plan_id="plan-1",
            root_run_id="run-1",
            goal="并行检查",
            tasks=[_task(), _task()],
        )


def test_v1_task_rejects_dependencies() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(
            task_id="task-2",
            plan_id="plan-1",
            title="依赖任务",
            objective="不应被 V1 接受",
            depends_on=["task-1"],
        )


def test_terminal_task_status_cannot_transition() -> None:
    with pytest.raises(ValueError, match="invalid task transition"):
        ensure_task_transition(TaskStatus.COMPLETED, TaskStatus.QUEUED)


def test_worker_policy_always_removes_delegation_tools() -> None:
    policy = ToolPolicy.for_worker(
        ["read_local_file", "spawn_sub_agent", "spawn_parallel_agents"]
    )

    assert policy.permits("read_local_file")
    assert not policy.permits("spawn_sub_agent")
    assert not policy.permits("spawn_parallel_agents")


def test_completed_worker_result_requires_structured_output() -> None:
    with pytest.raises(ValidationError, match="must contain output"):
        WorkerResult(
            plan_id="plan-1",
            task_id="task-1",
            run_id="worker-1",
            status="completed",
        )


@pytest.mark.asyncio
async def test_task_ledger_claims_and_saves_result(tmp_path) -> None:
    database = Database(str(tmp_path / "orchestration.db"))
    await database.connect()
    session = await database.sessions.create("session-1", "Orchestration")
    plan = ExecutionPlan(
        plan_id="plan-1",
        root_run_id="root-1",
        goal="执行检查",
        tasks=[_task()],
    )
    await database.orchestration.create_plan(session.id, plan)

    claimed = await database.orchestration.claim_next_task("plan-1", "dispatcher-1")
    assert claimed is not None
    assert claimed.status == TaskStatus.CLAIMED.value
    assert claimed.attempt == 1
    assert await database.orchestration.transition_task(
        "task-1",
        TaskStatus.CLAIMED,
        TaskStatus.RUNNING,
        worker_run_id="worker-1",
    )

    result = WorkerResult(
        plan_id="plan-1",
        task_id="task-1",
        run_id="worker-1",
        status="completed",
        output={"summary": "ok"},
    )
    assert await database.orchestration.save_result(result) is True
    assert await database.orchestration.save_result(result) is False
    assert await database.orchestration.transition_task(
        "task-1", TaskStatus.RUNNING, TaskStatus.COMPLETED
    )

    await database.close()
