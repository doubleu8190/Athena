"""数据库 CRUD 测试."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest

from athena.db.database import Database
from athena.models import (
    Message,
    MessageRole,
    Step,
    StepStatus,
    StepType,
    ToolCallRecord,
    ToolCallStatus,
)


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    await database.connect()
    yield database
    await database.close()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_create_and_get_session(db: Database):
    session = await db.sessions.create("sess-1", title="Test")
    assert session.id == "sess-1"
    assert session.title == "Test"
    assert session.status == "idle"

    fetched = await db.sessions.get("sess-1")
    assert fetched is not None
    assert fetched.title == "Test"
    assert fetched.status == "idle"


@pytest.mark.asyncio
async def test_list_sessions(db: Database):
    await db.sessions.create("s1")
    await db.sessions.create("s2")
    sessions = await db.sessions.list_all()
    assert len(sessions) == 2


@pytest.mark.asyncio
async def test_update_session_status(db: Database):
    await db.sessions.create("s1")
    await db.sessions.update("s1", status="running", run_id="20260730")
    fetched = await db.sessions.get("s1")
    assert fetched is not None
    assert fetched.status == "running"
    assert fetched.run_id == "20260730"


@pytest.mark.asyncio
async def test_save_and_get_messages(db: Database):
    await db.sessions.create("s1")
    await db.messages.save(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.USER,
        content="hello",
        timestamp=datetime.now(),
    ))
    await db.messages.save(Message(
        id="m2",
        session_id="s1",
        role=MessageRole.ASSISTANT,
        content="hi",
        timestamp=datetime.now(),
    ))
    msgs = await db.messages.get_by_session("s1")
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"


@pytest.mark.asyncio
async def test_save_step_and_query(db: Database):
    await db.sessions.create("s1")
    await db.steps.save(Step(
        id="step-1",
        session_id="s1",
        run_id="r1",
        step_number=1,
        step_type=StepType.LLM_CALL,
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.steps.update("step-1", {
        "status": "completed", "duration_ms": 100.0,
        "llm_input_tokens": 50, "llm_output_tokens": 30,
    })
    steps = await db.steps.get_by_session("s1")
    assert len(steps) == 1
    assert steps[0].status == "completed"
    assert steps[0].llm_input_tokens == 50


@pytest.mark.asyncio
async def test_save_tool_call(db: Database):
    await db.sessions.create("s1")
    await db.steps.save(Step(
        id="step-1",
        session_id="s1",
        run_id="r1",
        step_number=1,
        step_type=StepType.TOOL_EXECUTION,
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.tool_calls.save(ToolCallRecord(
        id="tc-1",
        session_id="s1",
        step_id="step-1",
        tool_name="read_file",
        arguments={"path": "/tmp"},
        status=ToolCallStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.tool_calls.update("tc-1", {
        "status": "success", "raw_output": "data",
        "duration_ms": 50.0, "completed_at": datetime.now().isoformat(),
    })
    calls = await db.tool_calls.query("s1")
    assert len(calls) == 1
    assert calls[0].status == "success"


@pytest.mark.asyncio
async def test_delete_session_cascade(db: Database):
    await db.sessions.create("s1")
    await db.messages.save(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.USER,
        content="x",
        timestamp=datetime.now(),
    ))
    await db.sessions.delete("s1")
    assert await db.sessions.get("s1") is None
    assert await db.messages.get_by_session("s1") == []


@pytest.mark.asyncio
async def test_migration_v4_adds_parent_run_id(tmp_path):
    """模拟 v3 库升级 v4：steps 表新增 parent_run_id 列，user_version 递增."""
    import aiosqlite

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from athena.db.engine import _run_migrations

    db_file = tmp_path / "migrate.db"
    # 手工构造 v3 库：steps 表不含 parent_run_id，user_version=3
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.execute("""
            CREATE TABLE steps (
                id VARCHAR PRIMARY KEY, session_id VARCHAR, run_id VARCHAR,
                step_number INTEGER, step_type VARCHAR, parent_step_id VARCHAR,
                status VARCHAR, started_at VARCHAR, completed_at VARCHAR,
                duration_ms REAL, llm_input_tokens INTEGER, llm_output_tokens INTEGER,
                error_message TEXT, metadata_json TEXT, deleted_time VARCHAR
            )
        """)
        await conn.execute("PRAGMA user_version = 3;")
        await conn.commit()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        async with engine.begin() as conn:
            await _run_migrations(conn)
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA table_info(steps)"))
            cols = [row[1] for row in result.fetchall()]
            assert "parent_run_id" in cols
            result = await conn.execute(text("PRAGMA user_version;"))
            assert result.scalar() == 5
    finally:
        await engine.dispose()
