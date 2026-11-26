"""消息 async SQLAlchemy 仓储测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pg_unit_harness import create_pg_unit_db
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.model import UserModel
from adapter.llm.model import ModelProfileVersionModel
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from adapter.system_prompt.model import SystemPromptVersionModel
from domain.message import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessage,
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

_MODEL_PROFILE_ID = "profile_msg_test"
_MAIN_PROMPT_ID = "sp_main_msg_test"
_COMP_PROMPT_ID = "sp_comp_msg_test"
_MODEL_SNAPSHOT: dict[str, object] = {
    "id": _MODEL_PROFILE_ID,
    "name": "测试模型",
    "protocol": "chat_completions",
    "base_url": "https://api.example.com/v1",
    "model": "gpt-4o-mini",
    "context_window_tokens": 128000,
    "tokenizer_encoding": "cl100k_base",
    "supports_stream": True,
    "supports_tools": True,
    "supports_parallel_tool_calls": False,
    "max_output_tokens": 2048,
    "request_timeout_seconds": 30,
    "execution_timeout_seconds": 120,
    "max_concurrency": 4,
    "temperature": None,
    "scenario_label": None,
    "supports_vision": False,
    "version": 1,
}


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


async def _make_repository() -> tuple[
    Any,
    async_sessionmaker[AsyncSession],
    SqlAlchemyMessageRepository,
]:
    engine, session_factory = await create_pg_unit_db()
    return engine, session_factory, SqlAlchemyMessageRepository(session_factory)


async def _ensure_run_dependencies(session: AsyncSession, timestamp: datetime) -> None:
    """补齐 Run 外键依赖的模型配置与系统提示词版本记录。"""
    if await session.get(ModelProfileVersionModel, _MODEL_PROFILE_ID) is None:
        session.add(
            ModelProfileVersionModel(
                id=_MODEL_PROFILE_ID,
                name="测试模型",
                protocol="chat_completions",
                base_url="https://api.example.com/v1",
                model="gpt-4o-mini",
                api_key_ciphertext=b"ciphertext",
                api_key_nonce=b"0123456789ab",
                api_key_key_version=1,
                context_window_tokens=128000,
                tokenizer_encoding="cl100k_base",
                supports_stream=True,
                supports_tools=True,
                supports_parallel_tool_calls=False,
                max_output_tokens=2048,
                request_timeout_seconds=30,
                execution_timeout_seconds=120,
                max_concurrency=4,
                temperature=None,
                enabled=True,
                visible_to_users=True,
                supports_vision=False,
                version=1,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
    if await session.get(SystemPromptVersionModel, _MAIN_PROMPT_ID) is None:
        session.add(
            SystemPromptVersionModel(
                id=_MAIN_PROMPT_ID,
                kind="main",
                content="main prompt",
                created_by=None,
                created_at=timestamp,
                restored_from_version_id=None,
            )
        )
    if await session.get(SystemPromptVersionModel, _COMP_PROMPT_ID) is None:
        session.add(
            SystemPromptVersionModel(
                id=_COMP_PROMPT_ID,
                kind="compression",
                content="compression prompt",
                created_by=None,
                created_at=timestamp,
                restored_from_version_id=None,
            )
        )
    await session.flush()


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
            await _ensure_run_dependencies(session, timestamp)
            if await session.get(UserModel, owner_id) is None:
                session.add(
                    UserModel(
                        id=owner_id,
                        email=f"{owner_id}@example.com",
                        password_hash="hash:test",
                        display_name=None,
                        status="active",
                        created_at=timestamp,
                        updated_at=timestamp,
                        deleted_time=None,
                    )
                )
                await session.flush()
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


async def _lock_visible_session(
    session: AsyncSession,
    session_id: str,
    owner_id: str,
) -> SessionModel:
    row = await session.scalar(
        select(SessionModel)
        .where(
            SessionModel.id == session_id,
            SessionModel.owner_id == owner_id,
            SessionModel.deleted_time.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise SessionNotFoundError("session_not_found")
    return row


def _build_run_row(
    *,
    run_id: str,
    session_id: str,
    owner_id: str,
    trigger_message_id: str,
    context_cutoff_sequence: int,
    timestamp: datetime,
    depth: int = 0,
) -> RunModel:
    return RunModel(
        id=run_id,
        session_id=session_id,
        owner_id=owner_id,
        status="completed",
        trigger_message_id=trigger_message_id,
        context_cutoff_sequence=context_cutoff_sequence,
        model_config_version_id=_MODEL_PROFILE_ID,
        model_snapshot_json=dict(_MODEL_SNAPSHOT),
        main_prompt_version_id=_MAIN_PROMPT_ID,
        compression_prompt_version_id=_COMP_PROMPT_ID,
        prompt_template_version="run-v2",
        event_sequence=0,
        execution_epoch=0,
        queued_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
        finished_at=timestamp,
        usage={},
        depth=depth,
    )


async def _append_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    repository: SqlAlchemyMessageRepository,
    draft: ConversationMessageDraft,
    *,
    run_depth: int = 0,
    create_run_if_missing: bool = True,
) -> ConversationMessage:
    """在单事务内锁定会话、写入会话消息并原子补齐所属 Run（满足延迟复合外键）。"""
    async with session_factory() as session:
        async with session.begin():
            locked = await _lock_visible_session(session, draft.session_id, draft.owner_id)
            message = await repository.append_conversation_in_session(
                session,
                draft,
                locked_session=locked,
            )
            if create_run_if_missing and await session.get(RunModel, draft.run_id) is None:
                session.add(
                    _build_run_row(
                        run_id=draft.run_id,
                        session_id=draft.session_id,
                        owner_id=draft.owner_id,
                        trigger_message_id=message.id,
                        context_cutoff_sequence=message.sequence,
                        timestamp=draft.created_at,
                        depth=run_depth,
                    )
                )
                await session.flush()
            return message


async def _append_execution(
    session_factory: async_sessionmaker[AsyncSession],
    repository: SqlAlchemyMessageRepository,
    draft: ExecutionMessageDraft,
) -> ExecutionMessage:
    """在单事务内锁定会话并写入执行消息。"""
    async with session_factory() as session:
        async with session.begin():
            locked = await _lock_visible_session(session, draft.session_id, draft.owner_id)
            return await repository.append_execution_in_session(
                session,
                draft,
                locked_session=locked,
            )


async def _seed_run(
    session_factory: async_sessionmaker[AsyncSession],
    repository: SqlAlchemyMessageRepository,
    *,
    run_id: str = "run_1",
    session_id: str = "session_1",
    owner_id: str = "user_1",
    depth: int = 0,
    trigger_message_id: str | None = None,
) -> ConversationMessage:
    """预置一条合法触发消息及所属 Run，供执行消息测试直接引用。"""
    msg_id = trigger_message_id or f"msg_trigger_{session_id}_{run_id}"
    return await _append_conversation(
        session_factory,
        repository,
        _conversation(
            msg_id,
            role=ConversationRole.USER,
            text=f"触发 {run_id}",
            run_id=run_id,
            context_turn_id=msg_id,
            session_id=session_id,
            owner_id=owner_id,
        ),
        run_depth=depth,
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
    context_turn_id: str | None = None,
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
        context_turn_id=context_turn_id or reply_to_message_id or message_id,
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


def test_repository_round_trip_shared_sequence_and_main_run_isolation() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            user = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_user_1",
                    role=ConversationRole.USER,
                    text="你好",
                    run_id="run_1",
                    client_message_id="client_1",
                ),
            )
            execution = await _append_execution(
                session_factory,
                repository,
                _execution(
                    "msg_system_1",
                    role=ExecutionRole.SYSTEM,
                    parts=(TextPart("system prompt"),),
                ),
            )
            # 子 Run（depth=1）的对话消息不得泄露到主会话公开历史列表中。
            subagent_msg = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_sub_1",
                    role=ConversationRole.USER,
                    text="子任务内部消息",
                    run_id="run_sub_1",
                ),
                run_depth=1,
            )
            assistant = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_assistant_1",
                    role=ConversationRole.ASSISTANT,
                    text="你好，有什么可以帮你？",
                    run_id="run_1",
                    context_turn_id=user.id,
                ),
            )

            sequences = (
                user.sequence,
                execution.sequence,
                subagent_msg.sequence,
                assistant.sequence,
            )
            assert sequences == (1, 2, 3, 4)
            assert user.parts == (TextPart("你好"),)
            assert user.context_turn_id == "msg_user_1"
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
            assert [item.sequence for item in page.items] == [1, 4]
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_enforces_run_session_foreign_key_on_messages() -> None:
    """conversation_messages 与 execution_messages 的 (run_id, session_id) 复合外键必须生效。"""

    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            with pytest.raises(IntegrityError):
                await _append_conversation(
                    session_factory,
                    repository,
                    _conversation(
                        "msg_orphan_run",
                        role=ConversationRole.USER,
                        text="孤儿 Run 消息",
                        run_id="run_nonexistent",
                    ),
                    create_run_if_missing=False,
                )
            with pytest.raises(IntegrityError):
                await _append_execution(
                    session_factory,
                    repository,
                    _execution(
                        "msg_exec_orphan_run",
                        role=ExecutionRole.SYSTEM,
                        parts=(TextPart("孤儿执行消息"),),
                        run_id="run_nonexistent",
                    ),
                )
            assert await _session_sequence(session_factory, "session_1") == 0
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

            await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_latest",
                    role=ConversationRole.USER,
                    text="较新消息",
                    run_id="run_latest",
                    client_message_id="client_latest",
                    created_at=latest,
                ),
            )
            stale_message = await _append_execution(
                session_factory,
                repository,
                _execution(
                    "msg_stale",
                    role=ExecutionRole.SYSTEM,
                    parts=(TextPart("较早消息"),),
                    run_id="run_latest",
                    thread_id="root:run_latest",
                    created_at=stale,
                ),
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
                await _append_conversation(
                    session_factory,
                    repository,
                    _conversation(
                        f"msg_{index}",
                        role=ConversationRole.USER,
                        text=f"消息 {index}",
                        run_id=f"run_{index}",
                        client_message_id=f"client_{index}",
                    ),
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
            await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_1",
                    role=ConversationRole.USER,
                    text="消息",
                    run_id="run_1",
                    client_message_id="client_1",
                ),
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

            # 即使调用方传入了已锁定的 session_1，草稿 owner_id 不一致仍必须拒绝写入。
            with pytest.raises(SessionNotFoundError):
                async with session_factory() as session:
                    async with session.begin():
                        locked = await _lock_visible_session(session, "session_1", "user_1")
                        await repository.append_conversation_in_session(
                            session,
                            _conversation(
                                "msg_cross_user",
                                role=ConversationRole.USER,
                                text="越权消息",
                                run_id="run_1",
                                client_message_id="client_1",
                                owner_id="user_2",
                            ),
                            locked_session=locked,
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
            first = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_1",
                    role=ConversationRole.USER,
                    text="原始消息",
                    run_id="run_1",
                    client_message_id="client_1",
                ),
            )
            replay = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_2",
                    role=ConversationRole.USER,
                    text="原始消息",
                    run_id="run_1",
                    client_message_id="client_1",
                ),
            )

            assert replay == first
            with pytest.raises(MessageConflictError):
                await _append_conversation(
                    session_factory,
                    repository,
                    _conversation(
                        "msg_3",
                        role=ConversationRole.USER,
                        text="被篡改的消息",
                        run_id="run_1",
                        client_message_id="client_1",
                    ),
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


def test_same_role_messages_can_share_run_and_client_idempotency_remains() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            first = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_assistant_1",
                    role=ConversationRole.ASSISTANT,
                    text="最终回答",
                    run_id="run_1",
                ),
            )
            second = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_assistant_2",
                    role=ConversationRole.ASSISTANT,
                    text="补充回答",
                    run_id="run_1",
                ),
            )
            assert first.sequence == 1
            assert second.sequence == 2
            assert second.id != first.id

            keyed = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_assistant_keyed",
                    role=ConversationRole.ASSISTANT,
                    text="幂等消息",
                    run_id="run_1",
                    client_message_id="client-key",
                ),
            )
            replay = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_assistant_keyed_retry",
                    role=ConversationRole.ASSISTANT,
                    text="幂等消息",
                    run_id="run_1",
                    client_message_id="client-key",
                ),
            )
            assert replay == keyed

            with pytest.raises(MessageConflictError):
                await _append_conversation(
                    session_factory,
                    repository,
                    _conversation(
                        "msg_assistant_3",
                        role=ConversationRole.ASSISTANT,
                        text="冲突内容",
                        run_id="run_1",
                        client_message_id="client-key",
                    ),
                )
            assert await _session_sequence(session_factory, "session_1") == 3
            assert (
                await _message_count(
                    session_factory,
                    ConversationMessageModel,
                    session_id="session_1",
                )
                == 3
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_tool_result_is_idempotent_by_session_run_thread_and_call_id() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            await _seed_run(session_factory, repository, run_id="run_1")
            assistant = await _append_execution(
                session_factory,
                repository,
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
                ),
            )
            first_result = ToolResultPart(
                tool_call_id="call_1",
                status=ToolResultStatus.SUCCESS,
                output="结果",
            )
            tool = await _append_execution(
                session_factory,
                repository,
                _execution(
                    "msg_tool_1",
                    role=ExecutionRole.TOOL,
                    parts=(first_result,),
                    tool_call_id="call_1",
                    created_at=assistant.created_at + timedelta(seconds=1),
                ),
            )
            replay = await _append_execution(
                session_factory,
                repository,
                _execution(
                    "msg_tool_2",
                    role=ExecutionRole.TOOL,
                    parts=(first_result,),
                    tool_call_id="call_1",
                    created_at=assistant.created_at + timedelta(seconds=2),
                ),
            )
            assert replay == tool

            with pytest.raises(MessageConflictError):
                await _append_execution(
                    session_factory,
                    repository,
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
                    ),
                )
            # 1 条预置触发消息 + 2 条执行消息（call + result）。
            assert await _session_sequence(session_factory, "session_1") == 3
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
                ("session_2", "run_3"),
            ):
                owner_id = "user_1" if session_id == "session_1" else "user_2"
                await _seed_run(
                    session_factory,
                    repository,
                    run_id=run_id,
                    session_id=session_id,
                    owner_id=owner_id,
                )
                await _append_execution(
                    session_factory,
                    repository,
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
                    ),
                )
                await _append_execution(
                    session_factory,
                    repository,
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
                    ),
                )

            # session_1: 2 次触发消息 + 4 条执行消息；session_2: 1 次触发消息 + 2 条执行消息。
            assert await _session_sequence(session_factory, "session_1") == 6
            assert await _session_sequence(session_factory, "session_2") == 3
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
                await _append_execution(
                    session_factory,
                    repository,
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
                    ),
                )

            await _seed_session(session_factory)
            user = await _append_conversation(
                session_factory,
                repository,
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
                    context_turn_id="msg_file",
                    client_message_id="client_file",
                    created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                    completed_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                    metadata=MessageMetadata(),
                ),
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
            await _seed_run(session_factory, repository, run_id="run_1")
            await _seed_run(session_factory, repository, run_id="run_2")
            result_part = ToolResultPart(
                tool_call_id="call_1",
                status=ToolResultStatus.SUCCESS,
                output="结果",
            )

            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    _execution(
                        "msg_tool_missing",
                        role=ExecutionRole.TOOL,
                        parts=(result_part,),
                        tool_call_id="call_1",
                    ),
                )
            assert await _session_sequence(session_factory, "session_1") == 2
            assert (
                await _message_count(
                    session_factory,
                    ExecutionMessageModel,
                    session_id="session_1",
                )
                == 0
            )

            await _append_execution(
                session_factory,
                repository,
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
                ),
            )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    _execution(
                        "msg_wrong_thread",
                        role=ExecutionRole.TOOL,
                        parts=(result_part,),
                        tool_call_id="call_1",
                        thread_id="root:run_2",
                    ),
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    _execution(
                        "msg_wrong_run",
                        role=ExecutionRole.TOOL,
                        parts=(result_part,),
                        tool_call_id="call_1",
                        run_id="run_2",
                    ),
                )
            assert await _session_sequence(session_factory, "session_1") == 3
            assert (
                await _message_count(
                    session_factory,
                    ExecutionMessageModel,
                    session_id="session_1",
                )
                == 1
            )

            tool = await _append_execution(
                session_factory,
                repository,
                _execution(
                    "msg_tool",
                    role=ExecutionRole.TOOL,
                    parts=(result_part,),
                    tool_call_id="call_1",
                ),
            )
            assert tool.sequence == 4
        finally:
            await engine.dispose()

    _run(scenario())


def test_duplicate_assistant_tool_call_id_is_rejected_across_messages() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            await _seed_session(session_factory)
            await _seed_run(session_factory, repository, run_id="run_1")
            await _append_execution(
                session_factory,
                repository,
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
                ),
            )

            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
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
                    ),
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    _execution(
                        "msg_call_duplicate_parts",
                        role=ExecutionRole.ASSISTANT,
                        parts=(
                            ToolCallPart("call_2", "search", {}),
                            ToolCallPart("call_2", "fetch", {}),
                        ),
                    ),
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
            user = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_user",
                    role=ConversationRole.USER,
                    text="问题",
                    run_id="run_1",
                    client_message_id="client_1",
                ),
            )
            other_user = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_other_user",
                    role=ConversationRole.USER,
                    text="其他会话",
                    run_id="run_other",
                    client_message_id="client_other",
                    session_id="session_2",
                    owner_id="user_2",
                ),
            )

            with pytest.raises(InvalidMessageReferenceError):
                await _append_conversation(
                    session_factory,
                    repository,
                    _conversation(
                        "msg_missing_reply",
                        role=ConversationRole.ASSISTANT,
                        text="回答",
                        run_id="run_2",
                        reply_to_message_id="msg_missing",
                    ),
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_conversation(
                    session_factory,
                    repository,
                    _conversation(
                        "msg_cross_session_reply",
                        role=ConversationRole.ASSISTANT,
                        text="回答",
                        run_id="run_3",
                        reply_to_message_id=other_user.id,
                    ),
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

            assistant = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_assistant",
                    role=ConversationRole.ASSISTANT,
                    text="回答",
                    run_id="run_1",
                    reply_to_message_id=user.id,
                ),
            )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_conversation(
                    session_factory,
                    repository,
                    _conversation(
                        "msg_reply_to_assistant",
                        role=ConversationRole.ASSISTANT,
                        text="再次回答",
                        run_id="run_1",
                        reply_to_message_id=assistant.id,
                    ),
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
            user = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_user",
                    role=ConversationRole.USER,
                    text="问题",
                    run_id="run_1",
                    client_message_id="client_1",
                ),
            )
            other = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_other",
                    role=ConversationRole.USER,
                    text="其他会话",
                    run_id="run_other",
                    client_message_id="client_other",
                    session_id="session_2",
                    owner_id="user_2",
                ),
            )
            execution = await _append_execution(
                session_factory,
                repository,
                _execution(
                    "msg_system",
                    role=ExecutionRole.SYSTEM,
                    parts=(TextPart("system"),),
                    source_message_id=user.id,
                ),
            )

            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    _execution(
                        "msg_execution_source",
                        role=ExecutionRole.SYSTEM,
                        parts=(TextPart("错误来源"),),
                        source_message_id=execution.id,
                    ),
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    _execution(
                        "msg_cross_session_source",
                        role=ExecutionRole.SYSTEM,
                        parts=(TextPart("错误来源"),),
                        source_message_id=other.id,
                    ),
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    _execution(
                        "msg_missing_source",
                        role=ExecutionRole.SYSTEM,
                        parts=(TextPart("错误来源"),),
                        source_message_id="msg_missing",
                    ),
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

            final_assistant = await _append_conversation(
                session_factory,
                repository,
                _conversation(
                    "msg_assistant",
                    role=ConversationRole.ASSISTANT,
                    text="最终回答",
                    run_id="run_1",
                    reply_to_message_id=user.id,
                ),
            )
            sourced = await _append_execution(
                session_factory,
                repository,
                _execution(
                    "msg_from_assistant",
                    role=ExecutionRole.SYSTEM,
                    parts=(TextPart("assistant source"),),
                    source_message_id=final_assistant.id,
                ),
            )
            assert sourced.source_message_id == final_assistant.id
        finally:
            await engine.dispose()

    _run(scenario())
