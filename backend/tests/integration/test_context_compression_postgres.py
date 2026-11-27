"""上下文摘要 PostgreSQL 事务、幂等与失租验证。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from integration.llm_fixtures import encrypted_llm_key_fields
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from adapter.auth.model import UserModel
from adapter.llm.model import (
    CompressionPolicyVersionModel,
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.llm.repository import SqlAlchemyModelProfileRepository
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
    ContextStopReason,
    FailRunCommand,
    RecordRunUsageCommand,
    RunAttemptUsage,
    RunCompressionCompletedPayload,
    RunFailedPayload,
    RunStatus,
    RunUsage,
    SaveContextSummaryCommand,
    SubmitRunCommand,
)
from domain.run.model.errors import RunStateError
from domain.system_prompt import INITIAL_SYSTEM_PROMPT_IDS
from infrastructure.security.secrets import SecretCipher

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_MODEL_SNAPSHOT: dict[str, object] = {
    "id": "llmprof_main_pg",
    "name": "main test",
    "protocol": "chat_completions",
    "base_url": "https://llm.example.test/v1",
    "model": "main-model",
    "context_window_tokens": 2048,
    "tokenizer_encoding": "o200k_base",
    "supports_stream": True,
    "supports_tools": True,
    "supports_parallel_tool_calls": False,
    "max_output_tokens": 128,
    "request_timeout_seconds": 10,
    "execution_timeout_seconds": 30,
    "max_concurrency": 1,
    "temperature": None,
    "scenario_label": None,
    "supports_vision": False,
    "version": 1,
}
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
        async with isolated_postgres_schema(
            database_url, prefix="athena_context_compression"
        ) as db:
            factory = db.session_factory
            now = datetime.now(UTC)
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="usr_compression_pg",
                        email="compression-pg@example.test",
                        password_hash="unused",
                        display_name=None,
                        status="active",
                        role="member",
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
                            **encrypted_llm_key_fields("llmprof_compression_pg"),
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
                            updated_at=now,
                        ),
                        ModelProfileVersionModel(
                            id="llmprof_main_pg",
                            name="main test",
                            protocol="chat_completions",
                            base_url="https://llm.example.test/v1",
                            model="main-model",
                            **encrypted_llm_key_fields("llmprof_main_pg"),
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
                            updated_at=now,
                        ),
                        ConversationMessageModel(
                            id="msg_compression_history",
                            session_id="ses_compression_pg",
                            role="user",
                            status="completed",
                            sequence=1,
                            parts=[{"type": "text", "text": "history"}],
                            reply_to_message_id=None,
                            run_id="run_compression_history",
                            context_turn_id="msg_compression_history",
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
                            context_turn_id="msg_compression_trigger",
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
                        trigger_tokens=262_144,
                        recent_ratio=0.2,
                        recent_token_cap=32_768,
                        summary_ratio=0.1,
                        summary_token_cap=8_192,
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
                session.add_all(
                    [
                        RunModel(
                            id="run_compression_history",
                            session_id="ses_compression_pg",
                            owner_id="usr_compression_pg",
                            status="completed",
                            trigger_message_id="msg_compression_history",
                            context_cutoff_sequence=1,
                            retry_of_run_id=None,
                            model_config_version_id="llmprof_main_pg",
                            model_snapshot_json=dict(_MODEL_SNAPSHOT),
                            compression_policy_version_id=None,
                            main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                            compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
                            prompt_template_version="run-v2",
                            event_sequence=0,
                            execution_epoch=0,
                            queued_at=now,
                            created_at=now,
                            updated_at=now,
                            finished_at=now,
                            usage={},
                            depth=0,
                        ),
                        RunModel(
                            id="run_compression_pg",
                            session_id="ses_compression_pg",
                            owner_id="usr_compression_pg",
                            status="running",
                            trigger_message_id="msg_compression_trigger",
                            context_cutoff_sequence=2,
                            retry_of_run_id=None,
                            model_config_version_id="llmprof_compression_pg",
                            model_snapshot_json=dict(_MODEL_SNAPSHOT),
                            compression_policy_version_id="cmpol_compression_pg",
                            main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                            compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
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
                        ),
                    ]
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
                        context_turn_id="msg_snapshot_pg",
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
                created_at=now,
                batch_count=1,
            )
            first = await repository.save_context_summary(command_value)
            second = await repository.save_context_summary(command_value)
            assert first is not None and second is not None
            assert first == second
            assert (
                first[0].compression_prompt_version_id == INITIAL_SYSTEM_PROMPT_IDS["compression"]
            )
            assert isinstance(first[1].payload, RunCompressionCompletedPayload)
            async with factory() as session:
                summary_row = await session.scalar(
                    select(SessionContextSummaryModel).where(
                        SessionContextSummaryModel.run_id == "run_compression_pg"
                    )
                )
                assert summary_row is not None
                assert summary_row.id == "csum_compression_pg"
                assert (
                    summary_row.compression_prompt_version_id
                    == INITIAL_SYSTEM_PROMPT_IDS["compression"]
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
            # 重试 Run 的澄清回答复用原触发消息的 context_turn_id，保持同轮次分组不拆开。
            async with factory() as session, session.begin():
                original = await session.get(RunModel, "run_compression_pg")
                assert original is not None
                original.status = "failed"
                original.cancel_requested_at = None
                original.finished_at = datetime.now(UTC)
                original.worker_id = None
                original.lease_expires_at = None
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
                        context_turn_id="msg_compression_trigger",
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
                        model_snapshot_json=dict(_MODEL_SNAPSHOT),
                        compression_policy_version_id="cmpol_compression_pg",
                        main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                        compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
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

    asyncio.run(scenario())


def test_context_report_and_failed_state_are_atomic_idempotent_and_cancel_aware() -> None:
    """真实 PostgreSQL 下验证报告、FAILED、usage、事件和取消优先的原子语义。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is required")

    async def scenario() -> None:
        async with isolated_postgres_schema(database_url, prefix="athena_context_report") as db:
            factory = db.session_factory
            now = datetime.now(UTC)
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="usr_context_report",
                        email="context-report@example.test",
                        password_hash="unused",
                        display_name=None,
                        status="active",
                        role="member",
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                session.add(
                    ModelProfileVersionModel(
                        id="llmprof_context_report",
                        name="context report",
                        protocol="chat_completions",
                        base_url="https://llm.example.test/v1",
                        model="report-model",
                        **encrypted_llm_key_fields("llmprof_context_report"),
                        context_window_tokens=8192,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        max_output_tokens=256,
                        request_timeout_seconds=10,
                        execution_timeout_seconds=30,
                        max_concurrency=1,
                        temperature=None,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    SessionModel(
                        id="ses_context_report",
                        owner_id="usr_context_report",
                        title=None,
                        project_id=None,
                        last_message_sequence=1,
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                await session.flush()
                session.add(
                    ConversationMessageModel(
                        id="msg_context_report_trigger",
                        session_id="ses_context_report",
                        role="user",
                        status="completed",
                        sequence=1,
                        parts=[{"type": "text", "text": "trigger"}],
                        reply_to_message_id=None,
                        run_id="run_context_report",
                        context_turn_id="msg_context_report_trigger",
                        client_message_id=None,
                        created_at=now,
                        completed_at=now,
                        metadata_json={},
                    )
                )
                session.add(
                    RunModel(
                        id="run_context_report",
                        session_id="ses_context_report",
                        owner_id="usr_context_report",
                        status="running",
                        trigger_message_id="msg_context_report_trigger",
                        context_cutoff_sequence=1,
                        retry_of_run_id=None,
                        model_config_version_id="llmprof_context_report",
                        model_snapshot_json=dict(_MODEL_SNAPSHOT),
                        compression_policy_version_id=None,
                        main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                        compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=1,
                        worker_id="worker_context_report",
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
            failed_at = now + timedelta(seconds=1)
            usage_attempt = RunAttemptUsage(
                execution_epoch=1,
                attempt=1,
                purpose="answer",
                operation_id="reportop_atomic",
                batch_index=0,
                model_profile_id="llmprof_context_report",
                duration_ms=5,
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                complete=True,
            )
            report = ConversationMessageDraft(
                id="msg_context_report_result",
                session_id="ses_context_report",
                owner_id="usr_context_report",
                role=ConversationRole.ASSISTANT,
                status=MessageStatus.COMPLETED,
                parts=(TextPart("上下文安全停止。任务未标记为完成。"),),
                reply_to_message_id="msg_context_report_trigger",
                run_id="run_context_report",
                client_message_id=None,
                created_at=failed_at,
                completed_at=failed_at,
                metadata=MessageMetadata(provider="system_context_safety"),
                context_turn_id="msg_context_report_trigger",
            )
            fail_command = FailRunCommand(
                run_id="run_context_report",
                worker_id="worker_context_report",
                execution_epoch=1,
                safe_error_code="run_context_safety_stop",
                failed_at=failed_at,
                report_message=report,
                usage=RunUsage(attempts=(usage_attempt,)),
                context_stop_reason=ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )
            barrier = asyncio.Barrier(3)

            async def replay() -> object:
                await barrier.wait()
                return await repository.fail(fail_command)

            tasks = [asyncio.create_task(replay()) for _ in range(2)]
            await barrier.wait()
            results = await asyncio.gather(*tasks)
            assert all(item is not None for item in results)
            assert results[0] == results[1]

            async with factory() as session:
                run_row = await session.get(RunModel, "run_context_report")
                assert run_row is not None
                assert run_row.status == RunStatus.FAILED.value
                assert run_row.safe_error_code == "run_context_safety_stop"
                assert run_row.report_message_id == report.id
                assert run_row.usage["total_tokens"] == 120
                persisted_report = await session.get(ConversationMessageModel, report.id)
                assert persisted_report is not None
                assert persisted_report.sequence > 1
                failed_events = list(
                    (
                        await session.scalars(
                            select(RunEventModel).where(
                                RunEventModel.run_id == run_row.id,
                                RunEventModel.type == "failed",
                            )
                        )
                    ).all()
                )
                assert len(failed_events) == 1
                failed_payload = RunFailedPayload.model_validate(failed_events[0].payload)
                assert failed_payload.report_message_id == report.id
                assert (
                    failed_payload.context_stop_reason is ContextStopReason.NO_COMPRESSIBLE_HISTORY
                )

            async with factory() as session, session.begin():
                session.add(
                    ConversationMessageModel(
                        id="msg_context_report_cancel_trigger",
                        session_id="ses_context_report",
                        role="user",
                        status="completed",
                        sequence=3,
                        parts=[{"type": "text", "text": "cancel race"}],
                        reply_to_message_id=None,
                        run_id="run_context_report_cancel",
                        context_turn_id="msg_context_report_cancel_trigger",
                        client_message_id=None,
                        created_at=now + timedelta(seconds=2),
                        completed_at=now + timedelta(seconds=2),
                        metadata_json={},
                    )
                )
                session_row = await session.get(SessionModel, "ses_context_report")
                assert session_row is not None
                session_row.last_message_sequence = 3
                session.add(
                    RunModel(
                        id="run_context_report_cancel",
                        session_id="ses_context_report",
                        owner_id="usr_context_report",
                        status="running",
                        trigger_message_id="msg_context_report_cancel_trigger",
                        context_cutoff_sequence=3,
                        retry_of_run_id=None,
                        model_config_version_id="llmprof_context_report",
                        model_snapshot_json=dict(_MODEL_SNAPSHOT),
                        compression_policy_version_id=None,
                        main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                        compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
                        prompt_template_version="run-v2",
                        event_sequence=0,
                        execution_epoch=1,
                        worker_id="worker_context_report_cancel",
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

            race_report = replace(
                report,
                id="msg_context_report_cancel_result",
                reply_to_message_id="msg_context_report_cancel_trigger",
                run_id="run_context_report_cancel",
                created_at=now + timedelta(seconds=3),
                completed_at=now + timedelta(seconds=3),
                context_turn_id="msg_context_report_cancel_trigger",
            )
            cancel_command = FailRunCommand(
                run_id="run_context_report_cancel",
                worker_id="worker_context_report_cancel",
                execution_epoch=1,
                safe_error_code="run_context_safety_stop",
                failed_at=now + timedelta(seconds=3),
                report_message=race_report,
                usage=RunUsage(),
                context_stop_reason=ContextStopReason.NO_COMPRESSIBLE_HISTORY,
            )
            race_barrier = asyncio.Barrier(3)

            async def cancel_run() -> object:
                await race_barrier.wait()
                return await repository.cancel(
                    "ses_context_report",
                    "usr_context_report",
                    "run_context_report_cancel",
                    now + timedelta(seconds=3),
                )

            async def fail_run() -> object:
                await race_barrier.wait()
                return await repository.fail(cancel_command)

            race_tasks = [asyncio.create_task(cancel_run()), asyncio.create_task(fail_run())]
            await race_barrier.wait()
            await asyncio.gather(*race_tasks)

            async with factory() as session:
                race_row = await session.get(RunModel, "run_context_report_cancel")
                assert race_row is not None
                race_report_row = await session.get(ConversationMessageModel, race_report.id)
                if race_row.status == RunStatus.FAILED.value:
                    assert race_report_row is not None
                    assert race_row.report_message_id == race_report.id
                else:
                    assert race_row.cancel_requested_at is not None
                    assert race_report_row is None
                    assert race_row.report_message_id is None

    asyncio.run(scenario())


def test_compression_policy_enforces_non_null_token_caps_and_omits_legacy_columns() -> None:
    """基线 Schema 强制压缩策略 token 上限非空，并已移除 trigger_ratio 与模板版本旧列。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is required")

    async def scenario() -> None:
        async with isolated_postgres_schema(
            database_url,
            prefix="athena_context_policy_strict",
        ) as db:
            engine = db.engine
            factory = db.session_factory
            now = datetime.now(UTC)
            async with engine.connect() as connection:
                columns = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() "
                                "AND table_name = 'llm_compression_policy_versions'"
                            )
                        )
                    ).all()
                )
            assert "trigger_ratio" not in columns
            assert "prompt_template_version" not in columns
            assert {"trigger_tokens", "recent_token_cap", "summary_token_cap"}.issubset(columns)

            async with factory() as session, session.begin():
                session.add(
                    ModelProfileVersionModel(
                        id="llmprof_policy_strict",
                        name="strict model",
                        protocol="chat_completions",
                        base_url="https://llm.example.test/v1",
                        model="strict-model",
                        **encrypted_llm_key_fields("llmprof_policy_strict"),
                        context_window_tokens=8192,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        max_output_tokens=256,
                        request_timeout_seconds=10,
                        execution_timeout_seconds=30,
                        max_concurrency=1,
                        temperature=None,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )

            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO llm_compression_policy_versions (
                                id, compression_model_profile_id, trigger_tokens,
                                recent_ratio, recent_token_cap, summary_ratio,
                                summary_token_cap, created_at
                            ) VALUES (
                                'cmpol_null_cap', 'llmprof_policy_strict', NULL,
                                0.2, 1024, 0.1, 512, :now
                            )
                            """
                        ),
                        {"now": now},
                    )

            repository = SqlAlchemyModelProfileRepository(
                factory,
                SecretCipher(bytes(range(32))),
            )
            assert await repository.get_current_compression_policy() is None

    asyncio.run(scenario())


def test_failed_report_insert_rolls_back_run_usage_and_events() -> None:
    """报告消息写入失败时，Run 保持 running，usage 和 failed 事件均无部分提交。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is required")

    async def scenario() -> None:
        async with isolated_postgres_schema(
            database_url, prefix="athena_context_report_rollback"
        ) as db:
            factory = db.session_factory
            now = datetime.now(UTC)
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="usr_report_rollback",
                        email="report-rollback@example.test",
                        password_hash="unused",
                        display_name=None,
                        status="active",
                        role="member",
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                session.add(
                    ModelProfileVersionModel(
                        id="llmprof_report_rollback",
                        name="report rollback",
                        protocol="chat_completions",
                        base_url="https://llm.example.test/v1",
                        model="report-model",
                        **encrypted_llm_key_fields("llmprof_report_rollback"),
                        context_window_tokens=8192,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        max_output_tokens=256,
                        request_timeout_seconds=10,
                        execution_timeout_seconds=30,
                        max_concurrency=1,
                        temperature=None,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    SessionModel(
                        id="ses_report_rollback",
                        owner_id="usr_report_rollback",
                        title=None,
                        project_id=None,
                        last_message_sequence=2,
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        ConversationMessageModel(
                            id="msg_report_rollback_trigger",
                            session_id="ses_report_rollback",
                            role="user",
                            status="completed",
                            sequence=1,
                            parts=[{"type": "text", "text": "trigger"}],
                            reply_to_message_id=None,
                            run_id="run_report_rollback",
                            context_turn_id="msg_report_rollback_trigger",
                            client_message_id=None,
                            created_at=now,
                            completed_at=now,
                            metadata_json={},
                        ),
                        ConversationMessageModel(
                            id="msg_report_rollback_duplicate",
                            session_id="ses_report_rollback",
                            role="assistant",
                            status="completed",
                            sequence=2,
                            parts=[{"type": "text", "text": "already present"}],
                            reply_to_message_id="msg_report_rollback_trigger",
                            run_id="run_report_rollback",
                            context_turn_id="msg_report_rollback_trigger",
                            client_message_id=None,
                            created_at=now,
                            completed_at=now,
                            metadata_json={},
                        ),
                        RunModel(
                            id="run_report_rollback",
                            session_id="ses_report_rollback",
                            owner_id="usr_report_rollback",
                            status="running",
                            trigger_message_id="msg_report_rollback_trigger",
                            context_cutoff_sequence=1,
                            retry_of_run_id=None,
                            model_config_version_id="llmprof_report_rollback",
                            model_snapshot_json=dict(_MODEL_SNAPSHOT),
                            compression_policy_version_id=None,
                            main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                            compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
                            prompt_template_version="run-v2",
                            event_sequence=0,
                            execution_epoch=1,
                            worker_id="worker_report_rollback",
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
                        ),
                    ]
                )

            repository = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
            report = ConversationMessageDraft(
                id="msg_report_rollback_duplicate",
                session_id="ses_report_rollback",
                owner_id="usr_report_rollback",
                role=ConversationRole.ASSISTANT,
                status=MessageStatus.COMPLETED,
                parts=(TextPart("context safety report"),),
                reply_to_message_id="msg_report_rollback_trigger",
                run_id="run_report_rollback",
                client_message_id=None,
                created_at=now + timedelta(seconds=1),
                completed_at=now + timedelta(seconds=1),
                metadata=MessageMetadata(provider="system_context_safety"),
                context_turn_id="msg_report_rollback_trigger",
            )
            usage_attempt = RunAttemptUsage(
                execution_epoch=1,
                attempt=1,
                purpose="answer",
                operation_id="reportop_rollback",
                batch_index=0,
                model_profile_id="llmprof_report_rollback",
                duration_ms=5,
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                complete=True,
            )
            with pytest.raises(IntegrityError):
                await repository.fail(
                    FailRunCommand(
                        run_id="run_report_rollback",
                        worker_id="worker_report_rollback",
                        execution_epoch=1,
                        safe_error_code="run_context_safety_stop",
                        failed_at=now + timedelta(seconds=1),
                        report_message=report,
                        usage=RunUsage(attempts=(usage_attempt,)),
                        context_stop_reason=(ContextStopReason.NO_COMPRESSIBLE_HISTORY),
                    )
                )

            async with factory() as session:
                run_row = await session.get(RunModel, "run_report_rollback")
                session_row = await session.get(SessionModel, "ses_report_rollback")
                assert run_row is not None
                assert session_row is not None
                assert run_row.status == RunStatus.RUNNING.value
                assert run_row.report_message_id is None
                assert run_row.usage == {}
                assert session_row.last_message_sequence == 2
                failed_event_count = await session.scalar(
                    select(RunEventModel)
                    .where(
                        RunEventModel.run_id == run_row.id,
                        RunEventModel.type == "failed",
                    )
                    .with_only_columns(RunEventModel.id)
                )
                assert failed_event_count is None

    asyncio.run(scenario())
