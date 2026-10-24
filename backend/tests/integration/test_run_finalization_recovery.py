"""PostgreSQL acceptance for atomic final Run artifacts and recovery."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.runs import RunExecutionService
from domain.runs import CommandExecutionResult, CommandType, RunCommand, RunStartPayload
from infrastructure.persistence.postgres.models import (
    AgentCommandModel,
    AgentEventModel,
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    MemoryProcessingJobModel,
    MessageModel,
    SessionModel,
    OrchestrationCommandModel,
    OrchestrationInboxModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RootResumeJobModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from infrastructure.persistence.postgres.repositories.event_repository import (
    PostgresEventRepository,
)
from infrastructure.persistence.postgres.repositories.memory_job_repository import (
    PostgresMemoryJobRepository,
)
from infrastructure.persistence.postgres.repositories.orchestration_outbox_repository import (
    PostgresOrchestrationOutboxRepository,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from workers.orchestration_outbox_dispatcher import OrchestrationOutboxDispatcher


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_answer_and_memory_job_survive_completion_receipt_loss() -> None:
    """A crash after Run commit leaves one answer, memory job and replayable outbox."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"run_finalization_{uuid4().hex}"
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
    message_id = f"message-{run_id}"
    command_id = f"command-{run_id}"

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        tables = [
            SessionModel.__table__,
            MessageModel.__table__,
            AgentCommandModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            OrchestrationCommandModel.__table__,
            OrchestrationInboxModel.__table__,
            OrchestrationOutboxModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            RootResumeJobModel.__table__,
            WorkerJobModel.__table__,
            AgentEventModel.__table__,
            MemoryProcessingJobModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session:
            session.add(SessionModel(id=session_id, title="stream recovery", created_at=now.isoformat(), updated_at=now.isoformat()))
            await session.commit()
        final_result = {
            "content": "I will keep reports concise.",
            "answer_stream": {
                "message_id": f"assistant:{run_id}", "stream_id": f"answer-{run_id}",
                "started_at": now.isoformat(), "last_chunk_id": 1,
            },
        }
        repository = PostgresRunRepository(session_factory)
        command = RunCommand(
            command_id=command_id,
            command_type=CommandType.RUN_START,
            session_id=session_id,
            run_id=run_id,
            payload=RunStartPayload(
                message="Remember that I prefer concise reports",
                message_id=message_id,
            ),
            issued_at=now,
        )
        await repository.enqueue_command(command)
        claimed_command = await repository.claim_next_command()
        assert claimed_command is not None

        class LoseCompletionReceipt:
            writes_cancellation_events_to_outbox = False
            writes_terminal_artifacts_to_outbox = True

            async def update_status(self, target_run_id, status):
                return await repository.update_status(target_run_id, status)

            async def complete(self, target_run_id, result):
                assert await repository.complete(target_run_id, result)
                raise asyncio.CancelledError

            async def arm_root_wait(self, target_run_id, **identity):
                return await repository.arm_root_wait(target_run_id, **identity)

        class Executor:
            def __init__(self) -> None:
                self.calls = 0

            async def execute(self, _command):
                self.calls += 1
                return CommandExecutionResult(
                    result=final_result
                )

            async def cancel(self, _run_id):
                return None

        executor = Executor()
        memory_jobs = PostgresMemoryJobRepository(session_factory)
        service = RunExecutionService(
            LoseCompletionReceipt(),
            repository,
            executor,
            PostgresEventRepository(session_factory),
            memory_jobs,
        )
        with pytest.raises(asyncio.CancelledError):
            await service.execute(claimed_command)

        async with session_factory() as session:
            run = await session.get(AgentRunModel, run_id)
            queued_command = await session.get(AgentCommandModel, command_id)
            outbox_count = await session.scalar(
                select(func.count()).select_from(OrchestrationOutboxModel)
            )
            memory_count = await session.scalar(
                select(func.count()).select_from(MemoryProcessingJobModel)
            )
        assert run is not None and run.status == "completed"
        assert json.loads(run.response_json) == final_result
        assert queued_command is not None and queued_command.status == "completed"
        assert outbox_count == 2
        assert memory_count == 1
        assert executor.calls == 1

        events = PostgresEventRepository(session_factory)
        dispatcher = OrchestrationOutboxDispatcher(
            PostgresOrchestrationOutboxRepository(
                session_factory,
                clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=1),
            ),
            events,
            worker_id=f"outbox-{run_id}",
        )
        delivered = [await dispatcher.dispatch_once() for _ in range(4)]
        assert delivered == [True, True, False, False]

        first_memory_attempt = await memory_jobs.claim_next_job()
        assert first_memory_attempt is not None
        assert first_memory_attempt["attempt"] == 1
        await memory_jobs.recover()
        recovered_memory_attempt = await memory_jobs.claim_next_job()
        assert recovered_memory_attempt is not None
        assert recovered_memory_attempt["attempt"] == 2
        await memory_jobs.mark_succeeded(run_id)

        assert await repository.complete(
            run_id, {"content": "I will keep reports concise."}
        ) is False
        async with session_factory() as session:
            answer_events = (
                await session.execute(
                    select(AgentEventModel).where(
                        AgentEventModel.run_id == run_id,
                        AgentEventModel.event_type.in_(
                            ("message.delta", "message.completed")
                        ),
                    )
                )
            ).scalars().all()
            completed_events = (
                await session.execute(
                    select(AgentEventModel).where(
                        AgentEventModel.run_id == run_id,
                        AgentEventModel.event_type == "run.completed",
                    )
                )
            ).scalars().all()
            memory_rows = (
                await session.execute(select(MemoryProcessingJobModel))
            ).scalars().all()
            outbox_rows = (
                await session.execute(select(OrchestrationOutboxModel))
            ).scalars().all()
        assert len(answer_events) == 1
        assert {event.message_id for event in answer_events} == {f"assistant:{run_id}"}
        assert json.loads(answer_events[0].payload_json)["content"] == final_result["content"]
        assert len(completed_events) == 1
        assert len(memory_rows) == 1
        assert memory_rows[0].status == "succeeded"
        assert memory_rows[0].user_text == "Remember that I prefer concise reports"
        assert memory_rows[0].assistant_text == "I will keep reports concise."
        assert len(outbox_rows) == 2
        assert all(row.status == "published" for row in outbox_rows)
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
