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
    session = await db.create_session("sess-1", title="Test")
    assert session.id == "sess-1"
    assert session.title == "Test"
    assert session.status == "idle"

    fetched = await db.get_session("sess-1")
    assert fetched is not None
    assert fetched.title == "Test"
    assert fetched.status == "idle"


@pytest.mark.asyncio
async def test_list_sessions(db: Database):
    await db.create_session("s1")
    await db.create_session("s2")
    sessions = await db.list_sessions()
    assert len(sessions) == 2


@pytest.mark.asyncio
async def test_update_session_status(db: Database):
    await db.create_session("s1")
    await db.update_session("s1", status="running", run_id="20260730")
    fetched = await db.get_session("s1")
    assert fetched is not None
    assert fetched.status == "running"
    assert fetched.run_id == "20260730"


@pytest.mark.asyncio
async def test_save_and_get_messages(db: Database):
    await db.create_session("s1")
    await db.save_message(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.USER,
        content="hello",
        timestamp=datetime.now(),
    ))
    await db.save_message(Message(
        id="m2",
        session_id="s1",
        role=MessageRole.ASSISTANT,
        content="hi",
        timestamp=datetime.now(),
    ))
    msgs = await db.get_messages("s1")
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"


@pytest.mark.asyncio
async def test_save_step_and_query(db: Database):
    await db.create_session("s1")
    await db.save_step(Step(
        id="step-1",
        session_id="s1",
        run_id="r1",
        step_number=1,
        step_type=StepType.LLM_CALL,
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.update_step("step-1", {
        "status": "completed", "duration_ms": 100.0,
        "llm_input_tokens": 50, "llm_output_tokens": 30,
    })
    steps = await db.get_steps("s1")
    assert len(steps) == 1
    assert steps[0].status == "completed"
    assert steps[0].llm_input_tokens == 50


@pytest.mark.asyncio
async def test_save_tool_call(db: Database):
    await db.create_session("s1")
    await db.save_step(Step(
        id="step-1",
        session_id="s1",
        run_id="r1",
        step_number=1,
        step_type=StepType.TOOL_EXECUTION,
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.save_tool_call(ToolCallRecord(
        id="tc-1",
        session_id="s1",
        step_id="step-1",
        tool_name="read_file",
        arguments={"path": "/tmp"},
        status=ToolCallStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.update_tool_call("tc-1", {
        "status": "success", "raw_output": "data",
        "duration_ms": 50.0, "completed_at": datetime.now().isoformat(),
    })
    calls = await db.query_tool_calls("s1")
    assert len(calls) == 1
    assert calls[0].status == "success"


@pytest.mark.asyncio
async def test_delete_session_cascade(db: Database):
    await db.create_session("s1")
    await db.save_message(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.USER,
        content="x",
        timestamp=datetime.now(),
    ))
    await db.delete_session("s1")
    assert await db.get_session("s1") is None
    assert await db.get_messages("s1") == []
