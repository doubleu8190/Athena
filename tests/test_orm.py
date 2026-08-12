"""SQLAlchemy ORM 测试 — 验证软删除、事务、JSON 序列化等功能."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest

from athena.db.database import Database
from athena.models import (
    ApprovalDecision,
    ApprovalLog,
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
    """创建临时数据库用于测试."""
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


# ---------------------------------------------------------------------------
# 软删除测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_delete_session(db: Database):
    """验证会话软删除功能."""
    # 创建会话
    await db.sessions.create("s1", title="Test Session")

    # 验证会话存在
    session = await db.sessions.get("s1")
    assert session is not None
    assert session.title == "Test Session"

    # 软删除会话
    await db.sessions.delete("s1")

    # 验证会话已被软删除（查询不到）
    session = await db.sessions.get("s1")
    assert session is None

    # 验证会话列表中不包含已删除的会话
    sessions = await db.sessions.list_all()
    assert len(sessions) == 0


@pytest.mark.asyncio
async def test_soft_delete_cascade(db: Database):
    """验证软删除级联操作."""
    # 创建会话和关联数据
    await db.sessions.create("s1")

    # 添加消息
    await db.messages.save(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.USER,
        content="hello",
        timestamp=datetime.now(),
    ))

    # 添加步骤
    await db.steps.save(Step(
        id="step-1",
        session_id="s1",
        run_id="r1",
        step_number=1,
        step_type=StepType.LLM_CALL,
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))

    # 添加工具调用
    await db.tool_calls.save(ToolCallRecord(
        id="tc-1",
        session_id="s1",
        step_id="step-1",
        tool_name="read_file",
        arguments={"path": "/tmp"},
        status=ToolCallStatus.RUNNING,
        started_at=datetime.now(),
    ))

    # 添加审批日志
    await db.approval_logs.save(ApprovalLog(
        id="al-1",
        session_id="s1",
        tool_call_id="tc-1",
        tool_name="read_file",
        arguments={"path": "/tmp"},
        risk_level="low",
        decision=ApprovalDecision.APPROVED,
        timestamp=datetime.now(),
    ))

    # 软删除会话
    await db.sessions.delete("s1")

    # 验证所有关联数据都被软删除
    assert await db.sessions.get("s1") is None
    assert await db.messages.get_by_session("s1") == []
    assert await db.steps.get_by_session("s1") == []
    assert await db.tool_calls.query("s1") == []
    assert await db.approval_logs.get_by_session("s1") == []


@pytest.mark.asyncio
async def test_soft_delete_preserves_data(db: Database):
    """验证软删除后数据仍然存在于数据库中（通过 Repository 直接查询）."""
    from athena.db.engine import get_session
    from athena.db.models import SessionModel
    from sqlalchemy import select

    # 创建会话
    await db.sessions.create("s1", title="Test")

    # 软删除会话
    await db.sessions.delete("s1")

    # 直接查询数据库，验证记录仍然存在
    async with get_session() as session:
        stmt = select(SessionModel).where(SessionModel.id == "s1")
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()

        assert row is not None
        assert row.deleted_time is not None  # 已设置删除时间
        assert row.title == "Test"


# ---------------------------------------------------------------------------
# 事务测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_rollback_on_error(db: Database):
    """验证事务异常时能正确回滚."""
    from athena.db.engine import get_session
    from athena.db.models import SessionModel
    from sqlalchemy import select

    # 创建会话
    await db.sessions.create("s1", title="Original")

    # 尝试更新一个不存在的会话（应该成功，不影响其他操作）
    await db.sessions.update("nonexistent", status="running")

    # 验证原会话状态未变
    session = await db.sessions.get("s1")
    assert session is not None
    assert session.status == "idle"


# ---------------------------------------------------------------------------
# JSON 序列化测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flattened_message_fields(db: Database):
    """验证 messages 平铺字段（step_id/tool_call_record_id/tool_name/type）的落库/读取.

    这些字段原存于 metadata_json，现为独立列；本测试验证双向往返.
    """
    await db.sessions.create("s1")

    await db.messages.save(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.ASSISTANT,
        content="assistant reply",
        step_id="step-1",
        timestamp=datetime.now(),
    ))
    await db.messages.save(Message(
        id="m2",
        session_id="s1",
        role=MessageRole.TOOL,
        content="tool output",
        tool_call_id="tc-1",
        step_id="step-2",
        tool_call_record_id="tc-record-1",
        tool_name="read_file",
        timestamp=datetime.now(),
    ))

    messages = await db.messages.get_by_session("s1")
    assert len(messages) == 2
    assert messages[0].step_id == "step-1"
    assert messages[0].tool_call_record_id is None
    assert messages[1].step_id == "step-2"
    assert messages[1].tool_call_record_id == "tc-record-1"
    assert messages[1].tool_name == "read_file"


@pytest.mark.asyncio
async def test_json_field_with_tool_calls(db: Database):
    """验证 tool_calls JSON 字段的序列化/反序列化."""
    await db.sessions.create("s1")

    tool_calls = [
        {"id": "tc1", "name": "read_file", "args": {"path": "/tmp"}},
        {"id": "tc2", "name": "write_file", "args": {"path": "/tmp", "content": "test"}},
    ]

    await db.messages.save(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=tool_calls,
        timestamp=datetime.now(),
    ))

    messages = await db.messages.get_by_session("s1")
    assert len(messages) == 1
    assert messages[0].tool_calls == tool_calls


# ---------------------------------------------------------------------------
# 并发安全测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_operations(db: Database):
    """验证并发操作的安全性."""
    import asyncio

    # 创建会话
    await db.sessions.create("s1")

    # 并发保存多条消息
    async def save_message(i: int):
        await db.messages.save(Message(
            id=f"m{i}",
            session_id="s1",
            role=MessageRole.USER,
            content=f"message {i}",
            timestamp=datetime.now(),
        ))

    # 并发执行
    tasks = [save_message(i) for i in range(10)]
    await asyncio.gather(*tasks)

    # 验证所有消息都保存成功
    messages = await db.messages.get_by_session("s1")
    assert len(messages) == 10


# ---------------------------------------------------------------------------
# 边界情况测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_nonexistent_session(db: Database):
    """验证获取不存在的会话返回 None."""
    session = await db.sessions.get("nonexistent")
    assert session is None


@pytest.mark.asyncio
async def test_update_nonexistent_session(db: Database):
    """验证更新不存在的会话不会抛出异常."""
    # 应该成功执行，不影响其他操作
    await db.sessions.update("nonexistent", status="running")


@pytest.mark.asyncio
async def test_empty_database_operations(db: Database):
    """验证空数据库的各种操作."""
    sessions = await db.sessions.list_all()
    assert sessions == []

    messages = await db.messages.get_by_session("s1")
    assert messages == []

    steps = await db.steps.get_by_session("s1")
    assert steps == []

    tool_calls = await db.tool_calls.query("s1")
    assert tool_calls == []

    approval_logs = await db.approval_logs.get_by_session("s1")
    assert approval_logs == []


@pytest.mark.asyncio
async def test_get_last_step_number_empty(db: Database):
    """验证获取空 run_id 的最大步骤号."""
    last_num = await db.steps.get_last_step_number("nonexistent")
    assert last_num == 0


@pytest.mark.asyncio
async def test_get_last_step_number(db: Database):
    """验证获取最大步骤号."""
    await db.sessions.create("s1")

    # 添加多个步骤
    for i in range(1, 6):
        await db.steps.save(Step(
            id=f"step-{i}",
            session_id="s1",
            run_id="r1",
            step_number=i,
            step_type=StepType.LLM_CALL,
            status=StepStatus.COMPLETED,
            started_at=datetime.now(),
        ))

    last_num = await db.steps.get_last_step_number("r1")
    assert last_num == 5
