"""Agent Store 命令入队、Run 分配和幂等行为测试。"""

from __future__ import annotations

import asyncio

import pytest

from athena.runtime.command_notifications import CommandNotifier
from athena.contracts.commands import Command, CommandType
from athena.contracts.errors import ErrorDetail
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.statuses import AgentRunStatus
from athena.runtime.stream_coalescer import StreamCoalescer
from athena.runtime.transport import SessionEventBus
from athena.infrastructure.postgre.repositories.agent_store import AgentStore
from athena.infrastructure.postgre.models import AgentEventModel
from athena.models.tool import RiskLevel
from athena.contracts.statuses import AgentApprovalDecision
from athena.infrastructure.postgre.database import Database
from athena.infrastructure.postgre.engine import get_session
from athena.utils.id_generation import generate_session_id


@pytest.fixture
async def agent_store(postgres_url):
    """创建隔离的 Agent Store 测试数据库。"""
    database = Database(postgres_url)
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


def test_agent_event_schema_has_no_persisted_chunk_id() -> None:
    """数据库事件模型不再声明实时 Chunk 字段。"""
    assert "chunk_id" not in AgentEventModel.__table__.columns


@pytest.mark.asyncio
async def test_message_enqueue_assigns_and_reuses_run_id(agent_store):
    """首次入队生成 Run，重复命令复用数据库中的 Run。"""
    database, store = agent_store
    session = await database.sessions.create(generate_session_id(), "测试")

    original = _message(session.id, "command-1", "你好")
    assert await store.enqueue_command(original) is True
    assert original.run_id

    retry = _message(session.id, "command-1", "你好", run_id="new-candidate")
    assert await store.enqueue_command(retry) is False
    assert retry.run_id == original.run_id


@pytest.mark.asyncio
async def test_same_command_id_with_different_schema_is_conflict(agent_store):
    """相同命令 ID 但协议版本不同不能被当作幂等重试。"""
    database, store = agent_store
    session = await database.sessions.create(generate_session_id(), "测试")

    original = _message(session.id, "command-1", "你好")
    assert await store.enqueue_command(original) is True

    conflicting = _message(session.id, "command-1", "你好")
    conflicting.schema_version = 2
    with pytest.raises(ValueError, match=ErrorDetail.COMMAND_ID_CONFLICT):
        await store.enqueue_command(conflicting)


@pytest.mark.asyncio
async def test_message_run_id_does_not_bypass_session_busy(agent_store):
    """即使消息携带 Run ID，也必须执行会话活跃 Run 检查。"""
    database, store = agent_store
    session = await database.sessions.create(generate_session_id(), "测试")

    first = _message(session.id, "command-1", "第一条", run_id="run-1")
    assert await store.enqueue_command(first) is True

    second = _message(session.id, "command-2", "第二条", run_id="run-2")
    with pytest.raises(ValueError, match=ErrorDetail.SESSION_BUSY):
        await store.enqueue_command(second)


@pytest.mark.asyncio
async def test_message_after_pause_creates_a_new_run(agent_store):
    """暂停旧 Run 后提交新消息时创建新的 Run。"""
    database, store = agent_store
    session = await database.sessions.create(generate_session_id(), "测试")

    first = _message(session.id, "command-1", "第一条", run_id="run-1")
    assert await store.enqueue_command(first) is True
    await store.update_run_control(
        first.run_id, pause=True, status=AgentRunStatus.PAUSED
    )

    second = _message(session.id, "command-2", "第二条", run_id="run-2")
    assert await store.enqueue_command(second) is True
    assert second.run_id == "run-2"
    assert second.run_id != first.run_id


@pytest.mark.asyncio
async def test_approval_cannot_be_resolved_for_stale_worker_attempt(agent_store):
    """迟到审批不得恢复已重试或已取消的 Worker 尝试。"""

    database, store = agent_store
    session = await database.sessions.create(generate_session_id(), "审批")
    await store.create_worker_run(
        run_id="worker-1",
        session_id=session.id,
        parent_run_id="root-1",
        attempt=1,
    )
    await store.create_approval(
        approval_id="approval-1",
        session_id=session.id,
        run_id="root-1",
        tool_call_id="call-1",
        tool_name="exec_shell",
        arguments={"command": "ls"},
        risk_level=RiskLevel.MEDIUM,
        plan_id="plan-1",
        task_id="task-1",
        worker_run_id="worker-1",
        approval_batch_id="batch-1",
    )

    assert await store.resolve_approval_batch(
        "batch-1", {"approval-1": AgentApprovalDecision.APPROVED}, expected_run_id="root-1"
    ) is True
    assert await store.resolve_approval_batch(
        "batch-1", {"approval-1": AgentApprovalDecision.APPROVED}, expected_run_id="root-1"
    ) is False

    approval = await store.get_approval("approval-1")
    assert approval is not None
    assert approval.status == "resolved"


@pytest.mark.asyncio
async def test_enqueue_notifies_runtime_after_commit(postgres_url):
    """新命令提交成功后，通知器才唤醒 Runtime。"""
    notifier = CommandNotifier()
    database = Database(postgres_url)
    await database.connect()
    store = AgentStore(command_notifier=notifier)
    session = await database.sessions.create(generate_session_id(), "测试")

    waiting = asyncio.create_task(notifier.wait())
    command = _message(session.id, "command-1", "你好")
    assert await store.enqueue_command(command) is True
    await asyncio.wait_for(waiting, timeout=1)

    await database.close()


@pytest.mark.asyncio
async def test_worker_run_is_independent_from_root_session(agent_store):
    """Worker Run 可以并行存在，但不会取代会话的 Root Run。"""

    database, _ = agent_store
    session = await database.sessions.create(generate_session_id(), "Worker Run")
    store = AgentStore()
    command = _message(session.id, "command-root", "执行任务", run_id="root-1")
    assert await store.enqueue_command(command) is True

    await store.create_worker_run(
        run_id="worker-1",
        session_id=session.id,
        parent_run_id="root-1",
        attempt=1,
    )

    active = await store.get_active_run(session.id)
    worker = await store.get_run("worker-1")
    assert active is not None and active.run_id == "root-1"
    assert worker is not None
    assert worker.parent_run_id == "root-1"


@pytest.mark.asyncio
async def test_realtime_events_are_broadcast_without_persistence(agent_store):
    """Realtime 事件只广播给在线订阅者，不占用会话游标。"""
    database, _ = agent_store
    session = await database.sessions.create(generate_session_id(), "事件")
    store = AgentStore(transport=SessionEventBus())

    queue, _ = await store.open_subscription(session.id)
    first = await store.publish_realtime(
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

    assert first.session_seq is None
    assert second.session_seq == 1
    assert (await queue.get()).session_seq is None
    rows = await store.list_events_after(session.id)
    assert [row.session_seq for row in rows] == [1]


@pytest.mark.asyncio
async def test_durable_event_transition_id_is_idempotent(agent_store):
    database, _ = agent_store
    session = await database.sessions.create(generate_session_id(), "事件幂等")
    store = AgentStore(transport=SessionEventBus())
    event = ApplicationEvent(
        event_type=EventType.RUN_STARTED,
        durability=EventDurability.DURABLE,
        session_id=session.id,
        run_id="run-1",
        transition_id="run:run-1:started",
        payload={"message_id": "message-1"},
    )

    first = await store.publish(event)
    retry = await store.publish(event)

    assert retry.session_seq == first.session_seq
    assert len(await store.list_events_after(session.id)) == 1

    with pytest.raises(ValueError, match="transition idempotency conflict"):
        await store.publish(event.model_copy(update={"payload": {"other": True}}))


@pytest.mark.asyncio
async def test_llm_token_must_use_realtime_publish(agent_store):
    """LLM Chunk 不得进入持久化事件表。"""
    database, _ = agent_store
    session = await database.sessions.create(generate_session_id(), "Chunk")
    store = AgentStore(transport=SessionEventBus())
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

    with pytest.raises(ValueError, match="publish_realtime"):
        await store.publish(event)
    assert len(await store.list_events_after(session.id)) == 0


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
    """合并器生成实时 Chunk，并且不写入事件数据库。"""
    database, _ = agent_store
    session = await database.sessions.create(generate_session_id(), "流")
    transport = SessionEventBus()
    store = AgentStore(transport=transport)
    queue, _ = await store.open_subscription(session.id)
    coalescer = StreamCoalescer(
        session_id=session.id,
        run_id="run-1",
        stream_id="answer-run-1",
        publish_realtime=store.publish_realtime,
        max_bytes=1,
    )

    await coalescer.append("你")
    await coalescer.append("好")
    events = [await queue.get(), await queue.get()]
    assert [(event.chunk_id, event.payload["delta"]) for event in events] == [
        (1, "你"),
        (2, "好"),
    ]
    assert await store.list_events_after(session.id) == []
