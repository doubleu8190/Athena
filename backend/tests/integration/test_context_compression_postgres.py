"""上下文摘要 PostgreSQL 事务、幂等与失租验证。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.llm.model.profile import (
    CompressionPolicyVersionModel,
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.message.model import ConversationMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.model import RunEventModel, RunModel, SessionContextSummaryModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from app.run.compression import _turn_groups
from domain.message import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
)
from domain.run.model import (
    RecordRunUsageCommand,
    RunCompressionCompletedPayload,
    SaveContextSummaryCommand,
    SubmitRunCommand,
)
from domain.run.model.errors import RunStateError

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="ATHENA_TEST_DATABASE_URL is required"),
]


def test_summary_and_completed_event_commit_atomically_and_require_live_lease() -> None:
    """重复操作复用已提交摘要，失租后拒绝创建新摘要。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is required")

    async def scenario() -> None:
        schema = f"athena_context_compression_{token_hex(8)}"
        admin_engine = create_async_engine(database_url, poolclass=NullPool)
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            config = Config(str(ALEMBIC_INI))
            config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            config.attributes["connect_args"] = {
                "server_settings": {"search_path": f"{schema},public"}
            }
            config.attributes["version_table_schema"] = schema
            await asyncio.to_thread(command.upgrade, config, "head")

            factory = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(UTC)
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="usr_compression_pg",
                        email="compression-pg@example.test",
                        password_hash="unused",
                        display_name=None,
                        status="active",
                        role="user",
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                await session.flush()
                session.add(
                    SessionModel(
                        id="ses_compression_pg",
                        owner_id="usr_compression_pg",
                        title=None,
                        project_id=None,
                        last_message_sequence=2,
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                session.add(
                    SessionModel(
                        id="ses_snapshot_pg",
                        owner_id="usr_compression_pg",
                        title=None,
                        project_id=None,
                        last_message_sequence=0,
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        ModelProfileVersionModel(
                            id="llmprof_compression_pg",
                            name="compression test",
                            protocol="chat_completions",
                            base_url="https://llm.example.test/v1",
                            model="summary-model",
                            credential_env="ATHENA_TEST_KEY",
                            context_window_tokens=2048,
                            tokenizer_encoding="o200k_base",
                            supports_stream=False,
                            supports_tools=False,
                            max_output_tokens=128,
                            request_timeout_seconds=10,
                            execution_timeout_seconds=30,
                            max_concurrency=1,
                            temperature=None,
                            enabled=True,
                            created_at=now,
                        ),
                        ModelProfileVersionModel(
                            id="llmprof_main_pg",
                            name="main test",
                            protocol="chat_completions",
                            base_url="https://llm.example.test/v1",
                            model="main-model",
                            credential_env="ATHENA_TEST_KEY",
                            context_window_tokens=2048,
                            tokenizer_encoding="o200k_base",
                            supports_stream=True,
                            supports_tools=True,
                            max_output_tokens=128,
                            request_timeout_seconds=10,
                            execution_timeout_seconds=30,
                            max_concurrency=1,
                            temperature=None,
                            enabled=True,
                            created_at=now,
                        ),
                        ConversationMessageModel(
                            id="msg_compression_history",
                            session_id="ses_compression_pg",
                            role="user",
                            status="completed",
                            sequence=1,
                            parts=[{"type": "text", "text": "history"}],
                            reply_to_message_id=None,
                            run_id=None,
                            managed_run_id=None,
                            client_message_id=None,
                            created_at=now,
                            completed_at=now,
                            metadata_json={},
                        ),
                        ConversationMessageModel(
                            id="msg_compression_trigger",
                            session_id="ses_compression_pg",
                            role="user",
                            status="completed",
                            sequence=2,
                            parts=[{"type": "text", "text": "trigger"}],
                            reply_to_message_id=None,
                            run_id="run_compression_pg",
                            managed_run_id="run_compression_pg",
                            client_message_id=None,
                            created_at=now,
                            completed_at=now,
                            metadata_json={},
                        ),
                    ]
                )
                await session.flush()
                session.add(
                    CompressionPolicyVersionModel(
                        id="cmpol_compression_pg",
                        compression_model_profile_id="llmprof_compression_pg",
                        trigger_ratio=0.8,
                        recent_ratio=0.2,
                        summary_ratio=0.1,
                        prompt_template_version="context-summary-v1",
                        created_at=now,
                    )
                )
                session.add_all(
                    [
                        DefaultModelProfileModel(singleton_id=1, profile_id="llmprof_main_pg"),
                        CurrentCompressionPolicyModel(
                            singleton_id=1, policy_version_id="cmpol_compression_pg"
                        ),
                    ]
                )
                session.add(
                    RunModel(
                        id="run_compression_pg",
                        session_id="ses_compression_pg",
                        owner_id="usr_compression_pg",
                        status="running",
                        trigger_message_id="msg_compression_trigger",
                        context_cutoff_sequence=2,
                        retry_of_run_id=None,
                        model_config_version_id="llmprof_compression_pg",
                        compression_policy_version_id="cmpol_compression_pg",
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=1,
                        worker_id="worker_compression_pg",
                        lease_expires_at=now + timedelta(minutes=2),
                        execution_started_at=now,
                        execution_deadline_at=now + timedelta(minutes=2),
                        queued_at=now,
                        first_started_at=now,
                        finished_at=None,
                        created_at=now,
                        updated_at=now,
                        cancel_requested_at=None,
                        safe_error_code=None,
                        usage={},
                    )
                )

            repository = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
            submitted = await repository.submit(
                "ses_snapshot_pg",
                "usr_compression_pg",
                SubmitRunCommand(
                    run_id="run_snapshot_pg",
                    request_id="req_snapshot_pg",
                    fingerprint="a" * 64,
                    prompt_template_version="run-v2",
                    message=ConversationMessageDraft(
                        id="msg_snapshot_pg",
                        session_id="ses_snapshot_pg",
                        owner_id="usr_compression_pg",
                        role=ConversationRole.USER,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("snapshot"),),
                        reply_to_message_id=None,
                        run_id="run_snapshot_pg",
                        client_message_id=None,
                        created_at=now,
                        completed_at=now,
                        metadata=MessageMetadata(),
                    ),
                    queued_at=now,
                ),
            )
            assert submitted.run.compression_policy_version_id == "cmpol_compression_pg"
            command_value = SaveContextSummaryCommand(
                run_id="run_compression_pg",
                worker_id="worker_compression_pg",
                execution_epoch=1,
                operation_id="cmpop_compression_pg",
                summary_id="csum_compression_pg",
                covered_through_sequence=1,
                parent_summary_id=None,
                text="verified history summary",
                policy_version_id="cmpol_compression_pg",
                compression_model_version_id="llmprof_compression_pg",
                prompt_version="context-summary-v1",
                created_at=now,
                batch_count=1,
            )
            first = await repository.save_context_summary(command_value)
            second = await repository.save_context_summary(command_value)
            assert first is not None and second is not None
            assert first == second
            assert isinstance(first[1].payload, RunCompressionCompletedPayload)
            async with factory() as session:
                assert (
                    await session.scalar(
                        select(SessionContextSummaryModel.id).where(
                            SessionContextSummaryModel.run_id == "run_compression_pg"
                        )
                    )
                    == "csum_compression_pg"
                )
                assert (
                    await session.scalar(
                        select(RunEventModel.id).where(
                            RunEventModel.run_id == "run_compression_pg",
                            RunEventModel.type == "compression_completed",
                        )
                    )
                    is not None
                )
                run_row = await session.get(RunModel, "run_compression_pg")
                assert run_row is not None
                run_row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
            expired_lease = await repository.save_context_summary(
                replace(
                    command_value,
                    operation_id="cmpop_expired_lease",
                    summary_id="csum_expired_lease",
                )
            )
            assert expired_lease is None
            async with factory() as session:
                run_row = await session.get(RunModel, "run_compression_pg")
                assert run_row is not None
                run_row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
                run_row.execution_epoch = 2
                await session.commit()
            with pytest.raises(RunStateError, match="run_execution_lease_lost"):
                await repository.save_context_summary(
                    replace(
                        command_value,
                        operation_id="cmpop_stale_epoch",
                        summary_id="csum_stale_epoch",
                    )
                )
            async with factory() as session:
                run_row = await session.get(RunModel, "run_compression_pg")
                assert run_row is not None
                run_row.execution_epoch = 1
                run_row.cancel_requested_at = datetime.now(UTC)
                await session.commit()
            rejected = await repository.save_context_summary(
                replace(
                    command_value,
                    operation_id="cmpop_after_cancel",
                    summary_id="csum_after_cancel",
                )
            )
            assert rejected is None
            assert not await repository.is_live_execution("run_compression_pg", 1)
            rejected_usage = await repository.record_usage(
                RecordRunUsageCommand(
                    run_id="run_compression_pg",
                    worker_id="worker_compression_pg",
                    execution_epoch=1,
                    attempt=1,
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                    complete=True,
                    recorded_at=datetime.now(UTC),
                    purpose="compression",
                    operation_id="cmpop_after_cancel",
                    batch_index=0,
                    model_profile_id="llmprof_compression_pg",
                    duration_ms=1,
                )
            )
            assert rejected_usage is None
            # 升级前创建的消息没有 context_turn_id；重试 Run 关联必须仍恢复原触发轮次。
            async with factory() as session, session.begin():
                original = await session.get(RunModel, "run_compression_pg")
                assert original is not None
                original.status = "failed"
                original.cancel_requested_at = None
                original.finished_at = datetime.now(UTC)
                original.worker_id = None
                original.lease_expires_at = None
                trigger = await session.get(ConversationMessageModel, "msg_compression_trigger")
                assert trigger is not None
                trigger.context_turn_id = None
                session.add(
                    ConversationMessageModel(
                        id="msg_legacy_retry_answer",
                        session_id="ses_compression_pg",
                        role="user",
                        status="completed",
                        sequence=3,
                        parts=[{"type": "text", "text": "clarification answer"}],
                        reply_to_message_id=None,
                        run_id="run_legacy_retry_pg",
                        managed_run_id="run_legacy_retry_pg",
                        context_turn_id=None,
                        client_message_id=None,
                        created_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                        metadata_json={},
                    )
                )
                session.add(
                    RunModel(
                        id="run_legacy_retry_pg",
                        session_id="ses_compression_pg",
                        owner_id="usr_compression_pg",
                        status="running",
                        trigger_message_id="msg_compression_trigger",
                        context_cutoff_sequence=3,
                        retry_of_run_id="run_compression_pg",
                        model_config_version_id="llmprof_main_pg",
                        compression_policy_version_id="cmpol_compression_pg",
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=1,
                        worker_id="worker_legacy_retry_pg",
                        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
                        execution_started_at=datetime.now(UTC),
                        execution_deadline_at=datetime.now(UTC) + timedelta(minutes=2),
                        queued_at=datetime.now(UTC),
                        first_started_at=datetime.now(UTC),
                        finished_at=None,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                        cancel_requested_at=None,
                        safe_error_code=None,
                        usage={},
                    )
                )
                session_row = await session.get(SessionModel, "ses_compression_pg")
                assert session_row is not None
                session_row.last_message_sequence = 3
            legacy_retry_messages = await repository.list_context_messages(
                "ses_compression_pg", "usr_compression_pg", "run_legacy_retry_pg"
            )
            assert legacy_retry_messages is not None
            assert [
                [item.sequence for item in group]
                for group in _turn_groups(
                    tuple(
                        item
                        for item in legacy_retry_messages
                        if isinstance(item, ConversationMessage)
                    )
                )
            ] == [[1], [2, 3]]
        finally:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await engine.dispose()
            await admin_engine.dispose()

    asyncio.run(scenario())
