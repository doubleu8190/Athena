"""Project memory forgetting must fence queued and still-running source turns."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.runs import RunStartPayload
from infrastructure.persistence.postgres.models import (
    AgentEventModel,
    AgentPlanModel,
    AgentRunModel,
    Base,
    ExecutionLeaseModel,
    IdempotencyReceiptModel,
    MemoryEpisodeModel,
    MemoryEpisodeProjectBindingModel,
    MemoryExtractionSnapshotModel,
    MemoryModel,
    MemoryProcessingJobModel,
    ProjectModel,
    MemoryRunEvidenceSnapshotModel,
    MemorySourceSuppressionModel,
    MemoryWriteOperationModel,
    MessageModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RootResumeJobModel,
    SessionMemorySettingsModel,
    SessionModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresProjectRepository,
    PostgresRunRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_forget_fences_a_running_run_and_cleans_scoped_jobs() -> None:
    """Forget serializes with Run start and blocks its later terminal enqueue."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"memory_forget_{uuid4().hex}"
    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(url)
    engine = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema},public"})
    )
    created = False
    project_id = f"project-{uuid4().hex}"
    session_id = f"session-{uuid4().hex}"
    run_id = f"run-{uuid4().hex}"
    legacy_turn_id = f"legacy-turn-{uuid4().hex}"
    operation_id = f"operation-{uuid4().hex}"
    memory_id = f"memory-{uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    projects = PostgresProjectRepository(factory)
    runs = PostgresRunRepository(factory)

    tables = [

        ExecutionLeaseModel.__table__,
        SessionModel.__table__,
        ProjectModel.__table__,
        SessionMemorySettingsModel.__table__,
        AgentRunModel.__table__,
        AgentEventModel.__table__,
        IdempotencyReceiptModel.__table__,
        MessageModel.__table__,
        RootResumeJobModel.__table__,
        AgentPlanModel.__table__,
        PlanVersionModel.__table__,
        PlanRoundModel.__table__,
        OrchestrationOutboxModel.__table__,
        MemoryModel.__table__,
        MemoryProcessingJobModel.__table__,
        MemoryEpisodeModel.__table__,
        MemoryEpisodeProjectBindingModel.__table__,
        MemoryExtractionSnapshotModel.__table__,
        MemoryRunEvidenceSnapshotModel.__table__,
        MemoryWriteOperationModel.__table__,
        MemorySourceSuppressionModel.__table__,
    ]
    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=tables, checkfirst=False
                )
            )
            actual_schema = await connection.scalar(
                text(
                    "SELECT n.nspname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE c.oid='projects'::regclass"
                )
            )
            assert actual_schema == schema

        async with factory() as session, session.begin():
            session.add(
                ProjectModel(
                    id=project_id,
                    name="Forget race project",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project_id,
                    title="Forget race",
                    created_at=now,
                    updated_at=now,
                    last_activity_at=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    MemoryProcessingJobModel(
                        job_id=f"job-{uuid4().hex}",
                        turn_id=legacy_turn_id,
                        session_id=session_id,
                        user_text="old project work",
                        assistant_text="old result",
                        status="queued",
                        attempt=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    ),
                    MemoryModel(
                        id=memory_id,
                        logical_memory_id=memory_id,
                        session_id=session_id,
                        content="project-only fact",
                        metadata_json=json.dumps(
                            {
                                "scope_kind": "project",
                                "scope_id": project_id,
                                "source_turn_id": run_id,
                                "fact": {
                                    "scope_kind": "project",
                                    "scope_id": project_id,
                                },
                            }
                        ),
                        created_at=now,
                        status="active",
                        validity_status="valid",
                    ),
                    MemoryExtractionSnapshotModel(
                        turn_id=legacy_turn_id,
                        snapshot_json="[]",
                        created_at=now,
                    ),
                    MemoryRunEvidenceSnapshotModel(
                        turn_id=legacy_turn_id,
                        source_fingerprint="test-fingerprint",
                        snapshot_json="{}",
                        created_at=now,
                    ),
                    MemoryWriteOperationModel(
                        operation_id=operation_id,
                        turn_id=legacy_turn_id,
                        candidate_json="{}",
                        status="pending",
                        projection_status="pending",
                        created_at=now,
                        updated_at=now,
                    ),
                    MemoryEpisodeModel(
                        episode_id=f"episode-{uuid4().hex}",
                        turn_id=legacy_turn_id,
                        session_id=session_id,
                        content="old project episode",
                        summary_json="{}",
                        source_refs_json="[]",
                        source_fingerprint="test-fingerprint",
                        created_at=now,
                    ),
                ]
            )
            await session.flush()
            episode_id = await session.scalar(
                select(MemoryEpisodeModel.episode_id).where(
                    MemoryEpisodeModel.turn_id == legacy_turn_id
                )
            )
            session.add(
                MemoryEpisodeProjectBindingModel(
                    episode_id=episode_id,
                    project_id=project_id,
                    created_at=now,
                )
            )

        await runs.enqueue_run(
            request_id=f"request-{uuid4().hex}",
            parameters={"message": "Keep this run in the project memory scope"},
            session_id=session_id,
            project_id=project_id,
            run_id=run_id,
            payload=RunStartPayload(
                message="Keep this run in the project memory scope",
                message_id=f"message-{uuid4().hex}",
            ),
        )
        invocation = await runs.claim_next_run()
        assert invocation is not None and invocation.run_id == run_id
        async with factory() as session:
            saved_session = await session.get(SessionModel, session_id)
            assert saved_session is not None
            assert saved_session.project_id == project_id

        with pytest.raises(ValueError, match="project_busy"):
            await projects.delete(project_id)
        completed = await runs.complete(
            run_id,
            {
                "content": "late terminal result",
                "answer_stream": {
                    "message_id": f"assistant:{run_id}",
                    "stream_id": f"answer-{run_id}",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "last_chunk_id": 1,
                },
            },
            lease=invocation.lease,
        )
        assert completed is True
        assert await projects.delete(project_id) is True

        async with factory() as session:
            deleted_project = await session.get(ProjectModel, project_id)
            assert deleted_project is not None
            assert deleted_project.deleted_time is not None
            deleted_session = await session.get(SessionModel, session_id)
            assert deleted_session is not None
            assert deleted_session.deleted_time is not None
            assert await session.get(MemoryProcessingJobModel, legacy_turn_id) is None
            assert await session.get(MemoryExtractionSnapshotModel, legacy_turn_id) is None
            assert await session.get(MemoryRunEvidenceSnapshotModel, legacy_turn_id) is None
            assert await session.get(MemoryWriteOperationModel, operation_id) is None
            forgotten_memory = await session.get(MemoryModel, memory_id)
            assert forgotten_memory is not None
            assert forgotten_memory.status == "deleted"
            assert forgotten_memory.deleted_time is not None
            episode = await session.scalar(
                select(MemoryEpisodeModel).where(
                    MemoryEpisodeModel.turn_id == legacy_turn_id
                )
            )
            assert episode is None
            suppressions = set(
                (
                    await session.execute(
                        select(
                            MemorySourceSuppressionModel.source_type,
                            MemorySourceSuppressionModel.source_id,
                        ).where(
                            MemorySourceSuppressionModel.source_type.in_(
                                ("project", "project_turn")
                            )
                        )
                    )
                ).all()
            )
            assert ("project", project_id) in suppressions
            assert ("project_turn", legacy_turn_id) in suppressions
            assert ("project_turn", run_id) in suppressions
            run = await session.get(AgentRunModel, run_id)
            assert run is not None

        async with factory() as session:
            assert (
                await session.get(MemoryProcessingJobModel, run_id) is None
            )
            assert await session.get(AgentRunModel, run_id) is not None
    finally:
        await engine.dispose()
        if created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
