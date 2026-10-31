"""验证新 Worker Plan/Task/Execution 与记忆证据仓储的 PostgreSQL 边界。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.memory import MemoryEpisode, MemoryEpisodeSummary
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    Base,
    ExecutionLeaseModel,
    MemoryEpisodeModel,
    MemoryProcessingJobModel,
    MemoryProcessingJobModel,
    MemoryRunEvidenceSnapshotModel,
    MemorySourceSuppressionModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    ProjectModel,
    SessionModel,
    WorkerContractModel,
    WorkerInstanceModel,
)
from infrastructure.persistence.postgres.repositories.lease_center import (
    PostgresLeaseCenter,
)
from infrastructure.persistence.postgres.repositories.memory_episode_repository import (
    PostgresMemoryEpisodeRepository,
)
from infrastructure.persistence.postgres.repositories.memory_job_repository import (
    PostgresMemoryJobRepository,
)


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_MEMORY_S2_TABLES = [
    ProjectModel.__table__,
    SessionModel.__table__,
    AgentRunModel.__table__,
    WorkerContractModel.__table__,
    WorkerInstanceModel.__table__,
    OrchestratorPlanModel.__table__,
    OrchestratorTaskModel.__table__,
    OrchestratorExecutionModel.__table__,
    MemoryProcessingJobModel.__table__,
    MemoryRunEvidenceSnapshotModel.__table__,
    MemoryEpisodeModel.__table__,
    MemorySourceSuppressionModel.__table__,
    ExecutionLeaseModel.__table__,
]


@pytest_asyncio.fixture
async def memory_store():
    """只在随机 schema 中建表，并在测试结束后删除该 schema。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"memory_s2_{uuid4().hex}"
    url = make_url(_DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with test_engine.begin() as connection:
            await connection.execute(
                text("CREATE TEXT SEARCH CONFIGURATION chinese (COPY = pg_catalog.simple)")
            )
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=_MEMORY_S2_TABLES
                )
            )
        yield async_sessionmaker(test_engine, expire_on_commit=False)
    finally:
        await test_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _seed_run(
    session_factory: async_sessionmaker,
    *,
    run_id: str,
    session_id: str,
    project_id: str,
) -> datetime:
    now = datetime.now(timezone.utc)
    async with session_factory() as session, session.begin():
        session.add(
            ProjectModel(
                id=project_id,
                name="Memory S2 test",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SessionModel(
                id=session_id,
                project_id=project_id,
                title="Memory S2 test",
                last_run_sequence=1,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
        )
        session.add(
            AgentRunModel(
                run_id=run_id,
                session_id=session_id,
                message_id=f"message-{run_id}",
                sequence=1,
                status="completed",
                request_json={"user_text": "Prepare a report"},
                response_json={"text": "The report is ready."},
                root_thread_id=f"root-{run_id}",
                root_wait_generation=0,
                orchestrator_plan_sequence=0,
                cancellation_epoch=0,
                cancellation_external_effect_status="none",
                created_at=now,
                updated_at=now,
            )
        )
    return now


def _repositories(session_factory: async_sessionmaker):
    lease_center = PostgresLeaseCenter(session_factory)
    return (
        PostgresMemoryJobRepository(session_factory, lease_center=lease_center),
        PostgresMemoryEpisodeRepository(session_factory, lease_center=lease_center),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plain_run_memory_job_claims_with_empty_worker_evidence(memory_store) -> None:
    """没有 Orchestrator Plan 的普通 Run 能正常领取记忆作业，执行证据为空。"""
    session_factory = memory_store
    run_id, session_id, project_id = (str(uuid4()) for _ in range(3))
    await _seed_run(
        session_factory,
        run_id=run_id,
        session_id=session_id,
        project_id=project_id,
    )
    jobs, _episodes = _repositories(session_factory)

    assert await jobs.enqueue(
        turn_id=run_id,
        session_id=session_id,
        user_text="Prepare a report",
        assistant_text="The report is ready.",
    )
    claimed = await jobs.claim_next_job()

    assert claimed is not None
    assert claimed.turn.execution_evidence is None
    await jobs.mark_succeeded(run_id, lease=claimed.lease)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_task_output_is_frozen_and_episode_source_can_be_read(
    memory_store,
) -> None:
    """冻结新 Task/Execution 状态，并从 episode 来源引用读取该次 Worker 输出。"""
    session_factory = memory_store
    run_id, session_id, project_id = (str(uuid4()) for _ in range(3))
    plan_id, task_id, execution_id = (str(uuid4()) for _ in range(3))
    contract_id = str(uuid4())
    task_ref = "write-report-" + "x" * 2100
    now = await _seed_run(
        session_factory,
        run_id=run_id,
        session_id=session_id,
        project_id=project_id,
    )
    output = {"report": "The verified result is 42."}
    async with session_factory() as session, session.begin():
        session.add(
            WorkerContractModel(
                contract_id=contract_id,
                revision=1,
                can_do_json=["write reports"],
                cannot_do_json=["access unprovided sources"],
                input_schema_json={"type": "object"},
                output_schema_json={"type": "object"},
                fingerprint_hash=f"contract-{contract_id}",
                created_at=now,
            )
        )
        session.add(
            OrchestratorPlanModel(
                plan_id=plan_id,
                run_id=run_id,
                sequence=1,
                idempotency_key="plan-1",
                request_hash="plan-hash",
                goal="Prepare a report",
                status="completed",
                expected_task_count=1,
                terminal_task_count=1,
                root_wait_generation=1,
                created_at=now,
                completed_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorTaskModel(
                task_id=task_id,
                operation_id=f"operation-{task_id}",
                plan_id=plan_id,
                task_ref=task_ref,
                contract_id=contract_id,
                contract_revision=1,
                objective="Write and verify the requested report.",
                input_json={"topic": "answer"},
                acceptance_criteria="Include the verified result.",
                status="completed",
                output_json=output,
                execution_generation=1,
                cancellation_epoch=0,
                active_execution_id=execution_id,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            OrchestratorExecutionModel(
                execution_id=execution_id,
                task_id=task_id,
                generation=1,
                status="completed",
                cancellation_epoch=0,
                capacity_reserved=False,
                worker_result_id="result-1",
                result_json=output,
                created_at=now,
                updated_at=now,
            )
        )

    jobs, episodes = _repositories(session_factory)
    assert await jobs.enqueue(
        turn_id=run_id,
        session_id=session_id,
        user_text="Prepare a report",
        assistant_text="The report is ready.",
    )
    claimed = await jobs.claim_next_job()
    assert claimed is not None
    evidence = claimed.turn.execution_evidence
    assert evidence is not None
    assert evidence.total_task_count == 1
    assert len(evidence.tasks) == 1
    task_evidence = evidence.tasks[0]
    assert task_evidence.task_id == task_id
    assert task_evidence.title == task_ref[:300]
    assert task_evidence.status == "completed"
    assert task_evidence.execution_generation == 1
    assert task_evidence.result_ref == execution_id
    assert task_evidence.tool_outcomes == ()
    artifact_reference = next(
        reference
        for reference in evidence.source_refs
        if reference.source_type == "artifact"
    )
    assert len(artifact_reference.excerpt) == 2000

    frozen_evidence = evidence
    await jobs.mark_failed(
        run_id,
        "temporary test retry",
        retry=True,
        lease=claimed.lease,
    )
    async with session_factory() as session, session.begin():
        job = await session.scalar(
            select(MemoryProcessingJobModel).where(
                MemoryProcessingJobModel.turn_id == run_id
            )
        )
        assert job is not None
        job.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    retried = await jobs.claim_next_job()
    assert retried is not None
    assert retried.attempt == 2
    assert retried.turn.execution_evidence == frozen_evidence

    episode = MemoryEpisode(
        episode_id=str(uuid4()),
        turn_id=run_id,
        session_id=session_id,
        project_id=project_id,
        content="The report was completed with the verified result.",
        summary=MemoryEpisodeSummary(
            title="Report preparation",
            goal="Prepare a report",
            outcome="completed",
            result="The Worker produced the verified result 42.",
        ),
        source_refs=evidence.source_refs,
        source_fingerprint=evidence.source_fingerprint,
        created_at=now,
    )
    persisted = await episodes.create_if_absent(episode, lease=retried.lease)
    assert persisted is not None
    expanded = await episodes.expand_sources(
        episode.episode_id,
        session_id=session_id,
        project_id=project_id,
        limit=4,
    )
    assert any(
        reference.source_type == "artifact"
        and reference.source_id == execution_id
        and reference.excerpt == '{"report":"The verified result is 42."}'
        for reference in expanded
    ), expanded
    await jobs.mark_succeeded(run_id, lease=retried.lease)
