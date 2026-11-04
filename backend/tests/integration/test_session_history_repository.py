"""PostgreSQL 会话历史只读取终态 Run 的原子快照测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.events import EventStreamService
from application.events.stream_service import decode_sync_token
from application.sessions.history_port import SessionTurnSnapshot
from infrastructure.persistence.postgres.models import (
    AgentEventModel,
    AgentRunModel,
    Base,
    MessageModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    ProjectModel,
    RunProgressModel,
    SessionModel,
    WorkerContractModel,
    WorkerDisplayResultModel,
    WorkerInstanceModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresSessionHistoryRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_TABLES = [
    ProjectModel.__table__,
    SessionModel.__table__,
    MessageModel.__table__,
    AgentRunModel.__table__,
    AgentEventModel.__table__,
    WorkerContractModel.__table__,
    WorkerInstanceModel.__table__,
    OrchestratorPlanModel.__table__,
    OrchestratorTaskModel.__table__,
    OrchestratorExecutionModel.__table__,
    WorkerDisplayResultModel.__table__,
    RunProgressModel.__table__,
]


async def _engines(schema: str):
    assert DATABASE_URL
    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        # 临时 schema 只隔离业务表；extensions 与 vector 等公共类型仍位于 public。
        url.update_query_dict({"options": f"-c search_path={schema},public"})
    )
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    async with test_engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=_TABLES,
                checkfirst=False,
            )
        )
    return admin_engine, test_engine


async def _dispose(admin_engine, test_engine, schema: str) -> None:
    await test_engine.dispose()
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await admin_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_history_reads_only_terminal_runs_and_every_execution_retry() -> None:
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"session_history_{uuid4().hex}"
    admin_engine, test_engine = await _engines(schema)
    try:
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        project_id = f"project-{uuid4().hex}"
        session_id = f"session-{uuid4().hex}"
        contract_id = f"contract-{uuid4().hex}"
        instance_id = f"instance-{uuid4().hex}"
        older_run_id = f"run-{uuid4().hex}"
        older_message_id = f"message-{uuid4().hex}"
        active_run_id = f"run-{uuid4().hex}"
        active_message_id = f"message-{uuid4().hex}"
        run_id = f"run-{uuid4().hex}"
        message_id = f"message-{uuid4().hex}"
        plan_id = f"plan-{uuid4().hex}"
        task_id = f"task-{uuid4().hex}"
        first_execution_id = f"execution-{uuid4().hex}"
        retry_execution_id = f"execution-{uuid4().hex}"

        async with factory() as session, session.begin():
            session.add(
                ProjectModel(
                    id=project_id,
                    name="History",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project_id,
                    title="History",
                    created_at=NOW,
                    updated_at=NOW,
                    last_activity_at=NOW,
                )
            )
            session.add(
                WorkerContractModel(
                    contract_id=contract_id,
                    revision=1,
                    can_do_json=["执行"],
                    cannot_do_json=["越权"],
                    input_schema_json={"type": "object"},
                    output_schema_json={"type": "object"},
                    fingerprint_hash="0" * 64,
                    created_at=NOW,
                )
            )
            await session.flush()
            session.add(
                WorkerInstanceModel(
                    instance_id=instance_id,
                    service_principal="worker-a",
                    instance_key="worker-a-1",
                    worker_name="Worker A",
                    contract_id=contract_id,
                    contract_revision=1,
                    base_url="https://worker.example",
                    execution_timeout_seconds=60,
                    max_concurrency=1,
                    status="ready",
                    accepting_tasks=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.flush()
            # 这些模型没有 ORM relationship，必须显式按 FK 层级 flush，
            # 否则同一事务中的子表可能先于 Session 或父任务插入。
            session.add_all(
                [
                    AgentRunModel(
                        run_id=older_run_id,
                        session_id=session_id,
                        message_id=older_message_id,
                        sequence=1,
                        status="completed",
                        request_json={},
                        root_thread_id=f"thread-{older_run_id}",
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    AgentRunModel(
                        run_id=active_run_id,
                        session_id=session_id,
                        message_id=active_message_id,
                        sequence=2,
                        status="running",
                        request_json={},
                        root_thread_id=f"thread-{active_run_id}",
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    AgentRunModel(
                        run_id=run_id,
                        session_id=session_id,
                        message_id=message_id,
                        sequence=3,
                        status="completed",
                        request_json={},
                        response_json={"content": "最终答复"},
                        root_thread_id=f"thread-{run_id}",
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )
            await session.flush()
            session.add(
                OrchestratorPlanModel(
                    plan_id=plan_id,
                    run_id=run_id,
                    sequence=1,
                    idempotency_key=f"plan:{plan_id}",
                    request_hash="1" * 64,
                    goal="执行最新任务",
                    status="completed",
                    expected_task_count=1,
                    terminal_task_count=1,
                    root_wait_generation=1,
                    created_at=NOW,
                    completed_at=NOW,
                )
            )
            await session.flush()
            session.add(
                OrchestratorTaskModel(
                    task_id=task_id,
                    operation_id=str(uuid4()),
                    plan_id=plan_id,
                    task_ref="task",
                    contract_id=contract_id,
                    contract_revision=1,
                    objective="整理资料",
                    input_json={},
                    acceptance_criteria="返回摘要",
                    status="completed",
                    execution_generation=2,
                    cancellation_epoch=0,
                    active_execution_id=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.flush()
            session.add_all(
                [
                    OrchestratorExecutionModel(
                        execution_id=first_execution_id,
                        task_id=task_id,
                        instance_id=instance_id,
                        worker_name="Worker A",
                        generation=1,
                        status="failed",
                        cancellation_epoch=0,
                        capacity_reserved=False,
                        created_at=NOW,
                        completed_at=NOW,
                        updated_at=NOW,
                    ),
                    OrchestratorExecutionModel(
                        execution_id=retry_execution_id,
                        task_id=task_id,
                        instance_id=instance_id,
                        worker_name="Worker A",
                        generation=2,
                        status="completed",
                        cancellation_epoch=0,
                        capacity_reserved=False,
                        created_at=NOW,
                        completed_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    MessageModel(
                        id=older_message_id,
                        session_id=session_id,
                        role="user",
                        content="更早问题",
                        tool_calls_json=[],
                        run_id=older_run_id,
                        timestamp=NOW,
                    ),
                    MessageModel(
                        id=active_message_id,
                        session_id=session_id,
                        role="user",
                        content="活动问题",
                        tool_calls_json=[],
                        run_id=active_run_id,
                        timestamp=NOW,
                    ),
                    MessageModel(
                        id=message_id,
                        session_id=session_id,
                        role="user",
                        content="最新问题",
                        tool_calls_json=[],
                        run_id=run_id,
                        timestamp=NOW,
                    ),
                    MessageModel(
                        id=f"assistant:{run_id}",
                        session_id=session_id,
                        role="assistant",
                        content="最终答复",
                        tool_calls_json=[],
                        run_id=run_id,
                        timestamp=NOW,
                    ),
                    WorkerDisplayResultModel(
                        execution_id=first_execution_id,
                        display_result="第一次执行失败",
                        display_result_at=NOW,
                    ),
                    WorkerDisplayResultModel(
                        execution_id=retry_execution_id,
                        display_result="重试执行完成",
                        display_result_at=NOW,
                    ),
                    RunProgressModel(
                        progress_id=f"progress-{uuid4().hex}",
                        session_id=session_id,
                        run_id=run_id,
                        execution_id=None,
                        task_id=None,
                        worker_id=None,
                        source="root",
                        scope_key=f"run:{run_id}",
                        sequence=1,
                        content="Root 正在汇总",
                        created_at=NOW,
                    ),
                    RunProgressModel(
                        progress_id=f"progress-{uuid4().hex}",
                        session_id=session_id,
                        run_id=run_id,
                        execution_id=retry_execution_id,
                        task_id=task_id,
                        worker_id=instance_id,
                        source="worker",
                        scope_key=f"execution:{retry_execution_id}",
                        sequence=0,
                        content="重试任务开始",
                        created_at=NOW,
                    ),
                    RunProgressModel(
                        progress_id=f"progress-{uuid4().hex}",
                        session_id=session_id,
                        run_id=run_id,
                        execution_id=retry_execution_id,
                        task_id=task_id,
                        worker_id=instance_id,
                        source="worker",
                        scope_key=f"execution:{retry_execution_id}",
                        sequence=1,
                        content="重试任务读取资料",
                        created_at=NOW,
                    ),
                    RunProgressModel(
                        progress_id=f"progress-{uuid4().hex}",
                        session_id=session_id,
                        run_id=active_run_id,
                        execution_id=None,
                        task_id=None,
                        worker_id=None,
                        source="root",
                        scope_key=f"run:{active_run_id}",
                        sequence=1,
                        content="活动进度",
                        created_at=NOW,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=1,
                        run_id=run_id,
                        event_type="run.completed",
                        durability="durable",
                        payload_json={},
                        occurred_at=NOW,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=2,
                        run_id=active_run_id,
                        event_type="run.started",
                        durability="durable",
                        payload_json={},
                        occurred_at=NOW,
                    ),
                ]
            )

        repository = PostgresSessionHistoryRepository(factory)
        snapshot = await repository.read_snapshot(
            session_id,
            before_sequence=None,
            limit=1,
        )

        assert [run.run_id for run in snapshot.runs] == [run_id]
        assert {message.id for message in snapshot.messages} == {
            message_id,
            f"assistant:{run_id}",
        }
        assert {entry.run_id for entry in snapshot.progress} == {run_id}
        assert {
            (task.execution_id, task.display_result)
            for task in snapshot.tasks
        } == {
            (first_execution_id, "第一次执行失败"),
            (retry_execution_id, "重试执行完成"),
        }
        assert snapshot.has_more is True
        assert snapshot.snapshot_event_seq == 2
        assert snapshot.snapshot_progress_ingest_seq >= 4

        older = await repository.read_snapshot(
            session_id,
            before_sequence=3,
            limit=1,
        )
        assert [run.run_id for run in older.runs] == [older_run_id]
        assert older.has_more is False
    finally:
        await _dispose(admin_engine, test_engine, schema)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_history_returns_no_user_turn_while_latest_run_is_active() -> None:
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"session_history_active_{uuid4().hex}"
    admin_engine, test_engine = await _engines(schema)
    try:
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        project_id = f"project-{uuid4().hex}"
        session_id = f"session-{uuid4().hex}"
        run_id = f"run-{uuid4().hex}"
        message_id = f"message-{uuid4().hex}"
        async with factory() as session, session.begin():
            session.add(
                ProjectModel(
                    id=project_id,
                    name="History",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project_id,
                    title="History",
                    active_run_id=run_id,
                    created_at=NOW,
                    updated_at=NOW,
                    last_activity_at=NOW,
                )
            )
            await session.flush()
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    session_id=session_id,
                    message_id=message_id,
                    sequence=1,
                    status="running",
                    request_json={},
                    root_thread_id=f"thread-{run_id}",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            # Run 先落库，随后再插入引用它的 Message。
            await session.flush()
            session.add(
                MessageModel(
                    id=message_id,
                    session_id=session_id,
                    role="user",
                    content="仍在执行",
                    tool_calls_json=[],
                    run_id=run_id,
                    timestamp=NOW,
                )
            )

        snapshot = await PostgresSessionHistoryRepository(factory).read_snapshot(
            session_id,
            before_sequence=None,
            limit=20,
        )
        assert snapshot.runs == ()
        assert snapshot.messages == ()
        assert snapshot.progress == ()
        assert snapshot.tasks == ()
        assert snapshot.has_more is False
    finally:
        await _dispose(admin_engine, test_engine, schema)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stream_snapshot_recovers_every_turn_after_history_watermark() -> None:
    """历史读取后连续完成两轮时，首次连接和重连都恢复全部轮次。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"session_stream_{uuid4().hex}"
    admin_engine, test_engine = await _engines(schema)
    try:
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        project_id = f"project-{uuid4().hex}"
        session_id = f"session-{uuid4().hex}"
        historical_run_id = f"run-{uuid4().hex}"
        first_new_run_id = f"run-{uuid4().hex}"
        second_new_run_id = f"run-{uuid4().hex}"
        active_run_id = f"run-{uuid4().hex}"

        async with factory() as session, session.begin():
            session.add(
                ProjectModel(
                    id=project_id,
                    name="Stream",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project_id,
                    title="Stream",
                    active_run_id=active_run_id,
                    created_at=NOW,
                    updated_at=NOW,
                    last_activity_at=NOW,
                )
            )
            await session.flush()

            def run(run_id: str, sequence: int, status: str) -> AgentRunModel:
                return AgentRunModel(
                    run_id=run_id,
                    session_id=session_id,
                    message_id=f"message-{sequence}",
                    sequence=sequence,
                    status=status,
                    request_json={},
                    root_thread_id=f"thread-{run_id}",
                    created_at=NOW,
                    updated_at=NOW,
                )

            session.add_all(
                [
                    run(historical_run_id, 1, "completed"),
                    run(first_new_run_id, 2, "completed"),
                    run(second_new_run_id, 3, "completed"),
                    run(active_run_id, 4, "running"),
                ]
            )
            # Run 先落库，下面的事件、Message 和 Progress 才能稳定引用它。
            await session.flush()
            for sequence, run_id, content in (
                (1, historical_run_id, "历史问题"),
                (2, first_new_run_id, "第一问"),
                (3, second_new_run_id, "第二问"),
                (4, active_run_id, "当前问题"),
            ):
                session.add(
                    MessageModel(
                        id=f"message-{sequence}",
                        session_id=session_id,
                        role="user",
                        content=content,
                        tool_calls_json=[],
                        run_id=run_id,
                        timestamp=NOW.replace(second=sequence),
                    )
                )
            for sequence, run_id, content in (
                (2, first_new_run_id, "第一答"),
                (3, second_new_run_id, "第二答"),
            ):
                session.add(
                    MessageModel(
                        id=f"assistant:{run_id}",
                        session_id=session_id,
                        role="assistant",
                        content=content,
                        tool_calls_json=[],
                        run_id=run_id,
                        timestamp=NOW.replace(second=sequence, microsecond=1),
                    )
                )
            session.add(
                AgentEventModel(
                    session_id=session_id,
                    session_seq=1,
                    run_id=historical_run_id,
                    event_type="run.completed",
                    durability="durable",
                    payload_json={},
                    occurred_at=NOW,
                )
            )
            # 两轮快速终态故意没有 event 或 Progress。Run 序号水位必须
            # 仍能把它们完整恢复，不能依赖 outbox 已经可见。
            session.add(
                RunProgressModel(
                    progress_id=f"progress-{uuid4().hex}",
                    session_id=session_id,
                    run_id=active_run_id,
                    execution_id=None,
                    task_id=None,
                    worker_id=None,
                    source="root",
                    scope_key=f"run:{active_run_id}",
                    sequence=1,
                    content="当前执行中",
                    created_at=NOW,
                )
            )

        repository = PostgresSessionHistoryRepository(factory)
        stream_service = EventStreamService(
            SimpleNamespace(),
            SimpleNamespace(),
            history_reader=repository,
            observability=SimpleNamespace(),
        )
        recovered_turns: list[SessionTurnSnapshot] = []
        event_sequence = 1
        progress_sequence = 0
        after_run_sequence: int | None = 1
        boundary_active_run_id = None
        reconnect_token = None
        while True:
            _, first, _, reconnect_token = await stream_service.snapshot(
                session_id,
                after_event_sequence=event_sequence,
                after_progress_ingest_sequence=progress_sequence,
                after_run_sequence=after_run_sequence,
                active_run_id=boundary_active_run_id,
                limit=1,
            )
            recovered_turns.extend(first.turns)
            (
                event_sequence,
                progress_sequence,
                after_run_sequence,
                boundary_active_run_id,
            ) = decode_sync_token(
                reconnect_token,
                session_id=session_id,
            )
            if not first.has_more:
                break

        expected = (
            first_new_run_id,
            second_new_run_id,
            active_run_id,
        )
        assert tuple(turn.run.run_id for turn in recovered_turns) == expected
        assert recovered_turns[0].messages[-1].content == "第一答"
        assert recovered_turns[1].messages[-1].content == "第二答"
        assert first is not None
        assert first.snapshot_event_seq == 1
        assert first.snapshot_progress_ingest_seq == 1
        assert first.snapshot_run_seq == 4
        assert boundary_active_run_id == active_run_id
        assert reconnect_token is not None

        # 模拟客户端已收到首个快照后断线，断线期间当前轮结束且又连续
        # 完成两轮。重连必须从首个快照令牌恢复这三轮，不能跳过中间轮次。
        disconnect_run_ids = tuple(
            f"run-{uuid4().hex}" for _ in range(2)
        )
        async with factory() as session, session.begin():
            active_run = await session.get(AgentRunModel, active_run_id)
            assert active_run is not None
            active_run.status = "completed"
            active_row = await session.get(SessionModel, session_id)
            if active_row is not None:
                active_row.active_run_id = None
            session.add_all(
                [
                    AgentRunModel(
                        run_id=run_id,
                        session_id=session_id,
                        message_id=f"message-{sequence}",
                        sequence=sequence,
                        status="completed",
                        request_json={},
                        root_thread_id=f"thread-{run_id}",
                        created_at=NOW,
                        updated_at=NOW,
                    )
                    for sequence, run_id in zip(
                        (5, 6), disconnect_run_ids, strict=True
                    )
                ]
            )
            # 断线期间新增的 Run 先落库，再写 Message 与 outbox 事件。
            await session.flush()
            all_disconnect_rows = (active_run_id, *disconnect_run_ids)
            for sequence, run_id in enumerate(disconnect_run_ids, start=5):
                session.add(
                    MessageModel(
                        id=f"message-{sequence}",
                        session_id=session_id,
                        role="user",
                        content=f"断线问题-{sequence}",
                        tool_calls_json=[],
                        run_id=run_id,
                        timestamp=NOW.replace(second=sequence),
                    )
                )
            for sequence, run_id in enumerate(all_disconnect_rows, start=4):
                session.add(
                    MessageModel(
                        id=f"assistant:{run_id}",
                        session_id=session_id,
                        role="assistant",
                        content=f"断线回答-{sequence}",
                        tool_calls_json=[],
                        run_id=run_id,
                        timestamp=NOW.replace(second=sequence, microsecond=1),
                    )
                )
            session.add_all(
                [
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=sequence,
                        run_id=run_id,
                        event_type="run.completed",
                        durability="durable",
                        payload_json={},
                        occurred_at=NOW,
                    )
                    for sequence, run_id in enumerate(
                        all_disconnect_rows, start=2
                    )
                ]
            )

        reconnected_turns: list[SessionTurnSnapshot] = []
        (
            reconnect_event_sequence,
            reconnect_progress_sequence,
            reconnect_after,
            reconnect_active_run_id,
        ) = decode_sync_token(
            reconnect_token,
            session_id=session_id,
        )
        assert reconnect_active_run_id == active_run_id
        while True:
            _, reconnect, _, next_token = await stream_service.snapshot(
                session_id,
                after_event_sequence=reconnect_event_sequence,
                after_progress_ingest_sequence=reconnect_progress_sequence,
                after_run_sequence=reconnect_after,
                active_run_id=reconnect_active_run_id,
                limit=1,
            )
            reconnected_turns.extend(reconnect.turns)
            (
                reconnect_event_sequence,
                reconnect_progress_sequence,
                reconnect_after,
                reconnect_active_run_id,
            ) = decode_sync_token(
                next_token,
                session_id=session_id,
            )
            if not reconnect.has_more:
                break
        assert tuple(turn.run.run_id for turn in reconnected_turns) == (
            active_run_id,
            *disconnect_run_ids,
        )
        assert all(
            turn.messages[-1].role.value == "assistant"
            for turn in reconnected_turns
        )
    finally:
        await _dispose(admin_engine, test_engine, schema)
