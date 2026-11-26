"""消息 PostgreSQL 迁移与仓储集成测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.model import UserModel
from adapter.llm.model import ModelProfileVersionModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from domain.message import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessage,
    ExecutionMessageDraft,
    ExecutionRole,
    InvalidMessageReferenceError,
    MessageMetadata,
    MessageStatus,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.system_prompt.defaults import INITIAL_SYSTEM_PROMPT_IDS
from domain.system_prompt.model import SystemPromptKind

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="ATHENA_TEST_DATABASE_URL is not configured",
    ),
]

_MODEL_PROFILE_ID = "profile_msg_pg"
_MODEL_SNAPSHOT: dict[str, object] = {
    "id": _MODEL_PROFILE_ID,
    "name": "集成测试模型",
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


async def _append_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    repository: SqlAlchemyMessageRepository,
    draft: ConversationMessageDraft,
    *,
    run_depth: int = 0,
    create_run_if_missing: bool = True,
) -> ConversationMessage:
    """在单事务内锁定会话、写入会话消息并补齐 Run（验证延迟复合外键）。"""
    async with session_factory() as session:
        async with session.begin():
            locked = await session.scalar(
                select(SessionModel)
                .where(
                    SessionModel.id == draft.session_id,
                    SessionModel.owner_id == draft.owner_id,
                    SessionModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if locked is None:
                raise SessionNotFoundError("session_not_found")
            message = await repository.append_conversation_in_session(
                session,
                draft,
                locked_session=locked,
            )
            if create_run_if_missing and await session.get(RunModel, draft.run_id) is None:
                session.add(
                    RunModel(
                        id=draft.run_id,
                        session_id=draft.session_id,
                        owner_id=draft.owner_id,
                        status="completed",
                        trigger_message_id=message.id,
                        context_cutoff_sequence=message.sequence,
                        model_config_version_id=_MODEL_PROFILE_ID,
                        model_snapshot_json=dict(_MODEL_SNAPSHOT),
                        main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS[SystemPromptKind.MAIN],
                        compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS[
                            SystemPromptKind.COMPRESSION
                        ],
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=0,
                        queued_at=draft.created_at,
                        created_at=draft.created_at,
                        updated_at=draft.created_at,
                        finished_at=draft.created_at,
                        usage={},
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
            locked = await session.scalar(
                select(SessionModel)
                .where(
                    SessionModel.id == draft.session_id,
                    SessionModel.owner_id == draft.owner_id,
                    SessionModel.deleted_time.is_(None),
                )
                .with_for_update()
            )
            if locked is None:
                raise SessionNotFoundError("session_not_found")
            return await repository.append_execution_in_session(
                session,
                draft,
                locked_session=locked,
            )


def test_message_migration_and_repository_round_trip() -> None:
    """在临时 schema 中执行真实迁移并验证两条消息流、外键与主/子 Run 隔离。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        async with isolated_postgres_schema(database_url, prefix="athena_v2_message_test") as db:
            expected_head = db.head_revision()
            session_factory = db.session_factory
            async with db.engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == expected_head

            timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        ModelProfileVersionModel(
                            id=_MODEL_PROFILE_ID,
                            name="集成测试模型",
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
                    session.add(
                        UserModel(
                            id="user_1",
                            email="user_1@example.com",
                            password_hash="hash:test",
                            display_name=None,
                            status="active",
                            created_at=timestamp,
                            updated_at=timestamp,
                            deleted_time=None,
                        )
                    )
                    session.add(
                        UserModel(
                            id="user_2",
                            email="user_2@example.com",
                            password_hash="hash:test",
                            display_name=None,
                            status="active",
                            created_at=timestamp,
                            updated_at=timestamp,
                            deleted_time=None,
                        )
                    )
                    session.add(
                        SessionModel(
                            id="session_1",
                            owner_id="user_1",
                            last_message_sequence=0,
                            created_at=timestamp,
                            updated_at=timestamp,
                            deleted_time=None,
                        )
                    )
                    session.add(
                        SessionModel(
                            id="session_2",
                            owner_id="user_2",
                            last_message_sequence=0,
                            created_at=timestamp,
                            updated_at=timestamp,
                            deleted_time=None,
                        )
                    )

            repository = SqlAlchemyMessageRepository(session_factory)
            user = await _append_conversation(
                session_factory,
                repository,
                ConversationMessageDraft(
                    id="msg_user",
                    session_id="session_1",
                    owner_id="user_1",
                    role=ConversationRole.USER,
                    status=MessageStatus.COMPLETED,
                    parts=(TextPart("请调用搜索"),),
                    reply_to_message_id=None,
                    run_id="run_1",
                    context_turn_id="msg_user",
                    client_message_id="client_1",
                    created_at=timestamp,
                    completed_at=timestamp,
                    metadata=MessageMetadata(),
                ),
            )
            other_user = await _append_conversation(
                session_factory,
                repository,
                ConversationMessageDraft(
                    id="msg_other_user",
                    session_id="session_2",
                    owner_id="user_2",
                    role=ConversationRole.USER,
                    status=MessageStatus.COMPLETED,
                    parts=(TextPart("其他会话"),),
                    reply_to_message_id=None,
                    run_id="run_other",
                    context_turn_id="msg_other_user",
                    client_message_id="client_other",
                    created_at=timestamp,
                    completed_at=timestamp,
                    metadata=MessageMetadata(),
                ),
            )
            # 复合外键 fk_conversation_messages_run_session 必须拒绝跨会话引用 Run。
            with pytest.raises(IntegrityError):
                await _append_conversation(
                    session_factory,
                    repository,
                    ConversationMessageDraft(
                        id="msg_cross_session_run",
                        session_id="session_1",
                        owner_id="user_1",
                        role=ConversationRole.USER,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("跨会话 Run"),),
                        reply_to_message_id=None,
                        run_id="run_other",
                        context_turn_id="msg_cross_session_run",
                        client_message_id=None,
                        created_at=timestamp,
                        completed_at=timestamp,
                        metadata=MessageMetadata(),
                    ),
                    create_run_if_missing=False,
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    ExecutionMessageDraft(
                        id="msg_missing_call",
                        session_id="session_1",
                        owner_id="user_1",
                        run_id="run_1",
                        thread_id="root:run_1",
                        role=ExecutionRole.TOOL,
                        parts=(
                            ToolResultPart(
                                tool_call_id="missing_call",
                                status=ToolResultStatus.SUCCESS,
                                output="无调用来源",
                            ),
                        ),
                        tool_call_id="missing_call",
                        source_message_id=None,
                        created_at=timestamp,
                        metadata=MessageMetadata(),
                    ),
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    ExecutionMessageDraft(
                        id="msg_cross_session_source",
                        session_id="session_1",
                        owner_id="user_1",
                        run_id="run_1",
                        thread_id="root:run_1",
                        role=ExecutionRole.SYSTEM,
                        parts=(TextPart("跨会话来源"),),
                        tool_call_id=None,
                        source_message_id=other_user.id,
                        created_at=timestamp,
                        metadata=MessageMetadata(),
                    ),
                )
            execution = await _append_execution(
                session_factory,
                repository,
                ExecutionMessageDraft(
                    id="msg_tool_call",
                    session_id="session_1",
                    owner_id="user_1",
                    run_id="run_1",
                    thread_id="root:run_1",
                    role=ExecutionRole.ASSISTANT,
                    parts=(
                        ToolCallPart(
                            tool_call_id="call_1",
                            name="search",
                            arguments={"query": "Athena"},
                        ),
                    ),
                    tool_call_id=None,
                    source_message_id=user.id,
                    created_at=timestamp,
                    metadata=MessageMetadata(),
                ),
            )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    ExecutionMessageDraft(
                        id="msg_tool_wrong_thread",
                        session_id="session_1",
                        owner_id="user_1",
                        run_id="run_1",
                        thread_id="worker:other",
                        role=ExecutionRole.TOOL,
                        parts=(
                            ToolResultPart(
                                tool_call_id="call_1",
                                status=ToolResultStatus.SUCCESS,
                                output="错误线程结果",
                            ),
                        ),
                        tool_call_id="call_1",
                        source_message_id=user.id,
                        created_at=timestamp,
                        metadata=MessageMetadata(),
                    ),
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    ExecutionMessageDraft(
                        id="msg_tool_wrong_run",
                        session_id="session_1",
                        owner_id="user_1",
                        run_id="run_2",
                        thread_id="root:run_2",
                        role=ExecutionRole.TOOL,
                        parts=(
                            ToolResultPart(
                                tool_call_id="call_1",
                                status=ToolResultStatus.SUCCESS,
                                output="错误 Run 结果",
                            ),
                        ),
                        tool_call_id="call_1",
                        source_message_id=user.id,
                        created_at=timestamp,
                        metadata=MessageMetadata(),
                    ),
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_execution(
                    session_factory,
                    repository,
                    ExecutionMessageDraft(
                        id="msg_execution_source",
                        session_id="session_1",
                        owner_id="user_1",
                        run_id="run_1",
                        thread_id="root:run_1",
                        role=ExecutionRole.SYSTEM,
                        parts=(TextPart("错误来源"),),
                        tool_call_id=None,
                        source_message_id=execution.id,
                        created_at=timestamp,
                        metadata=MessageMetadata(),
                    ),
                )
            tool = await _append_execution(
                session_factory,
                repository,
                ExecutionMessageDraft(
                    id="msg_tool_result",
                    session_id="session_1",
                    owner_id="user_1",
                    run_id="run_1",
                    thread_id="root:run_1",
                    role=ExecutionRole.TOOL,
                    parts=(
                        ToolResultPart(
                            tool_call_id="call_1",
                            status=ToolResultStatus.SUCCESS,
                            output="检索结果",
                        ),
                    ),
                    tool_call_id="call_1",
                    source_message_id=user.id,
                    created_at=timestamp,
                    metadata=MessageMetadata(),
                ),
            )
            assistant = await _append_conversation(
                session_factory,
                repository,
                ConversationMessageDraft(
                    id="msg_assistant",
                    session_id="session_1",
                    owner_id="user_1",
                    role=ConversationRole.ASSISTANT,
                    status=MessageStatus.COMPLETED,
                    parts=(TextPart("搜索结论"),),
                    reply_to_message_id=user.id,
                    run_id="run_1",
                    context_turn_id=user.id,
                    client_message_id=None,
                    created_at=timestamp,
                    completed_at=timestamp,
                    metadata=MessageMetadata(),
                ),
            )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_conversation(
                    session_factory,
                    repository,
                    ConversationMessageDraft(
                        id="msg_reply_to_assistant",
                        session_id="session_1",
                        owner_id="user_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("错误回复目标"),),
                        reply_to_message_id=assistant.id,
                        run_id="run_1",
                        context_turn_id=user.id,
                        client_message_id=None,
                        created_at=timestamp,
                        completed_at=timestamp,
                        metadata=MessageMetadata(),
                    ),
                )
            with pytest.raises(InvalidMessageReferenceError):
                await _append_conversation(
                    session_factory,
                    repository,
                    ConversationMessageDraft(
                        id="msg_cross_session_reply",
                        session_id="session_1",
                        owner_id="user_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("跨会话回复目标"),),
                        reply_to_message_id=other_user.id,
                        run_id="run_1",
                        context_turn_id=user.id,
                        client_message_id=None,
                        created_at=timestamp,
                        completed_at=timestamp,
                        metadata=MessageMetadata(),
                    ),
                )

            # 创建子 Run（depth=1）及其触发消息，同时验证跨 Run 同 thread 隔离与主历史排除子 Run。
            run2_trigger = await _append_conversation(
                session_factory,
                repository,
                ConversationMessageDraft(
                    id="msg_run2_sub",
                    session_id="session_1",
                    owner_id="user_1",
                    role=ConversationRole.USER,
                    status=MessageStatus.COMPLETED,
                    parts=(TextPart("子 Run 内部触发"),),
                    reply_to_message_id=None,
                    run_id="run_2",
                    context_turn_id="msg_run2_sub",
                    client_message_id=None,
                    created_at=timestamp,
                    completed_at=timestamp,
                    metadata=MessageMetadata(),
                ),
                run_depth=1,
            )
            cross_run_call = await _append_execution(
                session_factory,
                repository,
                ExecutionMessageDraft(
                    id="msg_cross_run_call",
                    session_id="session_1",
                    owner_id="user_1",
                    run_id="run_2",
                    thread_id="root:run_1",
                    role=ExecutionRole.ASSISTANT,
                    parts=(
                        ToolCallPart(
                            tool_call_id="call_1",
                            name="search",
                            arguments={"query": "复用的线程与调用 ID"},
                        ),
                    ),
                    tool_call_id=None,
                    source_message_id=user.id,
                    created_at=timestamp,
                    metadata=MessageMetadata(),
                ),
            )
            cross_run_tool = await _append_execution(
                session_factory,
                repository,
                ExecutionMessageDraft(
                    id="msg_cross_run_tool",
                    session_id="session_1",
                    owner_id="user_1",
                    run_id="run_2",
                    thread_id="root:run_1",
                    role=ExecutionRole.TOOL,
                    parts=(
                        ToolResultPart(
                            tool_call_id="call_1",
                            status=ToolResultStatus.SUCCESS,
                            output="不同 Run 的工具结果",
                        ),
                    ),
                    tool_call_id="call_1",
                    source_message_id=user.id,
                    created_at=timestamp,
                    metadata=MessageMetadata(),
                ),
            )

            assert (user.sequence, execution.sequence, tool.sequence, assistant.sequence) == (
                1,
                2,
                3,
                4,
            )
            assert (run2_trigger.sequence, cross_run_call.sequence, cross_run_tool.sequence) == (
                5,
                6,
                7,
            )
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
            assert [item.id for item in page.items] == ["msg_user", "msg_assistant"]

    asyncio.run(scenario())
