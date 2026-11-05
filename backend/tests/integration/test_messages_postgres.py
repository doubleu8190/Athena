"""消息 PostgreSQL 迁移与仓储集成测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.session.model import SessionModel
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    InvalidMessageReferenceError,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "backend" / "alembic.ini"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="ATHENA_TEST_DATABASE_URL is not configured",
    ),
]


def test_message_migration_and_repository_round_trip() -> None:
    """在临时 schema 中执行真实迁移并验证两条消息流。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        schema = f"athena_v2_message_test_{token_hex(8)}"
        admin_engine = create_async_engine(database_url, poolclass=NullPool)
        test_engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

            migration_config = Config(str(ALEMBIC_INI))
            migration_config.set_main_option(
                "sqlalchemy.url",
                database_url.replace("%", "%%"),
            )
            migration_config.attributes["connect_args"] = {
                "server_settings": {"search_path": schema}
            }
            await asyncio.to_thread(command.upgrade, migration_config, "head")

            session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
            async with test_engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "20261105_0005"

            timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            async with session_factory() as session:
                async with session.begin():
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
            user = await repository.append_conversation(
                ConversationMessageDraft(
                    id="msg_user",
                    session_id="session_1",
                    owner_id="user_1",
                    role=ConversationRole.USER,
                    status=MessageStatus.COMPLETED,
                    parts=(TextPart("请调用搜索"),),
                    reply_to_message_id=None,
                    run_id="run_1",
                    client_message_id="client_1",
                    created_at=timestamp,
                    completed_at=timestamp,
                    metadata=MessageMetadata(),
                )
            )
            other_user = await repository.append_conversation(
                ConversationMessageDraft(
                    id="msg_other_user",
                    session_id="session_2",
                    owner_id="user_2",
                    role=ConversationRole.USER,
                    status=MessageStatus.COMPLETED,
                    parts=(TextPart("其他会话"),),
                    reply_to_message_id=None,
                    run_id="run_other",
                    client_message_id="client_other",
                    created_at=timestamp,
                    completed_at=timestamp,
                    metadata=MessageMetadata(),
                )
            )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
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
                    )
                )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
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
                    )
                )
            execution = await repository.append_execution(
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
                )
            )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
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
                    )
                )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
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
                    )
                )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_execution(
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
                    )
                )
            tool = await repository.append_execution(
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
                )
            )
            assistant = await repository.append_conversation(
                ConversationMessageDraft(
                    id="msg_assistant",
                    session_id="session_1",
                    owner_id="user_1",
                    role=ConversationRole.ASSISTANT,
                    status=MessageStatus.COMPLETED,
                    parts=(TextPart("搜索结论"),),
                    reply_to_message_id=user.id,
                    run_id="run_1",
                    client_message_id=None,
                    created_at=timestamp,
                    completed_at=timestamp,
                    metadata=MessageMetadata(),
                )
            )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_conversation(
                    ConversationMessageDraft(
                        id="msg_reply_to_assistant",
                        session_id="session_1",
                        owner_id="user_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("错误回复目标"),),
                        reply_to_message_id=assistant.id,
                        run_id="run_2",
                        client_message_id=None,
                        created_at=timestamp,
                        completed_at=timestamp,
                        metadata=MessageMetadata(),
                    )
                )
            with pytest.raises(InvalidMessageReferenceError):
                await repository.append_conversation(
                    ConversationMessageDraft(
                        id="msg_cross_session_reply",
                        session_id="session_1",
                        owner_id="user_1",
                        role=ConversationRole.ASSISTANT,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("跨会话回复目标"),),
                        reply_to_message_id=other_user.id,
                        run_id="run_3",
                        client_message_id=None,
                        created_at=timestamp,
                        completed_at=timestamp,
                        metadata=MessageMetadata(),
                    )
                )

            cross_run_call = await repository.append_execution(
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
                )
            )
            cross_run_tool = await repository.append_execution(
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
                )
            )

            assert (user.sequence, execution.sequence, tool.sequence, assistant.sequence) == (
                1,
                2,
                3,
                4,
            )
            assert (cross_run_call.sequence, cross_run_tool.sequence) == (5, 6)
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
        finally:
            await test_engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin_engine.dispose()

    asyncio.run(scenario())
