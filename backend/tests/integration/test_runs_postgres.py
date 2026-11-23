"""Run 数据迁移与 PostgreSQL 原子命令集成测试。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from integration.llm_fixtures import TEST_LLM_CIPHER, encrypted_llm_key_fields
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import func, select, text, update

from adapter.llm.model.profile import DefaultModelProfileModel, ModelProfileVersionModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.run.model import RunEventModel, RunModel, RunRequestModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.session.repository.session import SqlAlchemySessionRepository
from adapter.system_prompt.repository import SqlAlchemySystemPromptRepository
from app.run.executor import RunExecutor
from domain.llm.model import (
    LlmRequest,
    LlmResponse,
    LlmStreamEvent,
    LlmText,
    LlmToolCall,
    ResponseCompleted,
    TextDelta,
)
from domain.llm.model.errors import LlmProviderError
from domain.llm.model.profile import RuntimeModelProfile
from domain.llm.profile_ports import ModelProfileRepository
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
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import (
    AnswerRunCommand,
    FailRunCommand,
    FinishRunCommand,
    RecordRunUsageCommand,
    RequestRunInputCommand,
    Run,
    RunFailedPayload,
    RunLease,
    RunQueuedPayload,
    RunStatus,
    RunUsage,
    SubmitRunCommand,
    SubmitRunResult,
)
from domain.run.model.errors import (
    RunConflictError,
    RunStateError,
    SessionHasActiveRunError,
)
from domain.run.ports import RunRepository
from domain.system_prompt import INITIAL_SYSTEM_PROMPT_IDS

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="ATHENA_TEST_DATABASE_URL is not configured"),
]


def _draft(
    *,
    message_id: str,
    owner_id: str,
    session_id: str,
    run_id: str | None,
    text_value: str,
    client_message_id: str | None = None,
) -> ConversationMessageDraft:
    return ConversationMessageDraft(
        id=message_id,
        session_id=session_id,
        owner_id=owner_id,
        role=ConversationRole.USER,
        status=MessageStatus.COMPLETED,
        parts=(TextPart(text_value),),
        reply_to_message_id=None,
        run_id=run_id,
        client_message_id=client_message_id,
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        metadata=MessageMetadata(),
    )


def test_subagent_parent_id_columns_accept_domain_max_lengths() -> None:
    """真实迁移后的父子关联列应接受 Run 128 与工具调用 256 的边界 ID。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        async with isolated_postgres_schema(
            database_url, prefix="athena_v2_subagent_parent_ids"
        ) as db:
            test_engine = db.engine
            factory = db.session_factory
            now = datetime.now(UTC)
            async with test_engine.begin() as connection:
                column_lengths: dict[str, int | None] = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT column_name, character_maximum_length "
                                "FROM information_schema.columns "
                                "WHERE table_schema = current_schema() "
                                "AND table_name = 'runs' "
                                "AND column_name IN "
                                "('parent_run_id','root_run_id','parent_tool_call_id')"
                            )
                        )
                    ).all()
                )
                assert column_lengths == {
                    "parent_run_id": 128,
                    "root_run_id": 128,
                    "parent_tool_call_id": 256,
                }
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id,email,password_hash,status,created_at,updated_at) "
                        "VALUES ('usr_subagent_parent','parent@example.com','unused',"
                        "'active',:now,:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO sessions "
                        "(id,owner_id,title,project_id,last_message_sequence,created_at,"
                        "updated_at) "
                        "VALUES ('ses_subagent_parent','usr_subagent_parent',NULL,NULL,"
                        "1,:now,:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO conversation_messages "
                        "(id,session_id,role,status,sequence,parts,run_id,"
                        "client_message_id,created_at,metadata) VALUES "
                        "('msg_subagent_parent','ses_subagent_parent','user','completed',"
                        "1,CAST(:parts AS jsonb),NULL,NULL,:now,CAST('{}' AS jsonb))"
                    ),
                    {
                        "parts": json.dumps([{"type": "text", "text": "parent ids"}]),
                        "now": now,
                    },
                )

            parent_run_id = f"parent_{'r' * 121}"
            child_run_id = f"child_{'c' * 122}"
            parent_tool_call_id = f"tool_{'t' * 251}"
            assert len(parent_run_id) == 128
            assert len(child_run_id) == 128
            assert len(parent_tool_call_id) == 256
            profile_id = "llmprof_subagent_parent"
            async with factory() as session, session.begin():
                session.add(
                    ModelProfileVersionModel(
                        id=profile_id,
                        name="Subagent parent IDs",
                        protocol="chat_completions",
                        base_url="https://llm.example/v1",
                        model="test-model",
                        **encrypted_llm_key_fields(profile_id),
                        context_window_tokens=8192,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        max_output_tokens=512,
                        request_timeout_seconds=30,
                        execution_timeout_seconds=60,
                        max_concurrency=1,
                        temperature=None,
                        enabled=True,
                        created_at=now,
                    )
                )
                await session.flush()
                session.add(
                    RunModel(
                        id=parent_run_id,
                        session_id="ses_subagent_parent",
                        owner_id="usr_subagent_parent",
                        status=RunStatus.COMPLETED.value,
                        trigger_message_id="msg_subagent_parent",
                        context_cutoff_sequence=1,
                        retry_of_run_id=None,
                        model_config_version_id=profile_id,
                        main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                        compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=0,
                        queued_at=now,
                        finished_at=now,
                        created_at=now,
                        updated_at=now,
                        usage={},
                    )
                )
                await session.flush()
                session.add(
                    RunModel(
                        id=child_run_id,
                        session_id="ses_subagent_parent",
                        owner_id="usr_subagent_parent",
                        status=RunStatus.COMPLETED.value,
                        trigger_message_id="msg_subagent_parent",
                        context_cutoff_sequence=1,
                        retry_of_run_id=None,
                        model_config_version_id=profile_id,
                        main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                        compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=0,
                        queued_at=now,
                        finished_at=now,
                        created_at=now,
                        updated_at=now,
                        usage={},
                        parent_run_id=parent_run_id,
                        root_run_id=parent_run_id,
                        parent_tool_call_id=parent_tool_call_id,
                        depth=1,
                    )
                )

            async with factory() as session:
                child = await session.get(RunModel, child_run_id)
                assert child is not None
                assert child.parent_run_id == parent_run_id
                assert child.root_run_id == parent_run_id
                assert child.parent_tool_call_id == parent_tool_call_id

    asyncio.run(scenario())


def test_subagent_parent_id_width_migration_repairs_old_0029_layout() -> None:
    """旧版 0029 库应安全扩宽，降级不回缩，异常更宽列则整体拒绝。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        async with isolated_postgres_schema(
            database_url,
            prefix="athena_v2_subagent_parent_widths",
            revision="20261128_0029",
        ) as db:
            test_engine = db.engine

            async def _read_widths() -> dict[str, int | None]:
                """读取三个父 Run 关联列在真实 PostgreSQL 中的宽度。"""
                async with test_engine.connect() as connection:
                    return dict(
                        (
                            await connection.execute(
                                text(
                                    "SELECT column_name, character_maximum_length "
                                    "FROM information_schema.columns "
                                    "WHERE table_schema = current_schema() "
                                    "AND table_name = 'runs' "
                                    "AND column_name IN "
                                    "('parent_run_id','root_run_id','parent_tool_call_id')"
                                )
                            )
                        ).all()
                    )

            async with test_engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE runs "
                        "ALTER COLUMN parent_run_id TYPE VARCHAR(64), "
                        "ALTER COLUMN root_run_id TYPE VARCHAR(64), "
                        "ALTER COLUMN parent_tool_call_id TYPE VARCHAR(64)"
                    )
                )
            assert await _read_widths() == {
                "parent_run_id": 64,
                "root_run_id": 64,
                "parent_tool_call_id": 64,
            }

            await db.upgrade_to("head")
            assert await _read_widths() == {
                "parent_run_id": 128,
                "root_run_id": 128,
                "parent_tool_call_id": 256,
            }

            await db.downgrade_to("20261128_0029")
            assert await _read_widths() == {
                "parent_run_id": 128,
                "root_run_id": 128,
                "parent_tool_call_id": 256,
            }

            async with test_engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE runs "
                        "ALTER COLUMN parent_run_id TYPE VARCHAR(256), "
                        "ALTER COLUMN root_run_id TYPE VARCHAR(64)"
                    )
                )
            with pytest.raises(
                RuntimeError,
                match="subagent_parent_id_width_unsafe:parent_run_id:256>128",
            ):
                await db.upgrade_to("head")
            assert await _read_widths() == {
                "parent_run_id": 256,
                "root_run_id": 64,
                "parent_tool_call_id": 256,
            }

    asyncio.run(scenario())


def test_run_migration_preserves_legacy_ids_and_atomic_commands() -> None:
    """真实迁移保留历史 run_id，事务命令、幂等及事件在 PostgreSQL 正确工作。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        async with isolated_postgres_schema(
            database_url,
            prefix="athena_v2_run_test",
            revision="20261110_0011",
        ) as db:
            test_engine = db.engine
            now = datetime.now(UTC)
            async with test_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users (id,email,password_hash,status,created_at,updated_at) "
                        "VALUES ('usr_run_test','run-test@example.com','unused','active',:now,:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO sessions "
                        "(id,owner_id,title,project_id,last_message_sequence,created_at,"
                        "updated_at) "
                        "VALUES ('ses_run_test','usr_run_test',NULL,NULL,1,:now,:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO conversation_messages "
                        "(id,session_id,role,status,sequence,parts,run_id,client_message_id,"
                        "created_at,metadata) VALUES "
                        "('msg_legacy','ses_run_test','user','completed',"
                        "1,CAST(:parts AS jsonb),'legacy_run_id',NULL,:now,CAST('{}' AS jsonb))"
                    ),
                    {
                        "parts": json.dumps([{"type": "text", "text": "history"}]),
                        "now": now,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO execution_messages "
                        "(id,session_id,run_id,thread_id,sequence,role,parts,tool_call_id,"
                        "source_message_id,created_at,metadata) VALUES "
                        "('exec_legacy','ses_run_test',"
                        "'legacy_execution_id','thread_legacy',1,'assistant',CAST(:parts AS jsonb),"
                        "NULL,NULL,:now,CAST('{}' AS jsonb))"
                    ),
                    {
                        "parts": json.dumps([{"type": "text", "text": "internal history"}]),
                        "now": now,
                    },
                )
            await db.upgrade_to("head")
            factory = db.session_factory
            async with factory() as session:
                legacy = await session.get(ConversationMessageModel, "msg_legacy")
                assert legacy is not None
                assert legacy.run_id == "legacy_run_id"
                assert legacy.managed_run_id is None
                legacy_execution = await session.get(ExecutionMessageModel, "exec_legacy")
                assert legacy_execution is not None
                assert legacy_execution.run_id == "legacy_execution_id"
                assert legacy_execution.managed_run_id is None
                session.add(
                    ModelProfileVersionModel(
                        id="llmprof_run_test",
                        name="Run test",
                        protocol="chat_completions",
                        base_url="https://llm.example/v1",
                        model="test-model",
                        **encrypted_llm_key_fields("llmprof_run_test"),
                        context_window_tokens=8192,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        max_output_tokens=512,
                        request_timeout_seconds=30,
                        execution_timeout_seconds=60,
                        max_concurrency=1,
                        temperature=None,
                        enabled=True,
                        created_at=now,
                    )
                )
                await session.flush()
                session.add(DefaultModelProfileModel(singleton_id=1, profile_id="llmprof_run_test"))
                await session.commit()
                future_run_created_at = now + timedelta(days=1)
                session.add(
                    RunModel(
                        id="zz_future_clock_baseline",
                        session_id="ses_run_test",
                        owner_id="usr_run_test",
                        status=RunStatus.COMPLETED.value,
                        trigger_message_id="msg_legacy",
                        context_cutoff_sequence=1,
                        retry_of_run_id=None,
                        model_config_version_id="llmprof_run_test",
                        main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                        compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=0,
                        queued_at=now,
                        finished_at=future_run_created_at,
                        created_at=future_run_created_at,
                        updated_at=future_run_created_at,
                        usage={},
                    )
                )
                await session.commit()

            messages = SqlAlchemyMessageRepository(factory)
            repository = SqlAlchemyRunRepository(factory, messages)
            command_value = SubmitRunCommand(
                run_id="run_submit_test",
                request_id="req_submit_test",
                fingerprint="a" * 64,
                prompt_template_version="run-v2",
                message=_draft(
                    message_id="msg_submit_test",
                    owner_id="usr_run_test",
                    session_id="ses_run_test",
                    run_id="run_submit_test",
                    text_value="do work",
                ),
                queued_at=now,
            )
            failed_append = repository._append_event

            async def fail_event(*args: object, **kwargs: object) -> None:
                raise RuntimeError("injected_event_failure")

            repository._append_event = fail_event  # type: ignore[method-assign]
            with pytest.raises(RuntimeError, match="injected_event_failure"):
                await repository.submit("ses_run_test", "usr_run_test", command_value)
            repository._append_event = failed_append  # type: ignore[method-assign]
            async with factory() as session:
                assert await session.get(RunModel, "run_submit_test") is None
                assert await session.get(ConversationMessageModel, "msg_submit_test") is None
                assert await session.get(RunRequestModel, "runreq_not_created") is None
                session_row = await session.get(SessionModel, "ses_run_test")
                assert session_row is not None and session_row.last_message_sequence == 1

            submitted = await repository.submit("ses_run_test", "usr_run_test", command_value)
            assert submitted.run.status is RunStatus.QUEUED
            assert submitted.message.sequence == 2
            assert submitted.message.run_id == submitted.run.id
            assert submitted.run.created_at > future_run_created_at
            async with factory() as session:
                for sequence in range(3, 12):
                    session.add(
                        ConversationMessageModel(
                            id=f"msg_snapshot_{sequence}",
                            session_id="ses_run_test",
                            role="user",
                            status="completed",
                            sequence=sequence,
                            parts=[{"type": "text", "text": f"历史消息 {sequence}"}],
                            reply_to_message_id=None,
                            run_id=submitted.run.id,
                            managed_run_id=submitted.run.id,
                            client_message_id=None,
                            created_at=now,
                            completed_at=now,
                            metadata_json={},
                        )
                    )
                await session.execute(
                    update(SessionModel)
                    .where(SessionModel.id == "ses_run_test")
                    .values(last_message_sequence=11)
                )
                await session.commit()
            stream_snapshot = await repository.get_stream_snapshot(
                "ses_run_test", "usr_run_test", submitted.run.id
            )
            assert stream_snapshot is not None
            assert stream_snapshot.run.status is RunStatus.QUEUED
            assert stream_snapshot.run.event_sequence == 1
            assert len(stream_snapshot.messages) == 8
            assert stream_snapshot.messages_before_sequence == 4
            restored_history = await messages.list_conversation_for_owner(
                "ses_run_test", "usr_run_test", before_sequence=4, limit=100
            )
            assert restored_history is not None
            assert {message.id for message in restored_history.items} == {
                "msg_legacy",
                submitted.message.id,
                "msg_snapshot_3",
            }
            included_ids = {message.id for message in stream_snapshot.messages}
            assert not included_ids.intersection(message.id for message in restored_history.items)
            assert [event.sequence for event in stream_snapshot.events] == [1]
            assert not stream_snapshot.messages_complete
            assert stream_snapshot.events_complete
            async with factory() as session:
                session.add(
                    ConversationMessageModel(
                        id="msg_snapshot_utf8",
                        session_id="ses_run_test",
                        role="assistant",
                        status="completed",
                        sequence=12,
                        parts=[{"type": "text", "text": "界" * 200_000}],
                        reply_to_message_id=None,
                        run_id=submitted.run.id,
                        managed_run_id=submitted.run.id,
                        client_message_id=None,
                        created_at=now,
                        completed_at=now,
                        metadata_json={},
                    )
                )
                await session.execute(
                    update(SessionModel)
                    .where(SessionModel.id == "ses_run_test")
                    .values(last_message_sequence=12)
                )
                await session.commit()
            utf8_snapshot = await repository.get_stream_snapshot(
                "ses_run_test", "usr_run_test", submitted.run.id
            )
            assert utf8_snapshot is not None
            assert utf8_snapshot.messages == ()
            assert utf8_snapshot.messages_before_sequence == 13
            restored_utf8 = await messages.list_conversation_for_owner(
                "ses_run_test", "usr_run_test", before_sequence=13, limit=100
            )
            assert restored_utf8 is not None
            assert any(message.id == "msg_snapshot_utf8" for message in restored_utf8.items)
            for suffix, text_value in (("a", "repeat one"), ("b", "repeat two")):
                await messages.append_conversation(
                    _draft(
                        message_id=f"msg_same_role_{suffix}",
                        owner_id="usr_run_test",
                        session_id="ses_run_test",
                        run_id="legacy_run_without_entity",
                        text_value=text_value,
                    )
                )
            with pytest.raises(
                InvalidMessageReferenceError,
                match="managed_run_write_requires_run_command",
            ):
                await messages.append_conversation(
                    _draft(
                        message_id="msg_public_managed_bypass",
                        owner_id="usr_run_test",
                        session_id="ses_run_test",
                        run_id=submitted.run.id,
                        text_value="must use run command",
                    )
                )
            with pytest.raises(
                InvalidMessageReferenceError,
                match="managed_run_write_requires_run_command",
            ):
                await messages.append_execution(
                    ExecutionMessageDraft(
                        id="exec_public_managed_bypass",
                        session_id="ses_run_test",
                        owner_id="usr_run_test",
                        run_id=submitted.run.id,
                        thread_id="thread_run_test",
                        role=ExecutionRole.ASSISTANT,
                        parts=(ToolCallPart("call_public_bypass", "tool", {}),),
                        tool_call_id=None,
                        source_message_id=None,
                        created_at=now,
                        metadata=MessageMetadata(),
                    )
                )
            async with factory() as session:
                managed = await session.get(ConversationMessageModel, submitted.message.id)
                assert managed is not None and managed.managed_run_id == submitted.run.id
                event_rows = (
                    await session.scalars(
                        select(RunEventModel).where(RunEventModel.run_id == submitted.run.id)
                    )
                ).all()
                assert len(event_rows) == 1
                assert event_rows[0].sequence == 1
                assert event_rows[0].payload == RunQueuedPayload(
                    trigger_message_id=submitted.message.id
                ).model_dump(mode="json")
            async with factory() as session:
                for sequence in range(2, 106):
                    session.add(
                        RunEventModel(
                            id=f"evt_snapshot_{sequence}",
                            run_id=submitted.run.id,
                            session_id="ses_run_test",
                            sequence=sequence,
                            type="queued",
                            created_at=now,
                            payload={"trigger_message_id": submitted.message.id},
                        )
                    )
                await session.execute(
                    update(RunModel)
                    .where(RunModel.id == submitted.run.id)
                    .values(event_sequence=105)
                )
                await session.commit()
            large_event_snapshot = await repository.get_stream_snapshot(
                "ses_run_test", "usr_run_test", submitted.run.id
            )
            assert large_event_snapshot is not None
            assert len(large_event_snapshot.events) == 100
            assert large_event_snapshot.events[-1].sequence == 100
            assert not large_event_snapshot.events_complete
            assert large_event_snapshot.events_after_sequence == 100
            missing_events = await repository.list_events(
                "ses_run_test",
                "usr_run_test",
                submitted.run.id,
                after_sequence=large_event_snapshot.events_after_sequence,
                through_sequence=large_event_snapshot.run.event_sequence,
                limit=100,
            )
            assert [event.sequence for event in missing_events] == [101, 102, 103, 104, 105]
            replay = await repository.submit("ses_run_test", "usr_run_test", command_value)
            assert replay.replayed and replay.run.id == submitted.run.id
            with pytest.raises(RunConflictError, match="run_request_id_conflict"):
                await repository.submit(
                    "ses_run_test",
                    "usr_run_test",
                    replace(command_value, fingerprint="b" * 64),
                )

            lease = (
                await repository.claim(
                    "worker-one", lease_seconds=60, execution_seconds=300, limit=1
                )
            )[0]
            assert lease.run.execution_started_at is not None
            assert (
                lease.execution_deadline_at - lease.run.execution_started_at
            ).total_seconds() <= 60
            question = replace(
                command_value.message,
                id="msg_question_test",
                role=ConversationRole.ASSISTANT,
                run_id=submitted.run.id,
                client_message_id=None,
                parts=(TextPart("Which option?"),),
            )
            tool_call = ExecutionMessageDraft(
                id="exec_call_test",
                session_id="ses_run_test",
                owner_id="usr_run_test",
                run_id=submitted.run.id,
                thread_id="thread_run_test",
                role=ExecutionRole.ASSISTANT,
                parts=(
                    ToolCallPart(
                        "call_input_test", "request_user_input", {"question": "Which option?"}
                    ),
                ),
                tool_call_id=None,
                source_message_id=question.id,
                created_at=now,
                metadata=MessageMetadata(),
            )
            first_attempt = await repository.record_usage(
                RecordRunUsageCommand(
                    run_id=submitted.run.id,
                    worker_id="worker-one",
                    execution_epoch=lease.run.execution_epoch,
                    attempt=1,
                    input_tokens=10,
                    output_tokens=4,
                    total_tokens=14,
                    complete=True,
                    recorded_at=now,
                )
            )
            assert first_attempt is not None and first_attempt.usage.total_tokens == 14

            async def assert_invalid_input_request(
                suffix: str,
                question_value: ConversationMessageDraft,
                execution_values: tuple[ExecutionMessageDraft, ...],
            ) -> None:
                with pytest.raises(RunStateError):
                    await repository.request_input(
                        RequestRunInputCommand(
                            run_id=submitted.run.id,
                            worker_id="worker-one",
                            execution_epoch=lease.run.execution_epoch,
                            input_request_id=f"input_invalid_{suffix}",
                            question_message=question_value,
                            execution_messages=execution_values,
                            usage=RunUsage(),
                            created_at=now,
                        )
                    )
                async with factory() as check_session:
                    assert (
                        await check_session.get(ConversationMessageModel, question_value.id) is None
                    )
                    for execution_value in execution_values:
                        assert (
                            await check_session.get(ExecutionMessageModel, execution_value.id)
                            is None
                        )

            empty_question = replace(question, id="msg_question_empty_calls")
            await assert_invalid_input_request("empty", empty_question, ())

            wrong_source_question = replace(question, id="msg_question_wrong_source")
            wrong_source_call = replace(
                tool_call,
                id="exec_call_wrong_source",
                parts=(
                    ToolCallPart(
                        "call_wrong_source", "request_user_input", {"question": "Which option?"}
                    ),
                ),
                source_message_id="msg_unrelated_question",
            )
            await assert_invalid_input_request(
                "wrong_source", wrong_source_question, (wrong_source_call,)
            )

            wrong_text_question = replace(question, id="msg_question_wrong_text")
            wrong_text_call = replace(
                tool_call,
                id="exec_call_wrong_text",
                parts=(
                    ToolCallPart(
                        "call_wrong_text", "request_user_input", {"question": "different"}
                    ),
                ),
                source_message_id=wrong_text_question.id,
            )
            await assert_invalid_input_request(
                "wrong_text", wrong_text_question, (wrong_text_call,)
            )

            multiple_question = replace(question, id="msg_question_multiple_calls")
            multiple_call_one = replace(
                tool_call,
                id="exec_call_multiple_one",
                parts=(
                    ToolCallPart(
                        "call_multiple_one", "request_user_input", {"question": "Which option?"}
                    ),
                ),
                source_message_id=multiple_question.id,
            )
            multiple_call_two = replace(
                tool_call,
                id="exec_call_multiple_two",
                parts=(
                    ToolCallPart(
                        "call_multiple_two", "request_user_input", {"question": "Which option?"}
                    ),
                ),
                source_message_id=multiple_question.id,
            )
            await assert_invalid_input_request(
                "multiple", multiple_question, (multiple_call_one, multiple_call_two)
            )

            inconsistent_thread_question = replace(question, id="msg_question_inconsistent_thread")
            inconsistent_thread_call = replace(
                tool_call,
                id="exec_call_inconsistent_thread",
                parts=(
                    ToolCallPart(
                        "call_inconsistent_thread",
                        "request_user_input",
                        {"question": "Which option?"},
                    ),
                ),
                source_message_id=inconsistent_thread_question.id,
            )
            wrong_thread_extra = replace(
                tool_call,
                id="exec_call_wrong_thread_extra",
                thread_id="another-thread",
                parts=(ToolCallPart("call_other", "some_tool", {}),),
                source_message_id=inconsistent_thread_question.id,
            )
            await assert_invalid_input_request(
                "thread",
                inconsistent_thread_question,
                (inconsistent_thread_call, wrong_thread_extra),
            )

            input_request = await repository.request_input(
                RequestRunInputCommand(
                    run_id=submitted.run.id,
                    worker_id="worker-one",
                    execution_epoch=lease.run.execution_epoch,
                    input_request_id="input_test",
                    question_message=question,
                    execution_messages=(tool_call,),
                    usage=RunUsage(),
                    created_at=now,
                )
            )
            assert input_request.status.value == "pending"
            async with factory() as session:
                managed_question = await session.get(ConversationMessageModel, question.id)
                managed_call = await session.get(ExecutionMessageModel, tool_call.id)
                assert managed_question is not None
                assert managed_question.managed_run_id == submitted.run.id
                assert managed_call is not None and managed_call.managed_run_id == submitted.run.id

            answer_draft = replace(
                command_value.message,
                id="msg_answer_test",
                run_id=submitted.run.id,
                client_message_id="answer-client-id",
                parts=(TextPart("Option A"),),
            )
            tool_result = ExecutionMessageDraft(
                id="exec_result_test",
                session_id="ses_run_test",
                owner_id="usr_run_test",
                run_id=submitted.run.id,
                thread_id="thread_run_test",
                role=ExecutionRole.TOOL,
                parts=(
                    ToolResultPart("call_input_test", ToolResultStatus.SUCCESS, output="Option A"),
                ),
                tool_call_id="call_input_test",
                source_message_id=None,
                created_at=now,
                metadata=MessageMetadata(),
            )
            answer_command = AnswerRunCommand(
                run_id=submitted.run.id,
                input_request_id=input_request.id,
                request_id="req_answer_test",
                fingerprint="c" * 64,
                answer_message=answer_draft,
                execution_message=tool_result,
                queued_at=now,
            )
            resumed = await repository.answer(
                "ses_run_test",
                "usr_run_test",
                answer_command,
            )
            assert resumed.status is RunStatus.QUEUED
            assert (
                await repository.answer("ses_run_test", "usr_run_test", answer_command)
            ).status is RunStatus.QUEUED
            with pytest.raises(RunConflictError, match="run_request_id_conflict"):
                await repository.answer(
                    "ses_run_test",
                    "usr_run_test",
                    replace(answer_command, fingerprint="e" * 64),
                )
            current_events = await repository.list_events(
                "ses_run_test", "usr_run_test", submitted.run.id, after_sequence=105, limit=10
            )
            assert [event.type for event in current_events] == [
                "started",
                "waiting_for_input",
                "resumed",
            ]
            assert (
                await repository.get_pending_input("ses_run_test", "usr_run_test", submitted.run.id)
                is None
            )
            history = await repository.list_context_messages(
                "ses_run_test", "usr_run_test", submitted.run.id
            )
            assert history is not None
            assert any(getattr(item, "id", None) == tool_result.id for item in history)
            with pytest.raises(SessionHasActiveRunError):
                await SqlAlchemySessionRepository(factory).soft_delete(
                    "usr_run_test", "ses_run_test", now
                )
            resumed_lease = (
                await repository.claim(
                    "worker-after-input", lease_seconds=60, execution_seconds=300, limit=1
                )
            )[0]
            assert resumed_lease.run.id == submitted.run.id
            after_clarification = await repository.record_usage(
                RecordRunUsageCommand(
                    run_id=submitted.run.id,
                    worker_id="worker-after-input",
                    execution_epoch=resumed_lease.run.execution_epoch,
                    attempt=1,
                    input_tokens=None,
                    output_tokens=3,
                    total_tokens=None,
                    complete=False,
                    recorded_at=now,
                )
            )
            assert after_clarification is not None
            assert after_clarification.usage.input_tokens == 10
            assert after_clarification.usage.output_tokens == 7
            assert after_clarification.usage.total_tokens == 14
            assert after_clarification.usage.complete is False
            second_question = replace(
                question,
                id="msg_question_second_round",
                parts=(TextPart("Which option next?"),),
            )
            second_call = replace(
                tool_call,
                id="exec_call_second_round",
                parts=(
                    ToolCallPart(
                        "call_second_round",
                        "request_user_input",
                        {"question": "Which option next?"},
                    ),
                ),
                source_message_id=second_question.id,
            )
            second_input = await repository.request_input(
                RequestRunInputCommand(
                    run_id=submitted.run.id,
                    worker_id="worker-after-input",
                    execution_epoch=resumed_lease.run.execution_epoch,
                    input_request_id="input_second_round",
                    question_message=second_question,
                    execution_messages=(second_call,),
                    usage=RunUsage(),
                    created_at=now,
                )
            )
            assert second_input.status.value == "pending"
            second_answer = replace(
                answer_draft,
                id="msg_answer_second_round",
                parts=(TextPart("Second answer"),),
                client_message_id=None,
            )
            second_tool_result = replace(
                tool_result,
                id="exec_result_second_round",
                parts=(
                    ToolResultPart(
                        "call_second_round",
                        ToolResultStatus.SUCCESS,
                        output="Second answer",
                    ),
                ),
                tool_call_id="call_second_round",
            )
            second_answered = await repository.answer(
                "ses_run_test",
                "usr_run_test",
                AnswerRunCommand(
                    run_id=submitted.run.id,
                    input_request_id=second_input.id,
                    request_id="req_answer_second_round",
                    fingerprint="f" * 64,
                    answer_message=second_answer,
                    execution_message=second_tool_result,
                    queued_at=now,
                ),
            )
            assert second_answered.status is RunStatus.QUEUED
            after_second_answer_lease = (
                await repository.claim(
                    "worker-after-second-input",
                    lease_seconds=60,
                    execution_seconds=300,
                    limit=1,
                )
            )[0]
            assert after_second_answer_lease.run.id == submitted.run.id
            failed_after_usage = await repository.fail(
                FailRunCommand(
                    run_id=submitted.run.id,
                    worker_id="worker-after-second-input",
                    execution_epoch=after_second_answer_lease.run.execution_epoch,
                    safe_error_code="test_cleanup",
                    failed_at=now,
                )
            )
            assert failed_after_usage is not None

            race_commands = [
                SubmitRunCommand(
                    run_id=f"run_race_{index}",
                    request_id=f"req_race_{index}",
                    fingerprint=f"{index + 1:064x}",
                    prompt_template_version="run-v2",
                    message=_draft(
                        message_id=f"msg_race_{index}",
                        owner_id="usr_run_test",
                        session_id="ses_run_test",
                        run_id=f"run_race_{index}",
                        text_value=f"concurrent {index}",
                    ),
                    queued_at=now,
                )
                for index in range(2)
            ]
            barrier = asyncio.Barrier(3)

            async def concurrent_submit(command_value: SubmitRunCommand) -> object:
                await barrier.wait()
                try:
                    return await repository.submit("ses_run_test", "usr_run_test", command_value)
                except RunConflictError as error:
                    return error

            tasks = [asyncio.create_task(concurrent_submit(item)) for item in race_commands]
            await barrier.wait()
            race_results = await asyncio.gather(*tasks)
            assert sum(not isinstance(item, RunConflictError) for item in race_results) == 1
            assert sum(isinstance(item, RunConflictError) for item in race_results) == 1

            claim_barrier = asyncio.Barrier(3)

            async def concurrent_claim(worker_id: str) -> tuple[RunLease, ...]:
                await claim_barrier.wait()
                return await repository.claim(
                    worker_id, lease_seconds=60, execution_seconds=300, limit=1
                )

            claim_tasks = [
                asyncio.create_task(concurrent_claim("worker-race-a")),
                asyncio.create_task(concurrent_claim("worker-race-b")),
            ]
            await claim_barrier.wait()
            claim_results = await asyncio.gather(*claim_tasks)
            claimed = [item for result in claim_results for item in result]
            assert len(claimed) == 1
            claimed_run = claimed[0].run
            usage_command = RecordRunUsageCommand(
                run_id=claimed_run.id,
                worker_id=claimed_run.worker_id or "",
                execution_epoch=claimed_run.execution_epoch,
                attempt=1,
                input_tokens=12,
                output_tokens=3,
                total_tokens=15,
                complete=True,
                recorded_at=now,
            )
            first_usage = await repository.record_usage(usage_command)
            second_usage = await repository.record_usage(usage_command)
            assert first_usage is not None and second_usage is not None
            assert first_usage.usage == second_usage.usage
            assert first_usage.usage.total_tokens == 15
            with pytest.raises(RunConflictError, match="run_usage_attempts_required"):
                repository._merge_usage(
                    first_usage.usage.model_dump(mode="json"),
                    RunUsage(input_tokens=3, output_tokens=2, total_tokens=5),
                )
            async with factory() as session:
                await session.execute(
                    update(RunModel)
                    .where(RunModel.id == claimed_run.id)
                    .values(lease_expires_at=func.clock_timestamp() - text("interval '1 second'"))
                )
                await session.commit()
            assert (
                await repository.heartbeat(
                    claimed_run.id,
                    claimed_run.worker_id or "",
                    claimed_run.execution_epoch,
                    lease_seconds=60,
                )
                is None
            )
            assert await repository.record_usage(usage_command) is None
            expired_reply = replace(
                command_value.message,
                id="msg_expired_reply",
                role=ConversationRole.ASSISTANT,
                run_id=claimed_run.id,
                client_message_id=None,
                parts=(TextPart("should not persist"),),
            )
            assert (
                await repository.finish(
                    FinishRunCommand(
                        run_id=claimed_run.id,
                        worker_id=claimed_run.worker_id or "",
                        execution_epoch=claimed_run.execution_epoch,
                        reply_message=expired_reply,
                        usage=RunUsage(),
                        finished_at=now,
                    )
                )
                is None
            )
            async with factory() as session:
                assert await session.get(ConversationMessageModel, expired_reply.id) is None
            assert await repository.reclaim_expired(limit=5)
            recovered = await repository.get("ses_run_test", "usr_run_test", claimed_run.id)
            assert recovered is not None and recovered.status is RunStatus.FAILED

            retry = await repository.retry(
                "ses_run_test",
                "usr_run_test",
                claimed_run.id,
                "req_retry_test",
                "d" * 64,
                "run_retry_test",
                "run-v2",
                now + timedelta(seconds=1),
            )
            assert retry is not None
            assert retry.created_at > claimed_run.created_at
            assert retry.trigger_message_id == claimed_run.trigger_message_id
            assert retry.context_cutoff_sequence == claimed_run.context_cutoff_sequence
            assert retry.prompt_template_version == "run-v2"
            retry_replay = await repository.retry(
                "ses_run_test",
                "usr_run_test",
                claimed_run.id,
                "req_retry_test",
                "d" * 64,
                "run_retry_replay_should_not_be_created",
                "run-v2",
                now + timedelta(seconds=2),
            )
            assert retry_replay is not None and retry_replay.id == retry.id
            with pytest.raises(RunStateError, match="run_not_latest"):
                await repository.retry(
                    "ses_run_test",
                    "usr_run_test",
                    claimed_run.id,
                    "req_retry_stale_test",
                    "e" * 64,
                    "run_retry_stale_should_not_be_created",
                    "run-v2",
                    now + timedelta(seconds=2),
                )
            async with factory() as session:
                await session.execute(
                    update(RunModel).where(RunModel.id == retry.id).values(status="failed")
                )
                await session.commit()
            second_retry = await repository.retry(
                "ses_run_test",
                "usr_run_test",
                retry.id,
                "req_retry_second_test",
                "f" * 64,
                "run_retry_second_test",
                "run-v2",
                now + timedelta(seconds=3),
            )
            assert second_retry is not None
            assert second_retry.created_at > retry.created_at
            # 同一创建时间时，Run 列表以 ID 倒序打破并列；较小 ID 的 Run 不可重试。
            async with factory() as session:
                await session.execute(
                    update(RunModel).where(RunModel.id == second_retry.id).values(status="failed")
                )
                session.add(
                    RunModel(
                        id="zz_latest_tie_test",
                        session_id="ses_run_test",
                        owner_id="usr_run_test",
                        status=RunStatus.FAILED.value,
                        trigger_message_id=claimed_run.trigger_message_id,
                        context_cutoff_sequence=claimed_run.context_cutoff_sequence,
                        retry_of_run_id=None,
                        model_config_version_id="llmprof_run_test",
                        main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                        compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=0,
                        queued_at=second_retry.created_at,
                        created_at=second_retry.created_at,
                        updated_at=second_retry.created_at,
                        usage={},
                    )
                )
                await session.commit()
            with pytest.raises(RunStateError, match="run_not_latest"):
                await repository.retry(
                    "ses_run_test",
                    "usr_run_test",
                    second_retry.id,
                    "req_retry_tie_test",
                    "g" * 64,
                    "run_retry_tie_should_not_be_created",
                    "run-v2",
                    now + timedelta(seconds=3),
                )
            async with factory() as session:
                await session.execute(
                    update(RunModel).where(RunModel.id == second_retry.id).values(status="queued")
                )
                await session.commit()

            timeout_lease = (
                await repository.claim(
                    "worker-timeout", lease_seconds=60, execution_seconds=300, limit=1
                )
            )[0]
            async with factory() as session:
                await session.execute(
                    update(RunModel)
                    .where(RunModel.id == timeout_lease.run.id)
                    .values(
                        execution_deadline_at=func.clock_timestamp() - text("interval '1 second'")
                    )
                )
                await session.commit()
            timeout_result = await repository.fail(
                FailRunCommand(
                    run_id=timeout_lease.run.id,
                    worker_id="worker-timeout",
                    execution_epoch=timeout_lease.run.execution_epoch,
                    safe_error_code="execution_timeout",
                    failed_at=now,
                )
            )
            assert timeout_result is not None and timeout_result.status is RunStatus.FAILED

            cancel_command = replace(
                command_value,
                run_id="run_cancel_deadline",
                request_id="req_cancel_deadline",
                fingerprint="e" * 64,
                message=replace(
                    command_value.message,
                    id="msg_cancel_deadline",
                    run_id="run_cancel_deadline",
                    client_message_id=None,
                ),
                queued_at=now,
            )
            cancel_submitted = await repository.submit(
                "ses_run_test", "usr_run_test", cancel_command
            )
            cancel_lease = (
                await repository.claim(
                    "worker-cancel", lease_seconds=60, execution_seconds=300, limit=1
                )
            )[0]
            assert cancel_lease.run.id == cancel_submitted.run.id
            cancelling = await repository.cancel(
                "ses_run_test", "usr_run_test", cancel_submitted.run.id, now
            )
            assert cancelling is not None and cancelling.status is RunStatus.CANCELLING
            async with factory() as session:
                await session.execute(
                    update(RunModel)
                    .where(RunModel.id == cancel_submitted.run.id)
                    .values(
                        execution_deadline_at=func.clock_timestamp() - text("interval '1 second'")
                    )
                )
                await session.commit()
            cancel_heartbeat = await repository.heartbeat(
                cancel_submitted.run.id,
                "worker-cancel",
                cancel_lease.run.execution_epoch,
                lease_seconds=60,
            )
            assert cancel_heartbeat is not None
            settled = await repository.settle_cancelled(
                cancel_submitted.run.id,
                "worker-cancel",
                cancel_lease.run.execution_epoch,
                now,
            )
            assert settled is not None and settled.status is RunStatus.CANCELLED

            async with factory() as session:
                async with session.begin():
                    session.add(
                        SessionModel(
                            id="ses_profile_race",
                            owner_id="usr_run_test",
                            title=None,
                            project_id=None,
                            last_message_sequence=0,
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                    )
            config_barrier = asyncio.Barrier(3)

            async def race_submit() -> SubmitRunResult | RunStateError | None:
                await config_barrier.wait()
                command_value = SubmitRunCommand(
                    run_id="run_profile_race",
                    request_id="req_profile_race",
                    fingerprint="f" * 64,
                    prompt_template_version="run-v2",
                    message=_draft(
                        message_id="msg_profile_race",
                        owner_id="usr_run_test",
                        session_id="ses_profile_race",
                        run_id="run_profile_race",
                        text_value="profile race",
                    ),
                    queued_at=now,
                )
                try:
                    return await repository.submit(
                        "ses_profile_race", "usr_run_test", command_value
                    )
                except RunStateError as error:
                    return error

            async def race_disable() -> None:
                await config_barrier.wait()
                async with factory() as session:
                    async with session.begin():
                        profile_row = await session.scalar(
                            select(ModelProfileVersionModel)
                            .where(ModelProfileVersionModel.id == "llmprof_run_test")
                            .with_for_update()
                        )
                        assert profile_row is not None
                        profile_row.enabled = False

            config_tasks = [
                asyncio.create_task(race_submit()),
                asyncio.create_task(race_disable()),
            ]
            await config_barrier.wait()
            config_result, _ = await asyncio.gather(*config_tasks)
            if isinstance(config_result, RunStateError):
                assert str(config_result) == "llm_default_profile_unavailable"
                assert (
                    await repository.get("ses_profile_race", "usr_run_test", "run_profile_race")
                    is None
                )
            else:
                assert config_result is not None
                assert config_result.run.model_config_version_id == "llmprof_run_test"
                async with factory() as session:
                    async with session.begin():
                        await session.execute(
                            update(ModelProfileVersionModel)
                            .where(ModelProfileVersionModel.id == "llmprof_run_test")
                            .values(enabled=True)
                        )
                await repository.cancel("ses_profile_race", "usr_run_test", "run_profile_race", now)

            async with factory() as session:
                async with session.begin():
                    session.add(
                        SessionModel(
                            id="ses_executor_test",
                            owner_id="usr_run_test",
                            title=None,
                            project_id=None,
                            last_message_sequence=0,
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                    )
                    await session.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == "llmprof_run_test")
                        .values(enabled=True)
                    )
            executor_submit = SubmitRunCommand(
                run_id="run_executor_test",
                request_id="req_executor_test",
                fingerprint="9" * 64,
                prompt_template_version="run-v2",
                message=_draft(
                    message_id="msg_executor_test",
                    owner_id="usr_run_test",
                    session_id="ses_executor_test",
                    run_id="run_executor_test",
                    text_value="answer this literal <|endoftext|>",
                ),
                queued_at=now,
            )
            await repository.submit("ses_executor_test", "usr_run_test", executor_submit)
            executor_lease = (
                await repository.claim(
                    "worker-executor-test",
                    lease_seconds=60,
                    execution_seconds=300,
                    limit=1,
                )
            )[0]

            class FakeProvider:
                @asynccontextmanager
                async def stream(
                    self, runtime: RuntimeModelProfile, request: LlmRequest
                ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                    assert request.messages[-1].content == "answer this literal <|endoftext|>"

                    async def events() -> AsyncIterator[LlmStreamEvent]:
                        yield ResponseCompleted(
                            LlmResponse(
                                output=(LlmText("executor completed"),),
                                status="completed",
                                incomplete_reason=None,
                                model="fake-model",
                                response_id="fake-response",
                                request_id="fake-request",
                                usage=None,
                            )
                        )

                    yield events()

            repository_profiles = SqlAlchemyModelProfileRepository(factory, TEST_LLM_CIPHER)
            system_prompts = SqlAlchemySystemPromptRepository(factory)
            executor = RunExecutor(
                repository,
                repository_profiles,
                FakeProvider(),
                system_prompts=system_prompts,
            )
            await executor.execute(executor_lease)
            completed = await repository.get(
                "ses_executor_test", "usr_run_test", "run_executor_test"
            )
            assert completed is not None and completed.status is RunStatus.COMPLETED
            assert completed.usage.input_tokens is None
            assert completed.usage.output_tokens is None
            async with factory() as session:
                final_message = await session.scalar(
                    select(ConversationMessageModel).where(
                        ConversationMessageModel.run_id == "run_executor_test",
                        ConversationMessageModel.role == "assistant",
                    )
                )
                assert final_message is not None
                assert final_message.parts[0]["text"] == "executor completed"

            async with factory() as session:
                async with session.begin():
                    session.add(
                        SessionModel(
                            id="ses_executor_clarify",
                            owner_id="usr_run_test",
                            title=None,
                            project_id=None,
                            last_message_sequence=0,
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                    )
            clarify_run_id = "run_executor_clarify"
            await repository.submit(
                "ses_executor_clarify",
                "usr_run_test",
                SubmitRunCommand(
                    run_id=clarify_run_id,
                    request_id="req_executor_clarify",
                    fingerprint="8" * 64,
                    prompt_template_version="run-v2",
                    message=_draft(
                        message_id="msg_executor_clarify",
                        owner_id="usr_run_test",
                        session_id="ses_executor_clarify",
                        run_id=clarify_run_id,
                        text_value="prepare the plan",
                    ),
                    queued_at=now,
                ),
            )

            class OneResponseProvider:
                def __init__(
                    self, response: LlmResponse, expected_prior_call_ids: tuple[str, ...] = ()
                ):
                    self.response = response
                    self.expected_prior_call_ids = expected_prior_call_ids

                @asynccontextmanager
                async def stream(
                    self, runtime: RuntimeModelProfile, request: LlmRequest
                ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                    prior_call_ids = tuple(
                        call.call_id for message in request.messages for call in message.tool_calls
                    )
                    assert prior_call_ids == self.expected_prior_call_ids

                    async def events() -> AsyncIterator[LlmStreamEvent]:
                        yield ResponseCompleted(self.response)

                    yield events()

            prior_calls: list[str] = []
            for round_number in (1, 2):
                round_lease = (
                    await repository.claim(
                        f"worker-clarify-{round_number}",
                        lease_seconds=60,
                        execution_seconds=300,
                        limit=1,
                    )
                )[0]
                call_id = f"call_executor_clarify_{round_number}"
                response = LlmResponse(
                    output=(
                        LlmText("我需要确认一个细节。"),
                        LlmToolCall(
                            call_id=call_id,
                            name="request_user_input",
                            arguments={"question": f"Clarification {round_number}?"},
                        ),
                    ),
                    status="completed",
                    incomplete_reason=None,
                    model="fake-model",
                    response_id=f"fake-response-{round_number}",
                    request_id=f"fake-request-{round_number}",
                    usage=None,
                )
                await RunExecutor(
                    repository,
                    repository_profiles,
                    OneResponseProvider(response, tuple(prior_calls)),
                    system_prompts=system_prompts,
                ).execute(round_lease)
                pending = await repository.get_pending_input(
                    "ses_executor_clarify", "usr_run_test", clarify_run_id
                )
                assert pending is not None
                call = await repository.get_input_call(
                    "ses_executor_clarify",
                    "usr_run_test",
                    clarify_run_id,
                    pending.id,
                )
                assert call is not None and call.tool_call_id == call_id
                prior_calls.append(call_id)
                answer_id = f"msg_executor_answer_{round_number}"
                answer_text = f"Answer {round_number}"
                await repository.answer(
                    "ses_executor_clarify",
                    "usr_run_test",
                    AnswerRunCommand(
                        run_id=clarify_run_id,
                        input_request_id=pending.id,
                        request_id=f"req_executor_answer_{round_number}",
                        fingerprint=f"{round_number}" * 64,
                        answer_message=replace(
                            executor_submit.message,
                            id=answer_id,
                            session_id="ses_executor_clarify",
                            run_id=clarify_run_id,
                            client_message_id=None,
                            parts=(TextPart(answer_text),),
                        ),
                        execution_message=ExecutionMessageDraft(
                            id=f"exec_executor_answer_{round_number}",
                            session_id="ses_executor_clarify",
                            owner_id="usr_run_test",
                            run_id=clarify_run_id,
                            thread_id=call.thread_id,
                            role=ExecutionRole.TOOL,
                            parts=(
                                ToolResultPart(
                                    call.tool_call_id,
                                    ToolResultStatus.SUCCESS,
                                    output=answer_text,
                                ),
                            ),
                            tool_call_id=call.tool_call_id,
                            source_message_id=None,
                            created_at=now,
                            metadata=MessageMetadata(),
                        ),
                        queued_at=now,
                    ),
                )
            final_lease = (
                await repository.claim(
                    "worker-clarify-final",
                    lease_seconds=60,
                    execution_seconds=300,
                    limit=1,
                )
            )[0]
            final_response = LlmResponse(
                output=(LlmText("clarifications complete"),),
                status="completed",
                incomplete_reason=None,
                model="fake-model",
                response_id="fake-response-final",
                request_id="fake-request-final",
                usage=None,
            )
            await RunExecutor(
                repository,
                repository_profiles,
                OneResponseProvider(final_response, tuple(prior_calls)),
                system_prompts=system_prompts,
            ).execute(final_lease)
            clarified = await repository.get("ses_executor_clarify", "usr_run_test", clarify_run_id)
            assert clarified is not None and clarified.status is RunStatus.COMPLETED
            context = await repository.list_context_messages(
                "ses_executor_clarify", "usr_run_test", clarify_run_id
            )
            assert context is not None
            persisted_call_ids = [
                part.tool_call_id
                for message in context
                if isinstance(message, ExecutionMessage)
                for part in message.parts
                if isinstance(part, ToolCallPart)
            ]
            assert persisted_call_ids == prior_calls

            async with factory() as session:
                async with session.begin():
                    session.add(
                        SessionModel(
                            id="ses_executor_retry",
                            owner_id="usr_run_test",
                            title=None,
                            project_id=None,
                            last_message_sequence=0,
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                    )
            retry_run_id = "run_executor_retry"
            await repository.submit(
                "ses_executor_retry",
                "usr_run_test",
                SubmitRunCommand(
                    run_id=retry_run_id,
                    request_id="req_executor_retry",
                    fingerprint="7" * 64,
                    prompt_template_version="run-v2",
                    message=_draft(
                        message_id="msg_executor_retry",
                        owner_id="usr_run_test",
                        session_id="ses_executor_retry",
                        run_id=retry_run_id,
                        text_value="retry safely",
                    ),
                    queued_at=now,
                ),
            )
            retry_lease = (
                await repository.claim(
                    "worker-executor-retry",
                    lease_seconds=60,
                    execution_seconds=300,
                    limit=1,
                )
            )[0]

            class RetryProvider:
                def __init__(self) -> None:
                    self.calls = 0
                    self.api_keys: list[str] = []

                @asynccontextmanager
                async def stream(
                    self, runtime: RuntimeModelProfile, request: LlmRequest
                ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                    self.calls += 1
                    self.api_keys.append(runtime.api_key)
                    call_number = self.calls

                    async def events() -> AsyncIterator[LlmStreamEvent]:
                        if call_number > 1:
                            yield TextDelta(0, 0, "partial")
                        raise LlmProviderError("llm_rate_limited", retry_after_seconds=0.4)

                    yield events()

            retry_provider = RetryProvider()
            retry_delays: list[float] = []

            async def record_retry_delay(seconds: float) -> None:
                retry_delays.append(seconds)

            await RunExecutor(
                repository,
                repository_profiles,
                retry_provider,
                system_prompts=system_prompts,
                sleep=record_retry_delay,
            ).execute(retry_lease)
            retried = await repository.get("ses_executor_retry", "usr_run_test", retry_run_id)
            assert retried is not None and retried.status is RunStatus.FAILED
            assert retry_provider.calls == 2
            assert retry_provider.api_keys == ["test-secret", "test-secret"]
            assert retry_delays == [0.4]
            assert [item.attempt for item in retried.usage.attempts] == [1, 2]

            auth_run_id = "run_executor_auth_error"
            await repository.submit(
                "ses_executor_retry",
                "usr_run_test",
                SubmitRunCommand(
                    run_id=auth_run_id,
                    request_id="req_executor_auth_error",
                    fingerprint="6" * 64,
                    prompt_template_version="run-v2",
                    message=_draft(
                        message_id="msg_executor_auth_error",
                        owner_id="usr_run_test",
                        session_id="ses_executor_retry",
                        run_id=auth_run_id,
                        text_value="do not retry authentication errors",
                    ),
                    queued_at=now,
                ),
            )
            auth_lease = (
                await repository.claim(
                    "worker-executor-auth",
                    lease_seconds=60,
                    execution_seconds=300,
                    limit=1,
                )
            )[0]

            class AuthenticationFailureProvider:
                def __init__(self) -> None:
                    self.calls = 0

                @asynccontextmanager
                async def stream(
                    self, runtime: RuntimeModelProfile, request: LlmRequest
                ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
                    self.calls += 1

                    async def events() -> AsyncIterator[LlmStreamEvent]:
                        raise LlmProviderError("llm_authentication_failed")
                        yield ResponseCompleted(
                            LlmResponse(
                                output=(),
                                status="completed",
                                incomplete_reason=None,
                                model=None,
                                response_id=None,
                                request_id=None,
                                usage=None,
                            )
                        )

                    yield events()

            auth_provider = AuthenticationFailureProvider()
            auth_delays: list[float] = []

            async def auth_retry_delay(seconds: float) -> None:
                auth_delays.append(seconds)

            await RunExecutor(
                repository,
                repository_profiles,
                auth_provider,
                system_prompts=system_prompts,
                sleep=auth_retry_delay,
            ).execute(auth_lease)
            auth_failed = await repository.get("ses_executor_retry", "usr_run_test", auth_run_id)
            assert auth_failed is not None and auth_failed.status is RunStatus.FAILED
            assert auth_provider.calls == 1 and not auth_delays

            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == "llmprof_run_test")
                        .values(execution_timeout_seconds=1)
                    )
            context_deadline_run_id = "run_executor_context_deadline"
            await repository.submit(
                "ses_executor_retry",
                "usr_run_test",
                SubmitRunCommand(
                    run_id=context_deadline_run_id,
                    request_id="req_executor_context_deadline",
                    fingerprint="4" * 64,
                    prompt_template_version="run-v2",
                    message=_draft(
                        message_id="msg_executor_context_deadline",
                        owner_id="usr_run_test",
                        session_id="ses_executor_retry",
                        run_id=context_deadline_run_id,
                        text_value="context preparation uses the execution budget",
                    ),
                    queued_at=now,
                ),
            )
            context_lease = (
                await repository.claim(
                    "worker-executor-context-deadline",
                    lease_seconds=60,
                    execution_seconds=300,
                    limit=1,
                )
            )[0]
            assert context_lease.run.execution_started_at is not None
            assert (
                context_lease.execution_deadline_at - context_lease.run.execution_started_at
            ).total_seconds() <= 1

            class SlowContextRepository:
                async def list_context_messages(
                    self, session_id: str, owner_id: str, run_id: str
                ) -> tuple[ConversationMessage | ExecutionMessage, ...] | None:
                    await asyncio.sleep(1.2)
                    return await repository.list_context_messages(session_id, owner_id, run_id)

                async def fail(self, command: FailRunCommand) -> Run | None:
                    return await repository.fail(command)

            context_provider = RetryProvider()
            await RunExecutor(
                cast(RunRepository, SlowContextRepository()),
                repository_profiles,
                context_provider,
                system_prompts=system_prompts,
            ).execute(context_lease)
            context_failed = await repository.get(
                "ses_executor_retry", "usr_run_test", context_deadline_run_id
            )
            assert context_failed is not None and context_failed.status is RunStatus.FAILED
            assert context_provider.calls == 0
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == "llmprof_run_test")
                        .values(execution_timeout_seconds=60)
                    )

            disabled_run_id = "run_executor_disabled_during_backoff"
            await repository.submit(
                "ses_executor_retry",
                "usr_run_test",
                SubmitRunCommand(
                    run_id=disabled_run_id,
                    request_id="req_executor_disabled_during_backoff",
                    fingerprint="5" * 64,
                    prompt_template_version="run-v2",
                    message=_draft(
                        message_id="msg_executor_disabled_during_backoff",
                        owner_id="usr_run_test",
                        session_id="ses_executor_retry",
                        run_id=disabled_run_id,
                        text_value="profile may be disabled before retry",
                    ),
                    queued_at=now,
                ),
            )
            disabled_lease = (
                await repository.claim(
                    "worker-executor-disabled",
                    lease_seconds=60,
                    execution_seconds=300,
                    limit=1,
                )
            )[0]

            class DisabledOnSecondAttemptProfiles:
                def __init__(self) -> None:
                    self.calls = 0

                async def get_runtime(self, profile_id: str) -> RuntimeModelProfile | None:
                    self.calls += 1
                    if self.calls >= 3:
                        return None
                    return await repository_profiles.get_runtime(profile_id)

            disabled_profiles = DisabledOnSecondAttemptProfiles()
            disabled_provider = RetryProvider()
            await RunExecutor(
                repository,
                cast(ModelProfileRepository, disabled_profiles),
                disabled_provider,
                system_prompts=system_prompts,
                sleep=record_retry_delay,
            ).execute(disabled_lease)
            disabled = await repository.get("ses_executor_retry", "usr_run_test", disabled_run_id)
            assert disabled is not None and disabled.status is RunStatus.FAILED
            assert disabled_provider.calls == 1
            assert disabled_profiles.calls == 3

            unavailable_run_id = "run_profile_unavailable_at_claim"
            await repository.submit(
                "ses_executor_retry",
                "usr_run_test",
                SubmitRunCommand(
                    run_id=unavailable_run_id,
                    request_id="req_profile_unavailable_at_claim",
                    fingerprint="3" * 64,
                    prompt_template_version="run-v2",
                    message=_draft(
                        message_id="msg_profile_unavailable_at_claim",
                        owner_id="usr_run_test",
                        session_id="ses_executor_retry",
                        run_id=unavailable_run_id,
                        text_value="profile is disabled before claim",
                    ),
                    queued_at=now,
                ),
            )
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == "llmprof_run_test")
                        .values(enabled=False)
                    )
            assert (
                await repository.claim(
                    "worker-profile-unavailable",
                    lease_seconds=60,
                    execution_seconds=300,
                    limit=1,
                )
                == ()
            )
            unavailable = await repository.get(
                "ses_executor_retry", "usr_run_test", unavailable_run_id
            )
            assert unavailable is not None and unavailable.status is RunStatus.FAILED
            unavailable_events = await repository.list_events(
                "ses_executor_retry",
                "usr_run_test",
                unavailable_run_id,
                after_sequence=0,
                limit=10,
            )
            assert [event.type for event in unavailable_events] == ["queued", "failed"]
            failed_payload = unavailable_events[-1].payload
            assert isinstance(failed_payload, RunFailedPayload)
            assert failed_payload.safe_error_code == "model_profile_unavailable"
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == "llmprof_run_test")
                        .values(enabled=True)
                    )
            downgrade_error = "Cannot restore uq_conversation_messages_run_role"
            with pytest.raises(RuntimeError, match=downgrade_error):
                await db.downgrade_to("20261110_0011")

    asyncio.run(scenario())


def test_tool_approval_migration_upgrades_legacy_run_tool_and_mcp_rows() -> None:
    """0014 的历史 Run、工具调用、MCP 与模型配置行可无损升级到审批版本。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        async with isolated_postgres_schema(
            database_url,
            prefix="athena_v2_approval_migration",
            revision="20261113_0014",
        ) as db:
            test_engine = db.engine
            now = datetime.now(UTC)
            async with test_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id,email,password_hash,status,created_at,updated_at) "
                        "VALUES ('usr_approval_migration','migration@example.com','x',"
                        "'active',:now,:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO sessions "
                        "(id,owner_id,title,project_id,last_message_sequence,created_at,"
                        "updated_at) VALUES ('ses_approval_migration','usr_approval_migration',"
                        "NULL,NULL,1,:now,:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO conversation_messages "
                        "(id,session_id,role,status,sequence,parts,run_id,client_message_id,"
                        "created_at,metadata) VALUES "
                        "('msg_approval_migration','ses_approval_migration','user',"
                        "'completed',1,CAST(:parts AS jsonb),NULL,NULL,:now,"
                        "CAST('{}' AS jsonb))"
                    ),
                    {
                        "parts": json.dumps([{"type": "text", "text": "legacy"}]),
                        "now": now,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO execution_messages "
                        "(id,session_id,run_id,thread_id,sequence,role,parts,tool_call_id,"
                        "source_message_id,created_at,metadata) VALUES "
                        "('exec_approval_migration','ses_approval_migration',"
                        "'run_approval_migration','thread_legacy',1,'assistant',"
                        "CAST(:parts AS jsonb),NULL,NULL,:now,CAST('{}' AS jsonb))"
                    ),
                    {
                        "parts": json.dumps(
                            [
                                {
                                    "type": "tool_call",
                                    "tool_call_id": "call_legacy",
                                    "name": "legacy_tool",
                                    "arguments": {"query": "legacy"},
                                }
                            ]
                        ),
                        "now": now,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO llm_model_profile_versions "
                        "(id,name,protocol,base_url,model,credential_env,"
                        "context_window_tokens,tokenizer_encoding,max_output_tokens,"
                        "request_timeout_seconds,execution_timeout_seconds,max_concurrency,"
                        "temperature,enabled,created_at) VALUES "
                        "('llmprof_approval_migration','Legacy','chat_completions',"
                        "'https://llm.example/v1','legacy','ATHENA_LLM_CREDENTIAL_LEGACY',"
                        "8192,'o200k_base',512,30,60,1,NULL,true,:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO llm_compression_policy_versions "
                        "(id,compression_model_profile_id,trigger_ratio,recent_ratio,"
                        "summary_ratio,prompt_template_version,created_at) VALUES "
                        "('cmpol_approval_migration','llmprof_approval_migration',"
                        "0.8,0.2,0.1,'context-summary-v1',:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO llm_default_model_profile (singleton_id,profile_id) "
                        "VALUES (1,'llmprof_approval_migration')"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO llm_current_compression_policy "
                        "(singleton_id,policy_version_id) VALUES "
                        "(1,'cmpol_approval_migration')"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO runs "
                        "(id,session_id,owner_id,status,trigger_message_id,"
                        "context_cutoff_sequence,model_config_version_id,"
                        "compression_policy_version_id,"
                        "prompt_template_version,execution_epoch,queued_at,created_at,"
                        "updated_at) VALUES "
                        "('run_approval_migration','ses_approval_migration',"
                        "'usr_approval_migration','completed','msg_approval_migration',1,"
                        "'llmprof_approval_migration','cmpol_approval_migration',"
                        "'run-v2',1,:now,:now,:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO run_tool_calls "
                        "(id,run_id,session_id,tool_call_id,ordinal,tool_id,tool_kind,"
                        "model_name,execution_epoch,status,call_message_id,started_at,"
                        "finished_at,safe_error_code,outcome,round) VALUES "
                        "('toolcall_legacy','run_approval_migration',"
                        "'ses_approval_migration','call_legacy',1,'builtin_legacy',"
                        "'builtin','legacy_tool',1,'completed','exec_approval_migration',"
                        ":started,:now,NULL,CAST(:outcome AS jsonb),0)"
                    ),
                    {
                        "started": now - timedelta(seconds=1),
                        "now": now,
                        "outcome": json.dumps(
                            {
                                "status": "ok",
                                "code": "tool_completed",
                                "message": "done",
                            }
                        ),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO mcp_servers "
                        "(id,name,transport,config_json,config_version,created_at,updated_at) "
                        "VALUES ('mcp_legacy','Legacy','stdio',CAST(:config AS jsonb),1,"
                        ":now,:now)"
                    ),
                    {
                        "config": json.dumps(
                            {
                                "transport": "stdio",
                                "command": "/bin/true",
                                "args": [],
                            }
                        ),
                        "now": now,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO mcp_tools "
                        "(id,server_id,remote_name,model_name,description,"
                        "input_schema_json,created_at) VALUES "
                        "('tool_legacy','mcp_legacy','remote_legacy','mcp__legacy',"
                        "'legacy',CAST(:schema AS jsonb),:now)"
                    ),
                    {"schema": json.dumps({"type": "object"}), "now": now},
                )
            await db.upgrade_to("head")
            async with test_engine.connect() as connection:
                tool_row = (
                    await connection.execute(
                        text(
                            "SELECT status, requires_approval, requested_at = started_at "
                            "AS requested_backfilled FROM run_tool_calls "
                            "WHERE id = 'toolcall_legacy'"
                        )
                    )
                ).one()
                mcp_row = (
                    await connection.execute(
                        text("SELECT requires_approval FROM mcp_tools WHERE id = 'tool_legacy'")
                    )
                ).one()
                profile_row = (
                    await connection.execute(
                        text(
                            "SELECT supports_parallel_tool_calls "
                            "FROM llm_model_profile_versions "
                            "WHERE id = 'llmprof_approval_migration'"
                        )
                    )
                ).one()
                profile_state = (
                    await connection.execute(
                        text(
                            "SELECT enabled, api_key_ciphertext IS NULL AS no_key "
                            "FROM llm_model_profile_versions "
                            "WHERE id = 'llmprof_approval_migration'"
                        )
                    )
                ).one()
                pointer_state = (
                    await connection.execute(
                        text(
                            "SELECT (SELECT count(*) FROM llm_default_model_profile) "
                            "AS defaults, (SELECT count(*) FROM llm_current_compression_policy) "
                            "AS current_policies, (SELECT count(*) "
                            "FROM llm_compression_policy_versions "
                            "WHERE id = 'cmpol_approval_migration') AS history"
                        )
                    )
                ).one()
                run_snapshot = (
                    await connection.execute(
                        text(
                            "SELECT model_config_version_id,compression_policy_version_id "
                            "FROM runs WHERE id = 'run_approval_migration'"
                        )
                    )
                ).one()
                run_row = (
                    await connection.execute(
                        text("SELECT status FROM runs WHERE id = 'run_approval_migration'")
                    )
                ).one()
            assert tool_row.status == "completed"
            assert tool_row.requires_approval is False
            assert tool_row.requested_backfilled is True
            assert mcp_row.requires_approval is False
            assert profile_row.supports_parallel_tool_calls is False
            assert profile_state.enabled is False
            assert profile_state.no_key is True
            assert pointer_state.defaults == 0
            assert pointer_state.current_policies == 0
            assert pointer_state.history == 1
            assert run_snapshot.model_config_version_id == "llmprof_approval_migration"
            assert run_snapshot.compression_policy_version_id == "cmpol_approval_migration"
            assert run_row.status == "completed"
            factory = db.session_factory
            messages = SqlAlchemyMessageRepository(factory)
            repository = SqlAlchemyRunRepository(factory, messages)
            async with factory() as session, session.begin():
                legacy_profile = await session.get(
                    ModelProfileVersionModel, "llmprof_approval_migration"
                )
                assert legacy_profile is not None
                legacy_profile.enabled = True
                session.add(
                    DefaultModelProfileModel(
                        singleton_id=1,
                        profile_id="llmprof_approval_migration",
                    )
                )
                await session.flush()
                with pytest.raises(RunStateError, match="llm_default_profile_unavailable"):
                    await repository._lock_default_profile_id(session)
                assert await repository._current_compression_policy_id(session) is None

    asyncio.run(scenario())
