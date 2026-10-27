"""PostgreSQL acceptance for Root checkpoint registration recovery."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.conninfo import make_conninfo
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.langgraph.graphs.state import RootGraphState
from application.orchestration.langgraph.runtime.root_adapter import LangGraphRootGraph
from infrastructure.persistence.postgres.checkpointer import PostgresCheckpointer
from infrastructure.persistence.postgres.graph_thread_lock import PostgresGraphThreadLock
from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    Base,
    PlanRoundModel,
    PlanVersionModel,
    ProjectModel,
    RootResumeJobModel,
    SessionModel,
)
from infrastructure.persistence.postgres.repositories.root_resume_job_repository import (
    PostgresRootResumeJobRepository,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from workers.root_resume_dispatcher import RootResumeDispatcher
from shared.contracts.runs import RoundCompletedResumeValueSchema


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_root_resume_recovers_checkpoint_written_before_run_arm() -> None:
    """从 PG checkpoint 找回 Root wait 并恢复未登记的 Resume Job。

    参数：无。
    返回：无。
    异常：数据库故障或验收断言失败时测试会失败。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"root_checkpoint_recovery_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    checkpoint_options: dict[str, str | int] = {
        "host": base_url.host or "",
        "port": base_url.port or 5432,
        "dbname": base_url.database or "",
        "user": base_url.username or "",
        "password": base_url.password or "",
        "options": f"-c search_path={schema}",
    }
    checkpoint_options.update(
        {
            name: value
            for name, value in base_url.query.items()
            if isinstance(value, (str, int)) and name != "options"
        }
    )
    checkpoint_connection_string = make_conninfo(**checkpoint_options)
    checkpointers: list[PostgresCheckpointer] = []
    checkpointer = PostgresCheckpointer(checkpoint_connection_string)
    checkpointers.append(checkpointer)
    schema_created = False
    now = datetime.now(timezone.utc)
    run_id = f"run-{uuid4().hex}"
    session_id = f"session-{run_id}"
    plan_id = f"plan-{run_id}"
    round_id = f"round-{run_id}"
    job_id = f"resume-{run_id}"
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        ProjectModel.__table__,
                        SessionModel.__table__,
                        AgentRunModel.__table__,
                        AgentPlanModel.__table__,
                        PlanVersionModel.__table__,
                        PlanRoundModel.__table__,
                        RootResumeJobModel.__table__,
                    ],
                )
            )
        await checkpointer.start()

        async with session_factory() as session, session.begin():
            session.add(ProjectModel(
                id=f"project-{run_id}", name="Root checkpoint recovery",
                created_at=now.isoformat(), updated_at=now.isoformat(),
            ))
            session.add(SessionModel(
                id=session_id, project_id=f"project-{run_id}",
                title="Root checkpoint recovery", active_run_id=run_id,
                last_run_sequence=1, created_at=now.isoformat(),
                updated_at=now.isoformat(), last_activity_at=now.isoformat(),
            ))
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    message_id="message:" + str(run_id),
                    sequence=1,
                    session_id=session_id,
                    status="running",
                    root_thread_id=f"run:{run_id}",
                    root_wait_generation=1,
                    root_wait_round_id=round_id,
                    root_wait_plan_version=1,
                    root_wait_armed_at=None,
                    root_wait_interrupt_id=None,
                    root_wait_checkpoint_id=None,
                    cancellation_epoch=0,
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
                    user_goal="Resume Root after recovering its checkpoint identity",
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
                    dag_json={"nodes": [], "edges": []},
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
            await session.flush()
            session.add(
                RootResumeJobModel(
                    job_id=job_id,
                    dedupe_key=f"round:{round_id}:root-resume:1",
                    run_id=run_id,
                    plan_id=plan_id,
                    round_id=round_id,
                    root_wait_generation=1,
                    status="pending",
                    attempt=0,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )

        async def root_wait_node(state: RootGraphState) -> dict[str, object]:
            """暂停到对应 Round 完成后再恢复。

            参数：state 是 LangGraph 持久化的 Root 执行状态。
            返回：包含已接收恢复决定的节点结果。
            异常：LangGraph 会将未恢复的 interrupt 持久化在 checkpoint 中。
            """
            resume_value = interrupt(
                {
                    "type": "round_wait",
                    "run_id": run_id,
                    "plan_id": plan_id,
                    "round_id": round_id,
                    "plan_version": 1,
                }
            )
            return {"resume_value": resume_value, "waiting": False}

        graph_builder = StateGraph(RootGraphState)
        graph_builder.add_node("root_wait", root_wait_node)
        graph_builder.add_edge(START, "root_wait")
        graph_builder.add_edge("root_wait", END)
        compiled_graph = graph_builder.compile(checkpointer=checkpointer.get_saver())
        graph_config = {"configurable": {"thread_id": f"run:{run_id}"}}
        paused = await compiled_graph.ainvoke(
            {"request": {"session_id": session_id, "run_id": run_id}},
            config=graph_config,
        )
        assert len(paused["__interrupt__"]) == 1
        checkpoint_snapshot = await compiled_graph.aget_state(graph_config)
        checkpoint_id = checkpoint_snapshot.config["configurable"]["checkpoint_id"]
        interrupt_id = checkpoint_snapshot.tasks[0].interrupts[0].id

        await checkpointer.stop()
        restarted_checkpointer = PostgresCheckpointer(checkpoint_connection_string)
        checkpointers.append(restarted_checkpointer)
        await restarted_checkpointer.start()
        compiled_graph = graph_builder.compile(
            checkpointer=restarted_checkpointer.get_saver()
        )
        root_graph = LangGraphRootGraph(
            workflow=None,
            dependencies=None,
            checkpointer=restarted_checkpointer,
            thread_lock=PostgresGraphThreadLock(checkpoint_connection_string),
        )
        root_graph._graph = compiled_graph
        root_jobs = PostgresRootResumeJobRepository(session_factory, clock=lambda: now)
        lease = await root_jobs.claim_next("root-resumer-before-restart", lease_seconds=60)
        assert lease is not None and lease.job_id == job_id
        assert lease.interrupt_id is None and lease.checkpoint_id is None

        class Execution:
            """通过同一 PG checkpoint 执行一次 Root 恢复命令。"""

            async def execute(self, command):
                """恢复图并持久化 Run 的终态。

                参数：command 是 dispatcher 构造的 Root Resume 命令。
                返回：Root 图恢复后的状态。
                异常：图恢复或数据库写入失败时透传异常。
                """
                result = await root_graph.invoke(
                    session_id=session_id,
                    run_id=run_id,
                    user_message="",
                    resume_value=RoundCompletedResumeValueSchema.from_domain(
                        command.payload.resume_value
                    ).model_dump(mode="json"),
                )
                async with session_factory() as session, session.begin():
                    run = await session.get(AgentRunModel, run_id)
                    assert run is not None
                    run.status = "completed"
                    run.updated_at = datetime.now(timezone.utc).isoformat()
                return result

        dispatcher = RootResumeDispatcher(
            root_jobs,
            root_graph,
            Execution(),
            PostgresRunRepository(session_factory),
            worker_id="root-resumer-before-restart",
        )
        await dispatcher._resume(lease)

        async with session_factory() as session:
            run = await session.get(AgentRunModel, run_id)
            job = await session.get(RootResumeJobModel, job_id)
            completed_job_id = await session.scalar(
                select(RootResumeJobModel.job_id).where(
                    RootResumeJobModel.status == "completed"
                )
            )
        assert run is not None and run.status == "completed"
        assert run.root_wait_interrupt_id == interrupt_id
        assert run.root_wait_checkpoint_id == checkpoint_id
        assert job is not None and job.status == "completed"
        assert completed_job_id == job_id
    finally:
        for value in reversed(checkpointers):
            await value.stop()
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
