"""PostgreSQL fault injection for a committed Root decision before checkpointing."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from itertools import count
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.command_handler import OrchestrationCommandHandler
from application.orchestration.root_workflow import RootWorkflow
from domain.orchestration import ReviewAction, ReviewContext, ReviewDecision
from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    Base,
    OrchestrationCommandModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresOrchestrationCommandRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


class EmptyToolService:
    """为 Review 提供空的当前 Worker 工具目录。"""

    async def list_tools(self):
        return []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_root_review_reuses_decision_after_commit_before_checkpoint() -> None:
    """命令提交后模拟进程崩溃，Root 重放时复用决议且不重复终态事件。

    参数：无。
    返回：无。
    异常：数据库故障或验收断言失败时测试会失败。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"root_review_recovery_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    now = datetime.now(timezone.utc)
    run_id = f"run-{uuid4().hex}"
    session_id = f"session-{run_id}"
    plan_id = f"plan-{run_id}"
    round_id = f"round-{run_id}"
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        tables = [
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            OrchestrationCommandModel.__table__,
            OrchestrationOutboxModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=tables
                )
            )

        async with session_factory() as session, session.begin():
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    message_id="message:" + str(run_id),
                    sequence=1,
                    session_id=session_id,
                    status="running",
                    root_thread_id=f"run:{run_id}",
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            session.add(
                AgentPlanModel(
                    plan_id=plan_id,
                    run_id=run_id,
                    session_id=session_id,
                    status="waiting_review",
                    user_goal="Complete a plan after its only task finishes",
                    max_parallelism=1,
                    current_version=1,
                    active_round_id=round_id,
                    cancellation_epoch=0,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            await session.flush()
            session.add(
                PlanVersionModel(
                    plan_id=plan_id,
                    version=1,
                    dag_json={"nodes": [{"task_id": "task-a"}], "edges": []},
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                PlanRoundModel(
                    round_id=round_id,
                    plan_id=plan_id,
                    plan_version=1,
                    round_number=1,
                    status="completed",
                    expected_task_count=1,
                    terminal_task_count=1,
                    created_at=now,
                    completed_at=now,
                )
            )
            session.add(
                AgentTaskModel(
                    task_id="task-a",
                    plan_id=plan_id,
                    run_id=run_id,
                    title="Task A",
                    objective="Complete Task A",
                    input_json="{}",
                    expected_output_json="{}",
                    allowed_tools_json="[]",
                    worker_type="general",
                    timeout_seconds=60,
                    status="completed",
                    output_json=json.dumps({"result": "done"}),
                    execution_generation=1,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )

        ids = count()
        repository = PostgresOrchestrationCommandRepository(
            session_factory,
            id_factory=lambda: f"generated-{next(ids)}",
            clock=lambda: now,
        )
        decision = ReviewDecision(
            ReviewAction.FINISH,
            "The completed result is ready.",
            final_status="completed",
        )

        class ReviewContexts:
            """为故障窗口测试提供当前 Round 的固定审查上下文。"""

            async def load_review_context(
                self, requested_round_id: str
            ) -> ReviewContext:
                assert requested_round_id == round_id
                return ReviewContext(
                    original_goal="Complete a plan after its only task finishes",
                    plan_id=plan_id,
                    plan_version=1,
                    round_id=round_id,
                    full_dag_summary={"nodes": [{"task_id": "task-a"}], "edges": []},
                    round_results=({"task_id": "task-a", "status": "completed"},),
                )

        class Agent:
            """统计 Root Review 调用，确保重放不再次请求决策。"""

            def __init__(self) -> None:
                self.review_calls = 0

            async def review(self, *_args, **_kwargs):
                self.review_calls += 1
                return decision

        class CrashAfterCommit:
            """在真实事务提交后、Root 节点返回前注入一次进程中断。"""

            def __init__(self) -> None:
                self.handler = OrchestrationCommandHandler(repository)
                self.crash_once = True

            async def load_review_decision(self, **kwargs):
                return await self.handler.load_review_decision(**kwargs)

            async def apply_review_decision(self, command, stored_decision):
                receipt = await self.handler.apply_review_decision(
                    command, stored_decision
                )
                if self.crash_once:
                    self.crash_once = False
                    raise RuntimeError("simulated_process_crash_after_review_commit")
                return receipt

        agent = Agent()
        commands = CrashAfterCommit()
        workflow = RootWorkflow(
            SimpleNamespace(
                review_context_repository=ReviewContexts(),
                orchestration_command_handler=commands,
                agent=agent,
                tool_service=EmptyToolService(),
            )
        )
        state = {
            "request": {"run_id": run_id, "session_id": session_id},
            "root_wait": {
                "type": "round_wait",
                "run_id": run_id,
                "plan_id": plan_id,
                "round_id": round_id,
                "plan_version": 1,
            },
            "resume_value": {
                "type": "round_completed",
                "run_id": run_id,
                "plan_id": plan_id,
                "round_id": round_id,
                "plan_version": 1,
                "interrupt_id": "interrupt-before-checkpoint",
            },
        }

        with pytest.raises(
            RuntimeError, match="simulated_process_crash_after_review_commit"
        ):
            await workflow.review_completed_round(state)

        async with session_factory() as session:
            committed_plan = await session.get(AgentPlanModel, plan_id)
            committed_command_count = await session.scalar(
                select(func.count())
                .select_from(OrchestrationCommandModel)
                .where(
                    OrchestrationCommandModel.idempotency_key
                    == f"root-review:{round_id}"
                )
            )
        assert committed_plan is not None
        assert committed_plan.status == "completed"
        assert committed_command_count == 1

        replay_result = await workflow.review_completed_round(state)

        async with session_factory() as session:
            final_plan = await session.get(AgentPlanModel, plan_id)
            command_count = await session.scalar(
                select(func.count())
                .select_from(OrchestrationCommandModel)
                .where(
                    OrchestrationCommandModel.idempotency_key
                    == f"root-review:{round_id}"
                )
            )
            terminal_event_count = await session.scalar(
                select(func.count())
                .select_from(OrchestrationOutboxModel)
                .where(OrchestrationOutboxModel.event_type == "PlanCompleted")
            )

        assert replay_result["review_action"] == "FINISH"
        assert agent.review_calls == 1
        assert final_plan is not None and final_plan.status == "completed"
        assert command_count == 1
        assert terminal_event_count == 1
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
