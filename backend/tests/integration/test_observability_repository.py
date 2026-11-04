"""PostgreSQL Trace/Progress、Worker 幂等和终态互斥集成测试。"""

from __future__ import annotations

import os
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.observability import (
    ProgressConflictError,
    ProgressExecutionTerminalError,
    RootNodeSpanAttributes,
    RunObservabilityService,
    RunSpanAttributes,
    TraceSpanAttributes,
    WorkerProgressContext,
)
from domain.events import EventPort
from domain.orchestration import WorkerExecutionError, WorkerResultSubmission
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    Base,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    ProjectModel,
    SessionModel,
    WorkerContractModel,
    WorkerDisplayResultModel,
    WorkerInstanceModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresObservabilityRepository,
    PostgresWorkerOrchestrationRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Events(EventPort):
    async def publish(self, event):
        return event

    async def publish_realtime(self, event):
        return event

    async def open_subscription(self, session_id):
        raise NotImplementedError

    async def list_events_between(self, session_id, after=0, upto=None, limit=256):
        return []

    async def list_events_for_run(
        self, session_id, run_id, *, after=0, limit=200
    ):
        return [], False

    async def close_subscription(self, session_id, queue):
        return None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_progress_is_idempotent_conflict_typed_and_terminal_closed() -> None:
    """同一序号同内容幂等，不同内容 409 语义，其他身份字段不能替代绑定。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"observability_{uuid4().hex}"
    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        # 临时 schema 只隔离业务表；extensions 与 vector 等公共类型仍位于 public。
        url.update_query_dict({"options": f"-c search_path={schema},public"})
    )
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(sync, checkfirst=False)
            )

        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        session_id = f"session-{uuid4().hex}"
        run_id = f"run-{uuid4().hex}"
        plan_id = f"plan-{uuid4().hex}"
        task_id = f"task-{uuid4().hex}"
        execution_id = f"execution-{uuid4().hex}"
        instance_id = f"instance-{uuid4().hex}"
        contract_id = f"contract-{uuid4().hex}"
        async with factory() as session, session.begin():
            project = ProjectModel(
                id=f"project-{uuid4().hex}",
                name="Observability",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(project)
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project.id,
                    title="Observability",
                    created_at=NOW,
                    updated_at=NOW,
                    last_activity_at=NOW,
                )
            )
            session.add(
                WorkerContractModel(
                    contract_id=contract_id,
                    revision=1,
                    can_do_json=["执行任务"],
                    cannot_do_json=["越权"],
                    input_schema_json={"type": "object"},
                    output_schema_json={"type": "object"},
                    fingerprint_hash="0" * 64,
                    created_at=NOW,
                )
            )
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
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    session_id=session_id,
                    message_id=f"message-{uuid4().hex}",
                    sequence=1,
                    status="running",
                    request_json={},
                    root_thread_id=f"thread-{run_id}",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.add(
                OrchestratorPlanModel(
                    plan_id=plan_id,
                    run_id=run_id,
                    sequence=1,
                    idempotency_key=f"plan:{plan_id}",
                    request_hash="1" * 64,
                    goal="执行任务",
                    status="running",
                    expected_task_count=1,
                    terminal_task_count=0,
                    root_wait_generation=1,
                    created_at=NOW,
                )
            )
            # 模型之间没有 ORM relationship，不能依赖 flush 自动推断 Plan 与
            # Task 的插入先后；先生成完整的父级链再插入 execution。
            await session.flush()
            session.add(
                OrchestratorTaskModel(
                    task_id=task_id,
                    operation_id=str(uuid4()),
                    plan_id=plan_id,
                    task_ref="task",
                    contract_id=contract_id,
                    contract_revision=1,
                    objective="执行任务",
                    input_json={},
                    acceptance_criteria="返回结果",
                    status="running",
                    execution_generation=1,
                    active_execution_id=execution_id,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.flush()
            session.add(
                OrchestratorExecutionModel(
                    execution_id=execution_id,
                    task_id=task_id,
                    instance_id=instance_id,
                    worker_name="Worker A",
                    generation=1,
                    status="running",
                    cancellation_epoch=0,
                    capacity_reserved=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

        repository = PostgresObservabilityRepository(factory)
        context = await repository.worker_progress_context(
            execution_id=execution_id,
            worker_id=instance_id,
        )
        assert context == WorkerProgressContext(
            execution_id=execution_id,
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
            worker_id=instance_id,
            worker_name="Worker A",
            task_title="执行任务",
        )
        first = await repository.append_worker_progress(
            context, sequence=1, content="正在读取资料"
        )
        assert first.worker_name == "Worker A"
        assert first.task_title == "执行任务"
        replayed_progress = await repository.list_progress_after(session_id, 0)
        assert replayed_progress[0].worker_name == "Worker A"
        assert replayed_progress[0].task_title == "执行任务"
        with pytest.raises(ProgressConflictError, match="sequence_incomplete"):
            await repository.append_worker_progress(
                context, sequence=3, content="跳号进度"
            )
        replay = await repository.append_worker_progress(
            context, sequence=1, content="正在读取资料"
        )
        assert replay == first
        with pytest.raises(ProgressConflictError):
            await repository.append_worker_progress(
                context, sequence=1, content="不同内容"
            )

        workers = PostgresWorkerOrchestrationRepository(factory)
        submission = WorkerResultSubmission(
            execution_id=execution_id,
            instance_id=instance_id,
            generation=1,
            result_id=str(uuid4()),
            status="failed",
            error=WorkerExecutionError(
                code="TEST_FAILURE",
                message="测试失败",
                retryable=False,
            ),
            last_progress_sequence=2,
            display_result="用户可读失败结果",
        )
        # 仅凭 last_progress_sequence=2 不能跨过缺失的序号 2 提前终结。
        with pytest.raises(
            ValueError, match="worker_progress_sequence_incomplete"
        ):
            await workers.record_result(submission=submission)
        second, first_result = await asyncio.gather(
            repository.append_worker_progress(
                context, sequence=2, content="连续进度"
            ),
            workers.record_result(submission=submission),
            return_exceptions=True,
        )
        assert not isinstance(second, BaseException)
        if isinstance(first_result, BaseException):
            assert isinstance(first_result, ValueError)
            assert str(first_result) == "worker_progress_sequence_incomplete"
            receipt = await workers.record_result(submission=submission)
        else:
            receipt = first_result
        assert receipt.disposition == "recorded"
        replayed_receipt = await workers.record_result(submission=submission)
        assert replayed_receipt.replayed is True

        async with factory() as session, session.begin():
            execution = await session.get(
                OrchestratorExecutionModel, execution_id
            )
            assert execution is not None
            assert execution.status == "failed"
            display_result = await session.get(
                WorkerDisplayResultModel, execution_id
            )
            assert display_result is not None
            assert display_result.display_result == "用户可读失败结果"
            display_columns = list(
                (
                    await session.execute(
                        text(
                            "SELECT column_name "
                            "FROM information_schema.columns "
                            "WHERE table_schema = current_schema() "
                            "AND table_name = 'worker_display_results' "
                            "ORDER BY ordinal_position"
                        )
                    )
                ).scalars()
            )
            assert display_columns == [
                "execution_id",
                "display_result",
                "display_result_at",
            ]
        assert (
            await repository.append_worker_progress(
                context, sequence=2, content="连续进度"
            )
            == second
        )
        with pytest.raises(ProgressExecutionTerminalError):
            await repository.append_worker_progress(
                context, sequence=3, content="迟到进度"
            )
    finally:
        await test_engine.dispose()
        if created:
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA "{schema}" CASCADE')
                )
        await admin_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trace_spans_round_trip_typed_attributes_and_parentage() -> None:
    """span 顺序、原始 started_at、父子关系和类型化属性可完整回读。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"trace_{uuid4().hex}"
    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        # 临时 schema 只隔离业务表；extensions 与 vector 等公共类型仍位于 public。
        url.update_query_dict({"options": f"-c search_path={schema},public"})
    )
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(sync, checkfirst=False)
            )

        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        session_id = f"session-{uuid4().hex}"
        run_id = f"run-{uuid4().hex}"
        async with factory() as session, session.begin():
            project = ProjectModel(
                id=f"project-{uuid4().hex}",
                name="Trace",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(project)
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project.id,
                    title="Trace",
                    created_at=NOW,
                    updated_at=NOW,
                    last_activity_at=NOW,
                )
            )
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    session_id=session_id,
                    message_id=f"message-{uuid4().hex}",
                    sequence=1,
                    status="running",
                    request_json={},
                    root_thread_id=f"thread-{run_id}",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

        repository = PostgresObservabilityRepository(factory)
        service = RunObservabilityService(
            repository,
            Events(),
            id_factory=iter(("span-root", "span-node")).__next__,
        )
        root = await service.start_span(
            session_id=session_id,
            run_id=run_id,
            name="run.execute",
            attributes=RunSpanAttributes(),
        )
        node = await service.start_span(
            session_id=session_id,
            run_id=run_id,
            name="decide_root_action",
            attributes=RootNodeSpanAttributes(node_name="decide_root_action"),
            parent_span_id=root.span_id,
        )
        await service.finish_span(
            node.span_id,
            status="failed",
            error={
                "code": "node_failed",
                "type": "RuntimeError",
                "message": "Authorization: Bearer trace-secret",
            },
        )
        await service.finish_span(root.span_id, status="completed")

        spans = await service.list_run_spans(run_id)
        assert [span.sequence for span in spans] == [1, 2]
        assert spans[1].parent_span_id == spans[0].span_id
        assert spans[1].error == {
            "code": "node_failed",
            "type": "RuntimeError",
        }
        async with factory() as session:
            raw_error = await session.scalar(
                text(
                    "SELECT error_json::text FROM trace_spans "
                    "WHERE span_id = :span_id"
                ),
                {"span_id": node.span_id},
            )
        assert raw_error is not None
        assert "trace-secret" not in raw_error
        parsed = TypeAdapter(TraceSpanAttributes).validate_python(
            spans[1].attributes.model_dump(mode="json")
        )
        assert parsed.span_type == "root_node"
        assert all(span.completed_at is not None for span in spans)
    finally:
        await test_engine.dispose()
        if created:
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA "{schema}" CASCADE')
                )
        await admin_engine.dispose()
