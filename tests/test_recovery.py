"""被动会话恢复回归测试 — 进程中断遗留的 running steps/tool_calls 应被标记为 failed.

背景：此前 recover_interrupted_sessions 只把 session 状态重置为 idle，
遗留的 running steps/tool_calls 永久卡 running。本测试验证清理逻辑。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest

from athena.db.database import Database
from athena.gateway.recovery import recover_interrupted_sessions
from athena.models.step import Step, StepStatus, StepType
from athena.models.tool import ToolCallRecord, ToolCallStatus


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
async def test_recovery_cleans_orphaned_running_steps(db: Database):
    """进程中断遗留的 running step / tool_call 恢复时应统一标记为 failed."""
    await db.create_session("s-orphan", title="orphan")
    # 模拟进程中断现场：session running + running step + running tool_call
    await db.update_session("s-orphan", status="running")
    await db.save_step(Step(
        id="step-1",
        session_id="s-orphan",
        run_id="20260808",
        step_number=1,
        step_type=StepType.LLM_CALL,
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.save_step(Step(
        id="step-2",
        session_id="s-orphan",
        run_id="20260808",
        step_number=2,
        step_type=StepType.TOOL_EXECUTION,
        parent_step_id="step-1",
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.save_tool_call(ToolCallRecord(
        id="tc-1",
        session_id="s-orphan",
        step_id="step-2",
        tool_name="exec_shell",
        arguments={"command": "x"},
        status=ToolCallStatus.RUNNING,
        started_at=datetime.now(),
    ))

    await recover_interrupted_sessions(db)

    session = await db.get_session("s-orphan")
    assert session.status.value == "idle"

    steps = await db.get_steps("s-orphan")
    assert steps
    assert all(s.status.value == "failed" for s in steps)
    assert all("进程中断" in (s.error_message or "") for s in steps)

    tool_calls = await db.query_tool_calls("s-orphan")
    assert tool_calls
    assert all(tc.status.value == "failed" for tc in tool_calls)
    assert all("进程中断" in (tc.error_message or "") for tc in tool_calls)

    # 关键：修复后查询不应抛 ValueError（此前写入非法状态会令 _row_to_* 崩溃）
    # 此处 get_steps / query_tool_calls 已隐式验证枚举转换安全


@pytest.mark.asyncio
async def test_recovery_ignores_idle_sessions(db: Database):
    """idle 会话不应被恢复逻辑触碰."""
    await db.create_session("s-idle", title="idle")
    await db.save_step(Step(
        id="step-1",
        session_id="s-idle",
        run_id="20260808",
        step_number=1,
        step_type=StepType.LLM_CALL,
        status=StepStatus.COMPLETED,
        started_at=datetime.now(),
    ))

    await recover_interrupted_sessions(db)

    session = await db.get_session("s-idle")
    assert session.status.value == "idle"
    steps = await db.get_steps("s-idle")
    assert steps
    assert steps[0].status.value == "completed"  # 未被误改
