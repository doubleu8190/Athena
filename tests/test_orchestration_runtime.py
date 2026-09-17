"""计划物化、调度与 Worker 的协调行为测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from athena.infrastructure.sqlite.database import Database
from athena.runtime.orchestration import ExecutionPlan, TaskSpec
from athena.runtime.orchestration.plan_dispatcher import PlanDispatcher
from athena.runtime.orchestration.plan_materializer import PlanMaterializer
from athena.runtime.orchestration.structured_llm import StructuredLLMService
from athena.runtime.orchestration.worker import WorkerExecutor
from athena.runtime.orchestration.events import OrchestrationEventPublisher
from athena.config.settings import Settings


def _task(task_id: str = "task-1", plan_id: str = "plan-1") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        plan_id=plan_id,
        title="检查模块",
        objective="返回模块检查结果",
        expected_output={"type": "object"},
    )


class _FakeWorker:
    async def execute(self, **kwargs):
        from athena.runtime.orchestration import WorkerResult

        return WorkerResult(
            plan_id=kwargs["task"].plan_id,
            task_id=kwargs["task"].task_id,
            run_id=kwargs["run_id"],
            status="completed",
            output={"summary": "ok"},
        )


@pytest.mark.asyncio
async def test_plan_materializer_persists_structured_plan(tmp_path) -> None:
    database = Database(str(tmp_path / "planner.db"))
    await database.connect()
    session = await database.sessions.create("session-1", "Plan")

    from athena.runtime.orchestration import OrchestrationEventPublisher

    materializer = PlanMaterializer(
        database, OrchestrationEventPublisher(AsyncMock())
    )
    plan = await materializer.materialize_submission(
        session.id,
        "root-1",
        "检查模块",
        {
            "plan_id": "plan-1",
            "tasks": [
                {
                    "task_id": "task-1",
                    "title": "检查模块",
                    "objective": "返回模块检查结果",
                    "allowed_tools": ["read_local_file"],
                }
            ],
        },
        ["read_local_file"],
    )

    assert plan.plan_id == "plan-1"
    assert plan.tasks[0].plan_id == "plan-1"
    assert plan.tasks[0].allowed_tools == ["read_local_file"]
    await database.close()


@pytest.mark.asyncio
async def test_plan_dispatcher_claims_and_runs_independent_task(tmp_path) -> None:
    database = Database(str(tmp_path / "dispatch.db"))
    await database.connect()
    session = await database.sessions.create("session-1", "Dispatch")
    plan = ExecutionPlan(
        plan_id="plan-1",
        root_run_id="root-1",
        goal="检查模块",
        tasks=[_task()],
    )
    await database.orchestration.create_plan(session.id, plan)

    dispatcher = PlanDispatcher(
        database, _FakeWorker(), OrchestrationEventPublisher(AsyncMock())
    )
    dispatcher.register_plan_session("plan-1", "session-1")
    results = await dispatcher.execute_plan(plan, session.id)

    assert set(results) == {"task-1"}
    assert results["task-1"].status == "completed"
    await database.close()


@pytest.mark.asyncio
async def test_recovery_requeues_stale_claimed_task(tmp_path) -> None:
    """过期租约的 CLAIMED 任务会被恢复器重新排队。"""

    from datetime import datetime, timedelta

    from sqlalchemy import text

    from athena.runtime.recovery_reconciler import RecoveryReconciler

    database = Database(str(tmp_path / "recovery.db"))
    await database.connect()
    session = await database.sessions.create("session-1", "Recovery")
    plan = ExecutionPlan(
        plan_id="plan-1",
        root_run_id="root-1",
        goal="检查模块",
        tasks=[_task()],
    )
    await database.orchestration.create_plan(session.id, plan)
    await database.orchestration.claim_next_task("plan-1", "old-owner")

    # 直接把 claimed_at 改为过期时间，模拟进程崩溃后遗留的租约。
    from athena.infrastructure.sqlite.engine import get_core_session

    expired_at = (datetime.now() - timedelta(seconds=3600)).isoformat()
    async with get_core_session() as db:
        await db.execute(
            text("UPDATE agent_tasks SET claimed_at=:claimed_at WHERE task_id='task-1'"),
            {"claimed_at": expired_at},
        )
        await db.commit()

    reconciler = RecoveryReconciler(
        AsyncMock(),
        orchestration=database.orchestration,
    )
    result = await reconciler.reconcile()

    task = await database.orchestration.get_task_record("task-1")
    assert task is not None
    assert task.status == "queued"
    assert task.lease_owner is None
    assert result["reclaimed_tasks"] == 1
    await database.close()
