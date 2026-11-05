"""消息 async SQLAlchemy 仓储测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.models import Base
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    FileRefPart,
    InvalidMessageReferenceError,
    MessageConflictError,
    MessageMetadata,
    MessageStatus,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.session import SessionScope


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


async def _make_repository() -> tuple[
    Any,
    async_sessionmaker[AsyncSession],
    SqlAlchemyMessageRepository,
]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory, SqlAlchemyMessageRepository(session_factory)


async def _seed_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str = "session_1",
    owner_id: str = "user_1",
    deleted: bool = False,
) -> None:
    timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                SessionModel(
                    id=session_id,
                    owner_id=owner_id,
                    last_message_sequence=0,
                    created_at=timestamp,
                    updated_at=timestamp,
                    deleted_time=timestamp if deleted else None,
                )
            )


async def _session_sequence(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> int:
    async with session_factory() as session:
        value = await session.scalar(
            select(SessionModel.last_message_sequence).where(SessionModel.id == session_id)
        )
    assert value is not None
    return value


async def _message_count(
    session_factory: async_sessionmaker[AsyncSession],
    model: type[ConversationMessageModel] | type[ExecutionMessageModel],
    *,
    session_id: str,
) -> int:
    async with session_factory() as session:
        value = await session.scalar(
            select(func.count()).select_from(model).where(model.session_id == session_id)
        )
    return int(value or 0)


def _conversation(
    message_id: str,
    *,
    role: ConversationRole,
    text: str,
    run_id: str,
    client_message_id: str | None = None,
    session_id: str = "session_1",
    owner_id: str = "user_1",
    reply_to_message_id: str | None = None,
    created_at: datetime | None = None,
) -> ConversationMessageDraft:
    timestamp = created_at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return ConversationMessageDraft(
        id=message_id,
        session_id=session_id,
        owner_id=owner_id,
        role=role,
        status=MessageStatus.COMPLETED,
        parts=(TextPart(text),),
        reply_to_message_id=reply_to_message_id,
        run_id=run_id,
        client_message_id=client_message_id,
        created_at=timestamp,
        completed_at=timestamp,
        metadata=MessageMetadata(provider="test", model="fixture"),
    )


def _execution(
    message_id: str,
    *,
    role: ExecutionRole,
    parts: tuple[TextPart | ToolCallPart | ToolResultPart, ...],
    tool_call_id: str | None = None,
    created_at: datetime | None = None,
    session_id: str = "session_1",
    owner_id: str = "user_1",
    run_id: str = "run_1",
    thread_id: str = "root:run_1",
    source_message_id: str | None = None,
) -> ExecutionMessageDraft:
    return ExecutionMessageDraft(
        id=message_id,
        session_id=session_id,
        owner_id=owner_id,
        run_id=run_id,
        thread_id=thread_id,
        role=role,
        parts=parts,
        tool_call_id=tool_call_id,
        source_message_id=source_message_id,
        created_at=created_at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        metadata=MessageMetadata(),
    )


def test_repository_round_trip_and_shared_sequence() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            user = await repository.append_conversation(
                _conversation(
                    "msg_user_1",
                    role=ConversationRole.USER,
                    text="你好",
                    run_id="run_1",
                    client_message_id="client_1",
                )
            )
            execution = await repository.append_execution(
                _execution(
                    "msg_system_1",
                    role=ExecutionRole.SYSTEM,
                    parts=(TextPart("system prompt"),),
                )
            )
            assistant = await repository.append_conversation(
                _conversation(
                    "msg_assistant_1",
                    role=ConversationRole.ASSISTANT,
                    text="你好，有什么可以帮你？",
                    run_id="run_1",
                )
            )

            assert (user.sequence, execution.sequence, assistant.sequence) == (1, 2, 3)
            assert user.parts == (TextPart("你好"),)
            assert user.metadata == MessageMetadata(provider="test", model="fixture")

            page = await repository.list_conversation_for_owner(
                "session_1",
                "user_1",
                before_sequence=None,
                limit=20,
            )
            assert page is not None
            assert [item.role for item in page.items] == [
                ConversationRole.USER,
                ConversationRole.ASSISTANT,
            ]
            assert [item.sequence for item in page.items] == [1, 3]
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_message_write_does_not_regress_session_updated_at() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            latest = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
            stale = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)

            await repository.append_conversation(
                _conversation(
                    "msg_latest",
                    role=ConversationRole.USER,
                    text="较新消息",
                    run_id="run_latest",
                    client_message_id="client_latest",
                    created_at=latest,
                )
            )
            stale_message = await repository.append_execution(
                _execution(
                    "msg_stale",
                    role=ExecutionRole.SYSTEM,
                    parts=(TextPart("较早消息"),),
                    created_at=stale,
                )
            )

            async with session_factory() as session:
                row = await session.get(SessionModel, "session_1")
            assert row is not None
            assert row.last_message_sequence == 2
            assert row.updated_at.replace(tzinfo=UTC) == latest
            assert stale_message.created_at == stale
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_paginates_backward_and_filters_owner() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            for index in range(1, 6):
                await repository.append_conversation(
                    _conversation(
                        f"msg_{index}",
                        role=ConversationRole.USER,
                        text=f"消息 {index}",
                        run_id=f"run_{index}",
                        client_message_id=f"client_{index}",
                    )
                )

            latest = await repository.list_conversation_for_owner(
                "session_1",
                "user_1",
                before_sequence=None,
                limit=2,
            )
            assert latest is not None
            assert [item.sequence for item in latest.items] == [4, 5]
            assert latest.next_before_sequence == 4

            older = await repository.list_conversation_for_owner(
                "session_1",
                "user_1",
                before_sequence=4,
                limit=2,
            )
            assert older is not None
            assert [item.sequence for item in older.items] == [2, 3]
            assert older.next_before_sequence == 2

            assert (
                await repository.list_conversation_for_owner(
                    "session_1",
                    "another_user",
                    before_sequence=None,
                    limit=20,
                )
                is None
            )
            assert (
                await repository.list_conversation_for_owner(
                    "session_missing",
                    "user_1",
                    before_sequence=None,
                    limit=20,
                )
                is None
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_message_read_rechecks_session_visibility_after_prior_lookup() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            await repository.append_conversation(
                _conversation(
                    "msg_1",
                    role=ConversationRole.USER,
                    text="消息",
                    run_id="run_1",
                    client_message_id="client_1",
                )
            )
            session_repository = SqlAlchemySessionRepository(session_factory)
            scope = SessionScope(session_id="session_1", owner_id="user_1")
            assert await session_repository.get_visible(scope) is not None

            deleted_at = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(SessionModel)
                        .where(SessionModel.id == "session_1")
                        .values(
                            deleted_time=deleted_at,
                            updated_at=deleted_at,
                        )
                    )

            assert (
                await repository.list_conversation_for_owner(
                    "session_1",
                    "user_1",
                    before_sequence=None,
                    limit=20,
                )
                is None
            )
            assert (
                await _message_count(
                    session_factory,
                    ConversationMessageModel,
                    session_id="session_1",
                )
                == 1
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_visible_session_without_messages_returns_empty_page() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)

            page = await repository.list_conversation_for_owner(
                "session_1",
                "user_1",
                before_sequence=None,
                limit=20,
            )

            assert page is not None
            assert page.items == ()
            assert page.next_before_sequence is None
        finally:
            await engine.dispose()

    _run(scenario())


def test_message_write_is_scoped_to_draft_owner() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)

            with pytest.raises(SessionNotFoundError):
                await repository.append_conversation(
                    _conversation(
                        "msg_cross_user",
                        role=ConversationRole.USER,
                        text="越权消息",
                        run_id="run_1",
                        client_message_id="client_1",
                        owner_id="user_2",
                    )
                )
            assert await _session_sequence(session_factory, "session_1") == 0
            assert (
                await _message_count(
                    session_factory,
                    ConversationMessageModel,
                    session_id="session_1",
                )
                == 0
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_user_message_is_idempotent_and_conflicting_reuse_is_rejected() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            first = await repository.append_conversation(
                _conversation(
                    "msg_1",
                    role=ConversationRole.USER,
                    text="原始消息",
                    run_id="run_1",
                    client_message_id="client_1",
                )
            )
            replay = await repository.append_conversation(
                _conversation(
                    "msg_2",
                    role=ConversationRole.USER,
                    text="原始消息",
                    run_id="run_1",
                    client_message_id="client_1",
                )
            )

            assert replay == first
            with pytest.raises(MessageConflictError):
                await repository.append_conversation(
                    _conversation(
                        "msg_3",
                        role=ConversationRole.USER,
                        text="被篡改的消息",
                        run_id="run_1",
                        client_message_id="client_1",
                    )
                )
            assert await _session_sequence(session_factory, "session_1") == 1
            assert (
                await _message_count(
                    session_factory,
                    ConversationMessageModel,
                    session_id="session_1",
                )
                == 1
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_assistant_message_is_unique_per_run() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            first = await repository.append_conversation(
                _conversation(
                    "msg_assistant_1",
                    role=ConversationRole.ASSISTANT,
                    text="最终回答",
                    run_id="run_1",
                )
            )
            replay = await repository.append_conversation(
                _conversation(
                    "msg_assistant_2",
                    role=ConversationRole.ASSISTANT,
                    text="最终回答",
                    run_id="run_1",
                )
            )
            assert replay == first

            with pytest.raises(MessageConflictError):
                await repository.append_conversation(
                    _conversation(
                        "msg_assistant_3",
                        role=ConversationRole.ASSISTANT,
                        text="另一个回答",
                        run_id="run_1",
                    )
                )
            assert await _session_sequence(session_factory, "session_1") == 1
            assert (
                await _message_count(
                    session_factory,
                    ConversationMessageModel,
                    session_id="session_1",
                )
                == 1
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_tool_result_is_idempotent_by_session_run_thread_and_call_id() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            assistant = await repository.append_execution(
                _execution(
                    "msg_assistant_tool",
                    role=ExecutionRole.ASSISTANT,
                    parts=(
                        ToolCallPart(
                            tool_call_id="call_1",
                            name="search",
                            arguments={"query": "Athena"},
                        ),
                    ),
                )
            )
            first_result = ToolResultPart(
                tool_call_id="call_1",
                status=ToolResultStatus.SUCCESS,
                output="结果",
            )
            tool = await repository.append_execution(
                _execution(
                    "msg_tool_1",
                    role=ExecutionRole.TOOL,
                    parts=(first_result,),
                    tool_call_id="call_1",
                    created_at=assistant.created_at + timedelta(seconds=1),
                )
            )
            replay = await repository.append_execution(
                _execution(
                    "msg_tool_2",
                    role=ExecutionRole.TOOL,
                    parts=(first_result,),
                    tool_call_id="call_1",
                    created_at=assistant.created_at + timedelta(seconds=2),
                )
            )
            assert replay == tool

            with pytest.raises(MessageConflictError):
                await repository.append_execution(
                    _execution(
                        "msg_tool_3",
                        role=ExecutionRole.TOOL,
                        parts=(
                            ToolResultPart(
                                tool_call_id="call_1",
                                status=ToolResultStatus.ERROR,
                                output="失败",
                            ),
                        ),
                        tool_call_id="call_1",
                    )
                )
            assert await _session_sequence(session_factory, "session_1") == 2
            assert (
                await _message_count(
                    session_factory,
                    ExecutionMessageModel,
                    session_id="session_1",
                )
                == 2
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_tool_call_scope_allows_same_thread_across_runs_and_sessions() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            await _seed_session(
                session_factory,
                session_id="session_2",
                owner_id="user_2",
            )
            for session_id, run_id in (
                ("session_1", "run_1"),
                ("session_1", "run_2"),
                ("session_2", "run_2"),
            ):
                owner_id = "user_1" if session_id == "session_1" else "user_2"
                await repository.append_execution(
                    _execution(
                        f"msg_call_{session_id}_{run_id}",
                        role=ExecutionRole.ASSISTANT,
                        parts=(
                            ToolCallPart(
                                tool_call_id="call_1",
                                name="search",
                                arguments={},
                            ),
                        ),
                        session_id=session_id,
                        owner_id=owner_id,
                        run_id=run_id,
                        thread_id="shared_thread",
                    )
                )
                await repository.append_execution(
                    _execution(
                        f"msg_result_{session_id}_{run_id}",
                        role=ExecutionRole.TOOL,
                        parts=(
                            ToolResultPart(
                                tool_call_id="call_1",
                                status=ToolResultStatus.SUCCESS,
                                output=f"{session_id}:{run_id}",
                            ),
                        ),
                        tool_call_id="call_1",
                        session_id=session_id,
                        owner_id=owner_id,
                        run_id=run_id,
                        thread_id="shared_thread",
                    )
                )

            assert await _session_sequence(session_factory, "session_1") == 4
            assert await _session_sequence(session_factory, "session_2") == 2
            assert (
                await _message_count(
                    session_factory,
                    ExecutionMessageModel,
                    session_id="session_1",
                )
                == 4
            )
            assert (
                await _message_count(
                    session_factory,
                    ExecutionMessageModel,
                    session_id="session_2",
                )
                == 2
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_file_parts_round_trip_and_deleted_session_is_hidden() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory, session_id="session_deleted", deleted=True)
            assert (
                await repository.list_conversation_for_owner(
                    "session_deleted",
                    "user_1",
                    before_sequence=None,
                    limit=20,
                )
                is None
            )
            with pytest.raises(SessionNotFoundError):
                await repository.append_execution(
                    ExecutionMessageDraft(
                        id="msg_deleted",
                        session_id="session_deleted",
                        owner_id="user_1",
                        run_id="run_1",
                        thread_id="root:run_1",
                        role=ExecutionRole.USER,
                        parts=(TextPart("不能写入"),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                        metadata=MessageMetadata(),
                    )
                )

            await _seed_session(session_factory)
            user = await repository.append_conversation(
                ConversationMessageDraft(
                    id="msg_file",
                    session_id="session_1",
                    owner_id="user_1",
                    role=ConversationRole.USER,
                    status=MessageStatus.COMPLETED,
                    parts=(
                        TextPart("请分析附件"),
                        FileRefPart(
                            file_id="file_1",
                            filename="report.pdf",
                            mime_type="application/pdf",
                            size_bytes=1024,
                        ),
                    ),
                    reply_to_message_id=None,
                    run_id="run_1",
                    client_message_id="client_file",
                    created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                    completed_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                    metadata=MessageMetadata(),
                )
            )
            assert isinstance(user.parts[1], FileRefPart)
            assert user.parts[1].filename == "report.pdf"
        finally:
            await engine.dispose()

    _run(scenario())


def test_tool_result_requires_matching_call_in_same_run_and_thread() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            result_part = ToolResultPart(
                tool_call_id="call_1",
                status=ToolResultStatus.SUCCESS,
                output="结果",
            )

            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
                    _execution(
                        "msg_tool_missing",
                        role=ExecutionRole.TOOL,
                        parts=(result_part,),
                        tool_call_id="call_1",
                    )
                )
            assert await _session_sequence(session_factory, "session_1") == 0
            assert (
                await _message_count(
                    session_factory,
                    ExecutionMessageModel,
                    session_id="session_1",
                )
                == 0
            )

            await repository.append_execution(
                _execution(
                    "msg_call",
                    role=ExecutionRole.ASSISTANT,
                    parts=(
                        ToolCallPart(
                            tool_call_id="call_1",
                            name="search",
                            arguments={"query": "Athena"},
                        ),
                    ),
                )
            )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
                    _execution(
                        "msg_wrong_thread",
                        role=ExecutionRole.TOOL,
                        parts=(result_part,),
                        tool_call_id="call_1",
                        thread_id="root:run_2",
                    )
                )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
                    _execution(
                        "msg_wrong_run",
                        role=ExecutionRole.TOOL,
                        parts=(result_part,),
                        tool_call_id="call_1",
                        run_id="run_2",
                    )
                )
            assert await _session_sequence(session_factory, "session_1") == 1
            assert (
                await _message_count(
                    session_factory,
                    ExecutionMessageModel,
                    session_id="session_1",
                )
                == 1
            )

            tool = await repository.append_execution(
                _execution(
                    "msg_tool",
                    role=ExecutionRole.TOOL,
                    parts=(result_part,),
                    tool_call_id="call_1",
                )
            )
            assert tool.sequence == 2
        finally:
            await engine.dispose()

    _run(scenario())


def test_duplicate_assistant_tool_call_id_is_rejected_across_messages() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            await repository.append_execution(
                _execution(
                    "msg_call_1",
                    role=ExecutionRole.ASSISTANT,
                    parts=(
                        ToolCallPart(
                            tool_call_id="call_1",
                            name="search",
                            arguments={},
                        ),
                    ),
                )
            )

            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
                    _execution(
                        "msg_call_2",
                        role=ExecutionRole.ASSISTANT,
                        parts=(
                            ToolCallPart(
                                tool_call_id="call_1",
                                name="fetch",
                                arguments={},
                            ),
                        ),
                    )
                )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
                    _execution(
                        "msg_call_duplicate_parts",
                        role=ExecutionRole.ASSISTANT,
                        parts=(
                            ToolCallPart("call_2", "search", {}),
                            ToolCallPart("call_2", "fetch", {}),
                        ),
                    )
                )

            assert await _session_sequence(session_factory, "session_1") == 1
            assert (
                await _message_count(
                    session_factory,
                    ExecutionMessageModel,
                    session_id="session_1",
                )
                == 1
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_assistant_reply_to_requires_same_session_user_message() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            await _seed_session(
                session_factory,
                session_id="session_2",
                owner_id="user_2",
            )
            user = await repository.append_conversation(
                _conversation(
                    "msg_user",
                    role=ConversationRole.USER,
                    text="问题",
                    run_id="run_1",
                    client_message_id="client_1",
                )
            )
            other_user = await repository.append_conversation(
                _conversation(
                    "msg_other_user",
                    role=ConversationRole.USER,
                    text="其他会话",
                    run_id="run_other",
                    client_message_id="client_other",
                    session_id="session_2",
                    owner_id="user_2",
                )
            )

            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_conversation(
                    _conversation(
                        "msg_missing_reply",
                        role=ConversationRole.ASSISTANT,
                        text="回答",
                        run_id="run_2",
                        reply_to_message_id="msg_missing",
                    )
                )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_conversation(
                    _conversation(
                        "msg_cross_session_reply",
                        role=ConversationRole.ASSISTANT,
                        text="回答",
                        run_id="run_3",
                        reply_to_message_id=other_user.id,
                    )
                )
            assert await _session_sequence(session_factory, "session_1") == 1
            assert (
                await _message_count(
                    session_factory,
                    ConversationMessageModel,
                    session_id="session_1",
                )
                == 1
            )

            assistant = await repository.append_conversation(
                _conversation(
                    "msg_assistant",
                    role=ConversationRole.ASSISTANT,
                    text="回答",
                    run_id="run_2",
                    reply_to_message_id=user.id,
                )
            )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_conversation(
                    _conversation(
                        "msg_reply_to_assistant",
                        role=ConversationRole.ASSISTANT,
                        text="再次回答",
                        run_id="run_3",
                        reply_to_message_id=assistant.id,
                    )
                )
            assert await _session_sequence(session_factory, "session_1") == 2
        finally:
            await engine.dispose()

    _run(scenario())


def test_execution_source_requires_same_session_conversation_message() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            await _seed_session(
                session_factory,
                session_id="session_2",
                owner_id="user_2",
            )
            user = await repository.append_conversation(
                _conversation(
                    "msg_user",
                    role=ConversationRole.USER,
                    text="问题",
                    run_id="run_1",
                    client_message_id="client_1",
                )
            )
            other = await repository.append_conversation(
                _conversation(
                    "msg_other",
                    role=ConversationRole.USER,
                    text="其他会话",
                    run_id="run_other",
                    client_message_id="client_other",
                    session_id="session_2",
                    owner_id="user_2",
                )
            )
            execution = await repository.append_execution(
                _execution(
                    "msg_system",
                    role=ExecutionRole.SYSTEM,
                    parts=(TextPart("system"),),
                    source_message_id=user.id,
                )
            )

            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
                    _execution(
                        "msg_execution_source",
                        role=ExecutionRole.SYSTEM,
                        parts=(TextPart("错误来源"),),
                        source_message_id=execution.id,
                    )
                )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
                    _execution(
                        "msg_cross_session_source",
                        role=ExecutionRole.SYSTEM,
                        parts=(TextPart("错误来源"),),
                        source_message_id=other.id,
                    )
                )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
                    _execution(
                        "msg_missing_source",
                        role=ExecutionRole.SYSTEM,
                        parts=(TextPart("错误来源"),),
                        source_message_id="msg_missing",
                    )
                )
            assert await _session_sequence(session_factory, "session_1") == 2
            assert (
                await _message_count(
                    session_factory,
                    ExecutionMessageModel,
                    session_id="session_1",
                )
                == 1
            )

            final_assistant = await repository.append_conversation(
                _conversation(
                    "msg_assistant",
                    role=ConversationRole.ASSISTANT,
                    text="最终回答",
                    run_id="run_1",
                    reply_to_message_id=user.id,
                )
            )
            sourced = await repository.append_execution(
                _execution(
                    "msg_from_assistant",
                    role=ExecutionRole.SYSTEM,
                    parts=(TextPart("assistant source"),),
                    source_message_id=final_assistant.id,
                )
            )
            assert sourced.source_message_id == final_assistant.id
        finally:
            await engine.dispose()

    _run(scenario())
