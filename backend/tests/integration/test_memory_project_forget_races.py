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

from domain.runs import CommandType, RunCommand, RunStartPayload
from infrastructure.persistence.postgres.models import (
    AgentCommandModel,
    AgentRunModel,
    Base,
    MemoryEpisodeModel,
    MemoryEpisodeProjectBindingModel,
    MemoryExtractionSnapshotModel,
    MemoryJobScopeSnapshotModel,
    MemoryModel,
    MemoryProcessingJobModel,
    MemoryProjectSpaceModel,
    MemoryRunEvidenceSnapshotModel,
    MemorySourceSuppressionModel,
    MemoryWriteOperationModel,
    MessageModel,
    OrchestrationOutboxModel,
    RootResumeJobModel,
    SessionMemoryProjectBindingModel,
    SessionMemorySettingsModel,
    SessionModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresMemoryProjectSpaceRepository,
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
    projects = PostgresMemoryProjectSpaceRepository(factory)
    runs = PostgresRunRepository(factory)

    tables = [
        SessionModel.__table__,
        MemoryProjectSpaceModel.__table__,
        SessionMemoryProjectBindingModel.__table__,
        SessionMemorySettingsModel.__table__,
        AgentCommandModel.__table__,
        AgentRunModel.__table__,
        MessageModel.__table__,
        RootResumeJobModel.__table__,
        OrchestrationOutboxModel.__table__,
        MemoryModel.__table__,
        MemoryProcessingJobModel.__table__,
        MemoryEpisodeModel.__table__,
        MemoryEpisodeProjectBindingModel.__table__,
        MemoryJobScopeSnapshotModel.__table__,
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
                    "WHERE c.oid='memory_project_spaces'::regclass"
                )
            )
            assert actual_schema == schema

        async with factory() as session, session.begin():
            session.add_all(
                [
                    SessionModel(
                        id=session_id,
                        title="Forget race",
                        status="idle",
                        created_at=now,
                        updated_at=now,
                    ),
                    MemoryProjectSpaceModel(
                        id=project_id,
                        name="Forget race project",
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                SessionMemoryProjectBindingModel(
                    session_id=session_id,
                    project_id=project_id,
                    updated_at=now,
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
                    MemoryJobScopeSnapshotModel(
                        turn_id=legacy_turn_id,
                        session_id=session_id,
                        project_id=project_id,
                        created_at=now,
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

        command = RunCommand(
            command_id=f"command-{uuid4().hex}",
            command_type=CommandType.RUN_START,
            session_id=session_id,
            run_id=run_id,
            payload=RunStartPayload(
                message="Keep this run in the project memory scope",
                message_id=f"message-{uuid4().hex}",
            ),
            issued_at=datetime.now(timezone.utc),
        )

        await runs.enqueue_command(command)
        async with factory() as session:
            start_snapshot = await session.get(MemoryJobScopeSnapshotModel, run_id)
            assert start_snapshot is not None
            assert start_snapshot.project_id == project_id

        # Racing project deletion with terminal enqueue exercises both legal
        # serial orders while the Run is active and absent from the memory queue.
        forgotten, completed = await asyncio.gather(
            projects.forget(project_id),
            runs.complete(
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
            ),
        )
        assert forgotten is True
        assert completed is True

        async with factory() as session:
            assert await session.get(MemoryProjectSpaceModel, project_id) is None
            assert (
                await session.get(SessionMemoryProjectBindingModel, session_id)
                is None
            )
            assert await session.get(MemoryJobScopeSnapshotModel, run_id) is None
            assert await session.get(MemoryJobScopeSnapshotModel, legacy_turn_id) is None
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
