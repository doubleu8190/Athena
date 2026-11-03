"""PostgreSQL 会话历史一致性快照测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from infrastructure.persistence.postgres.models import (
    AgentEventModel,
    AgentRunModel,
    Base,
    MessageModel,
    ProjectModel,
    SessionModel,
)
from infrastructure.persistence.postgres.repositories import (
    PostgresSessionHistoryRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_history_snapshot_reads_runs_messages_events_and_cursor_atomically() -> None:
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"session_history_{uuid4().hex}"
    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        ProjectModel.__table__,
                        SessionModel.__table__,
                        MessageModel.__table__,
                        AgentRunModel.__table__,
                        AgentEventModel.__table__,
                    ],
                    checkfirst=False,
                )
            )

        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session_id = f"session-{uuid4().hex}"
        run_id = f"run-{uuid4().hex}"
        active_run_id = f"run-{uuid4().hex}"
        user_message_id = f"message-{uuid4().hex}"
        assistant_message_id = f"assistant:{run_id}"
        older_run_id = f"run-{uuid4().hex}"
        older_user_message_id = f"message-{uuid4().hex}"
        older_assistant_message_id = f"assistant:{older_run_id}"
        async with factory() as session, session.begin():
            project = ProjectModel(
                id=f"project-{uuid4().hex}",
                name="History",
                created_at=now,
                updated_at=now,
            )
            session.add(project)
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project.id,
                    title="History",
                    created_at=now,
                    updated_at=now,
                    last_activity_at=now,
                )
            )
            session.add_all(
                [
                    AgentRunModel(
                        run_id=older_run_id,
                        session_id=session_id,
                        message_id=older_user_message_id,
                        sequence=1,
                        status="completed",
                        request_json={},
                        response_json={"content": "更早答复"},
                        root_thread_id=f"thread-{older_run_id}",
                        created_at=now,
                        updated_at=now,
                    ),
                    AgentRunModel(
                        run_id=run_id,
                        session_id=session_id,
                        message_id=user_message_id,
                        sequence=2,
                        status="completed",
                        request_json={},
                        response_json={"content": "答复"},
                        root_thread_id=f"thread-{run_id}",
                        created_at=now,
                        updated_at=now,
                    ),
                    AgentRunModel(
                        run_id=active_run_id,
                        session_id=session_id,
                        message_id=f"message-{uuid4().hex}",
                        sequence=3,
                        status="running",
                        request_json={},
                        root_thread_id=f"thread-{active_run_id}",
                        created_at=now,
                        updated_at=now,
                    ),
                    MessageModel(
                        id=older_user_message_id,
                        session_id=session_id,
                        role="user",
                        content="更早问题",
                        tool_calls_json=[],
                        run_id=older_run_id,
                        timestamp=now,
                    ),
                    MessageModel(
                        id=older_assistant_message_id,
                        session_id=session_id,
                        role="assistant",
                        content="更早答复",
                        tool_calls_json=[],
                        run_id=older_run_id,
                        timestamp=now,
                    ),
                    MessageModel(
                        id=user_message_id,
                        session_id=session_id,
                        role="user",
                        content="问题",
                        tool_calls_json=[],
                        run_id=run_id,
                        timestamp=now,
                    ),
                    MessageModel(
                        id=assistant_message_id,
                        session_id=session_id,
                        role="assistant",
                        content="答复",
                        tool_calls_json=[],
                        run_id=run_id,
                        timestamp=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=1,
                        run_id=older_run_id,
                        event_type="run.completed",
                        durability="durable",
                        payload_json={"summary": "older completed"},
                        occurred_at=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=2,
                        run_id=run_id,
                        event_type="run.completed",
                        durability="durable",
                        payload_json={"summary": "completed"},
                        occurred_at=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=3,
                        run_id=active_run_id,
                        event_type="run.started",
                        durability="durable",
                        payload_json={"summary": "running"},
                        occurred_at=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=4,
                        run_id=run_id,
                        event_type="run.completed",
                        durability="durable",
                        payload_json={"summary": "terminal outbox arrived after active event"},
                        occurred_at=now,
                    ),
                ]
            )

        snapshot = await PostgresSessionHistoryRepository(factory).read_snapshot(
            session_id,
            before_sequence=None,
            limit=1,
        )

        assert [run.run_id for run in snapshot.runs] == [run_id]
        assert {message.id for message in snapshot.messages} == {
            user_message_id,
            assistant_message_id,
        }
        assert [event.session_seq for event in snapshot.events] == [2, 4]
        # active 最早事件是 3；即使终态 outbox 的 4 更新，也不得越过 3。
        assert snapshot.snapshot_event_seq == 2
        assert snapshot.has_more is True

        older_snapshot = await PostgresSessionHistoryRepository(factory).read_snapshot(
            session_id,
            before_sequence=2,
            limit=1,
        )
        assert [run.run_id for run in older_snapshot.runs] == [older_run_id]
        assert {message.id for message in older_snapshot.messages} == {
            older_user_message_id,
            older_assistant_message_id,
        }
        assert [event.session_seq for event in older_snapshot.events] == [1]
        assert older_snapshot.snapshot_event_seq == 1
        assert older_snapshot.has_more is False
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
async def test_history_snapshot_watermark_stops_before_unselected_visible_event() -> None:
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"session_history_gap_{uuid4().hex}"
    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        ProjectModel.__table__,
                        SessionModel.__table__,
                        MessageModel.__table__,
                        AgentRunModel.__table__,
                        AgentEventModel.__table__,
                    ],
                    checkfirst=False,
                )
            )

        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session_id = f"session-{uuid4().hex}"
        selected_run_id = f"run-{uuid4().hex}"
        selected_message_id = f"message-{uuid4().hex}"
        unselected_run_id = f"run-{uuid4().hex}"
        async with factory() as session, session.begin():
            project = ProjectModel(
                id=f"project-{uuid4().hex}",
                name="History",
                created_at=now,
                updated_at=now,
            )
            session.add(project)
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project.id,
                    title="History",
                    created_at=now,
                    updated_at=now,
                    last_activity_at=now,
                )
            )
            session.add_all(
                [
                    AgentRunModel(
                        run_id=unselected_run_id,
                        session_id=session_id,
                        message_id=f"message-{uuid4().hex}",
                        sequence=0,
                        status="completed",
                        request_json={},
                        root_thread_id=f"thread-{unselected_run_id}",
                        created_at=now,
                        updated_at=now,
                    ),
                    AgentRunModel(
                        run_id=selected_run_id,
                        session_id=session_id,
                        message_id=selected_message_id,
                        sequence=1,
                        status="completed",
                        request_json={},
                        root_thread_id=f"thread-{selected_run_id}",
                        created_at=now,
                        updated_at=now,
                    ),
                    MessageModel(
                        id=selected_message_id,
                        session_id=session_id,
                        role="user",
                        content="当前页问题",
                        tool_calls_json=[],
                        run_id=selected_run_id,
                        timestamp=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=2,
                        run_id=selected_run_id,
                        event_type="run.completed",
                        durability="durable",
                        payload_json={"summary": "first visible page event"},
                        occurred_at=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=3,
                        run_id=unselected_run_id,
                        event_type="run.completed",
                        durability="durable",
                        payload_json={"summary": "unselected terminal gap"},
                        occurred_at=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=4,
                        run_id=selected_run_id,
                        event_type="task.completed",
                        durability="durable",
                        payload_json={"summary": "second visible page event"},
                        occurred_at=now,
                    ),
                ]
            )

        snapshot = await PostgresSessionHistoryRepository(factory).read_snapshot(
            session_id,
            before_sequence=None,
            limit=1,
        )

        assert [run.run_id for run in snapshot.runs] == [selected_run_id]
        assert [event.session_seq for event in snapshot.events] == [2, 4]
        assert snapshot.snapshot_event_seq == 2
        assert snapshot.has_more is True
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
async def test_history_snapshot_watermark_skips_hidden_output_events() -> None:
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"session_history_hidden_{uuid4().hex}"
    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        ProjectModel.__table__,
                        SessionModel.__table__,
                        MessageModel.__table__,
                        AgentRunModel.__table__,
                        AgentEventModel.__table__,
                    ],
                    checkfirst=False,
                )
            )

        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session_id = f"session-{uuid4().hex}"
        run_id = f"run-{uuid4().hex}"
        message_id = f"message-{uuid4().hex}"
        async with factory() as session, session.begin():
            project = ProjectModel(
                id=f"project-{uuid4().hex}",
                name="History",
                created_at=now,
                updated_at=now,
            )
            session.add(project)
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project.id,
                    title="History",
                    created_at=now,
                    updated_at=now,
                    last_activity_at=now,
                )
            )
            session.add_all(
                [
                    AgentRunModel(
                        run_id=run_id,
                        session_id=session_id,
                        message_id=message_id,
                        sequence=1,
                        status="completed",
                        request_json={},
                        root_thread_id=f"thread-{run_id}",
                        display_cutoff_session_seq=2,
                        created_at=now,
                        updated_at=now,
                    ),
                    MessageModel(
                        id=message_id,
                        session_id=session_id,
                        role="user",
                        content="停止后的历史问题",
                        tool_calls_json=[],
                        run_id=run_id,
                        timestamp=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=2,
                        run_id=run_id,
                        event_type="run.completed",
                        durability="durable",
                        payload_json={"summary": "visible before cutoff"},
                        occurred_at=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=3,
                        run_id=run_id,
                        event_type="message.delta",
                        durability="durable",
                        payload_json={"delta": "hidden stopped output"},
                        occurred_at=now,
                    ),
                    AgentEventModel(
                        session_id=session_id,
                        session_seq=4,
                        run_id=run_id,
                        event_type="task.completed",
                        durability="durable",
                        payload_json={"summary": "visible after hidden output"},
                        occurred_at=now,
                    ),
                ]
            )

        snapshot = await PostgresSessionHistoryRepository(factory).read_snapshot(
            session_id,
            before_sequence=None,
            limit=20,
        )

        assert [event.session_seq for event in snapshot.events] == [2, 4]
        assert snapshot.snapshot_event_seq == 4
        assert snapshot.has_more is False
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
async def test_history_snapshot_without_terminal_runs_does_not_advance_cursor() -> None:
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"session_history_empty_{uuid4().hex}"
    url = make_url(DATABASE_URL)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    test_engine = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        ProjectModel.__table__,
                        SessionModel.__table__,
                        MessageModel.__table__,
                        AgentRunModel.__table__,
                        AgentEventModel.__table__,
                    ],
                    checkfirst=False,
                )
            )
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session_id = f"session-{uuid4().hex}"
        run_id = f"run-{uuid4().hex}"
        async with factory() as session, session.begin():
            project = ProjectModel(
                id=f"project-{uuid4().hex}",
                name="History",
                created_at=now,
                updated_at=now,
            )
            session.add(project)
            await session.flush()
            session.add(
                SessionModel(
                    id=session_id,
                    project_id=project.id,
                    title="History",
                    created_at=now,
                    updated_at=now,
                    last_activity_at=now,
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
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AgentEventModel(
                    session_id=session_id,
                    session_seq=7,
                    run_id=run_id,
                    event_type="run.started",
                    durability="durable",
                    payload_json={"summary": "running"},
                    occurred_at=now,
                )
            )

        snapshot = await PostgresSessionHistoryRepository(factory).read_snapshot(
            session_id,
            before_sequence=None,
            limit=20,
        )

        assert snapshot.runs == ()
        assert snapshot.messages == ()
        assert snapshot.events == ()
        assert snapshot.snapshot_event_seq == 0
        assert snapshot.has_more is False
    finally:
        await test_engine.dispose()
        if created:
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA "{schema}" CASCADE')
                )
        await admin_engine.dispose()
