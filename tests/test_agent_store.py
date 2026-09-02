"""Agent Store 命令入队、Run 分配和幂等行为测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_runtime.command_notifications import CommandNotifier
from athena.contracts.commands import Command, CommandType
from athena.contracts.errors import ErrorDetail
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.statuses import AgentRunStatus
from agent_runtime.streaming import StreamCoalescer
from athena.infrastructure.sqlite.agent_store import AgentStore
from athena.infrastructure.sqlite.database import Database
from athena.utils.ids import generate_session_id


@pytest.fixture
async def agent_store(tmp_path):
    """创建隔离的 Agent Store 测试数据库。"""
    database = Database(str(tmp_path / "agent-store.db"))
    await database.connect()
    yield database, AgentStore()
    await database.close()


def _message(
    session_id: str,
    command_id: str,
    content: str,
    *,
    run_id: str | None = None,
) -> Command:
    """构造消息提交命令。"""
    return Command(
        command_id=command_id,
        command_type=CommandType.MESSAGE_SUBMIT,
        session_id=session_id,
        run_id=run_id,
        payload={"message": content},
    )


@pytest.mark.asyncio
async def test_event_cursor_cache_initializes_each_maximum_once():
    """会话游标和流 Chunk 游标只从数据库初始化一次。"""
    store = AgentStore()
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[7, 3])

    assert await store._cached_session_seq(db, "session-1") == 7
    assert await store._cached_session_seq(db, "session-1") == 7
    assert await store._cached_stream_chunk_id(db, "session-1", "stream-1") == 3
    assert await store._cached_stream_chunk_id(db, "session-1", "stream-1") == 3
    assert db.scalar.await_count == 2


def test_stream_chunk_cache_never_moves_backward():
    """乱序或重试的较小 Chunk 不会让缓存游标回退。"""
    store = AgentStore()
    cache_key = ("session-1", "stream-1")
    store._stream_chunk_cache[cache_key] = 5

    store._advance_stream_chunk_cache(cache_key, 3)
    assert store._stream_chunk_cache[cache_key] == 5

    store._advance_stream_chunk_cache(cache_key, 6)
    assert store._stream_chunk_cache[cache_key] == 6


@pytest.mark.asyncio
async def test_message_enqueue_assigns_and_reuses_run_id(agent_store):
    """首次入队生成 Run，重复命令复用数据库中的 Run。"""
    database, store = agent_store
    session = await database.sessions.create(generate_session_id(), "测试")

    original = _message(session.id, "command-1", "你好")
    assert await store.enqueue(original) is True
    assert original.run_id

    retry = _message(session.id, "command-1", "你好", run_id="new-candidate")
    assert await store.enqueue(retry) is False
    assert retry.run_id == original.run_id


@pytest.mark.asyncio
async def test_same_command_id_with_different_schema_is_conflict(agent_store):
    """相同命令 ID 但协议版本不同不能被当作幂等重试。"""
    database, store = agent_store
    session = await database.sessions.create(generate_session_id(), "测试")

    original = _message(session.id, "command-1", "你好")
    assert await store.enqueue(original) is True

    conflicting = _message(session.id, "command-1", "你好")
    conflicting.schema_version = 2
    with pytest.raises(ValueError, match=ErrorDetail.COMMAND_ID_CONFLICT):
        await store.enqueue(conflicting)


@pytest.mark.asyncio
async def test_message_run_id_does_not_bypass_session_busy(agent_store):
    """即使消息携带 Run ID，也必须执行会话活跃 Run 检查。"""
    database, store = agent_store
    session = await database.sessions.create(generate_session_id(), "测试")

    first = _message(session.id, "command-1", "第一条", run_id="run-1")
    assert await store.enqueue(first) is True

    second = _message(session.id, "command-2", "第二条", run_id="run-2")
    with pytest.raises(ValueError, match=ErrorDetail.SESSION_BUSY):
        await store.enqueue(second)


@pytest.mark.asyncio
async def test_message_after_pause_creates_a_new_run(agent_store):
    """暂停旧 Run 后提交新消息时创建新的 Run。"""
    database, store = agent_store
    session = await database.sessions.create(generate_session_id(), "测试")

    first = _message(session.id, "command-1", "第一条", run_id="run-1")
    assert await store.enqueue(first) is True
    await store.update_run_control(
        first.run_id, pause=True, status=AgentRunStatus.PAUSED
    )

    second = _message(session.id, "command-2", "第二条", run_id="run-2")
    assert await store.enqueue(second) is True
    assert second.run_id == "run-2"
    assert second.run_id != first.run_id

    runs = await store.runs_for_session(session.id)
    assert {(run.run_id, run.status) for run in runs} == {
        ("run-1", AgentRunStatus.PAUSED.value),
        ("run-2", AgentRunStatus.QUEUED.value),
    }


@pytest.mark.asyncio
async def test_enqueue_notifies_runtime_after_commit(tmp_path):
    """新命令提交成功后，通知器才唤醒 Runtime。"""
    notifier = CommandNotifier()
    database = Database(str(tmp_path / "notifier.db"))
    await database.connect()
    store = AgentStore(command_notifier=notifier)
    session = await database.sessions.create(generate_session_id(), "测试")

    waiting = asyncio.create_task(notifier.wait())
    command = _message(session.id, "command-1", "你好")
    assert await store.enqueue(command) is True
    await asyncio.wait_for(waiting, timeout=1)

    await database.close()


@pytest.mark.asyncio
async def test_events_share_session_sequence_across_durability_levels(agent_store):
    """Realtime 和 Durable 事件都使用同一个可恢复的会话游标。"""
    database, _ = agent_store
    session = await database.sessions.create(generate_session_id(), "事件")
    store = AgentStore()

    first = await store.publish(
        ApplicationEvent(
            event_type=EventType.LLM_TOKEN,
            durability=EventDurability.REALTIME,
            session_id=session.id,
            run_id="run-1",
            stream_id="answer-run-1",
            stream_type="answer",
            chunk_id=1,
            payload={"delta": "一"},
        )
    )
    second = await store.publish(
        ApplicationEvent(
            event_type=EventType.TOOL_CALL_START,
            durability=EventDurability.DURABLE,
            session_id=session.id,
            run_id="run-1",
            payload={"tool_name": "search"},
        )
    )

    assert (first.session_seq, second.session_seq) == (1, 2)
    rows = await store.events_after(session.id)
    assert [row.session_seq for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_stream_chunk_is_idempotent_and_conflicts_are_rejected(agent_store):
    """重复 Chunk 不重复入库；相同幂等键的内容变化必须失败。"""
    database, _ = agent_store
    session = await database.sessions.create(generate_session_id(), "Chunk")
    store = AgentStore()
    event = ApplicationEvent(
        event_type=EventType.LLM_TOKEN,
        durability=EventDurability.REALTIME,
        session_id=session.id,
        run_id="run-1",
        stream_id="answer-run-1",
        stream_type="answer",
        chunk_id=1,
        payload={"delta": "same"},
    )

    first = await store.publish(event)
    retry = await store.publish(event)
    assert retry.session_seq == first.session_seq
    assert len(await store.events_after(session.id)) == 1

    with pytest.raises(ValueError, match="idempotency conflict"):
        await store.publish(event.model_copy(update={"payload": {"delta": "other"}}))


@pytest.mark.asyncio
async def test_concurrent_event_publish_is_serialized(agent_store):
    """同一会话的并发生产者不能获得重复或交叉的 session_seq。"""
    database, _ = agent_store
    session = await database.sessions.create(generate_session_id(), "并发")
    store = AgentStore()

    events = [
        ApplicationEvent(
            event_type=EventType.RUN_STARTED,
            durability=EventDurability.DURABLE,
            session_id=session.id,
            run_id=f"run-{index}",
            payload={"index": index},
        )
        for index in range(10)
    ]
    persisted = await asyncio.gather(*(store.publish(event) for event in events))

    assert sorted(event.session_seq for event in persisted) == list(range(1, 11))


@pytest.mark.asyncio
async def test_stream_coalescer_emits_chunk_protocol(agent_store):
    """合并器生成连续的 Chunk 序号和 UTF-8 字节偏移。"""
    database, _ = agent_store
    session = await database.sessions.create(generate_session_id(), "流")
    store = AgentStore()
    coalescer = StreamCoalescer(
        session_id=session.id,
        run_id="run-1",
        stream_id="answer-run-1",
        publish=store.publish,
        max_bytes=1,
    )

    await coalescer.append("你")
    await coalescer.append("好")
    rows = await store.events_after(session.id)
    assert [(row.chunk_id, row.payload_json) for row in rows] == [
        (1, '{"base_version": 0, "chunk_id": 1, "delta": "你", "end_offset": 3, "start_offset": 0, "stream_id": "answer-run-1"}'),
        (2, '{"base_version": 1, "chunk_id": 2, "delta": "好", "end_offset": 6, "start_offset": 3, "stream_id": "answer-run-1"}'),
    ]
