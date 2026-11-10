"""Run 工具快照、额度、调用终态和项目知识库上限的真实 PostgreSQL 测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.knowledge.model import KnowledgeBaseModel
from adapter.knowledge.repository import KnowledgeRepository
from adapter.llm.model.profile import DefaultModelProfileModel, ModelProfileVersionModel
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.project.model import ProjectModel
from adapter.run.model import RunEventModel, RunModel, RunRequestModel, RunToolCallModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from domain.knowledge import ProjectKnowledgeLimitReachedError
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessage,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from domain.run.model import (
    FailRunCommand,
    FinishRunCommand,
    Run,
    RunLease,
    RunRequestOperation,
    RunRequestResult,
    RunStatus,
    RunUsage,
    SubmitRunCommand,
)
from domain.run.model.errors import (
    RunConflictError,
    RunStateError,
    ToolRetryConfirmationRequiredError,
)
from domain.tool import (
    AdmitToolCallCommand,
    BeginToolCallBatchCommand,
    BeginToolCallBatchItem,
    BeginToolCallCommand,
    BuiltinToolSource,
    DecideToolApprovalCommand,
    FinishToolCallCommand,
    InitializeRunToolsCommand,
    ReserveLlmRequestCommand,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    RunToolCallRecord,
    ToolApprovalDecision,
    ToolCallStatus,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
    protocol_status_for_outcome,
)

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "backend" / "alembic.ini"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="ATHENA_TEST_DATABASE_URL is not configured"),
]


@asynccontextmanager
async def _database() -> AsyncIterator[
    tuple[
        async_sessionmaker[AsyncSession],
        object,
        object,
        str,
    ]
]:
    """Create one isolated PostgreSQL schema and apply the real migration chain."""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"athena_v2_tool_test_{token_hex(8)}"
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
        config.attributes["connect_args"] = {"server_settings": {"search_path": f"{schema},public"}}
        config.attributes["version_table_schema"] = schema
        await asyncio.to_thread(command.upgrade, config, "head")
        yield async_sessionmaker(engine, expire_on_commit=False), engine, admin_engine, schema
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _seed_owner_and_profile(
    factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[str, str]:
    now = datetime.now(UTC)
    user_id = f"usr_tool_{suffix}"
    profile_id = f"llmprof_tool_{suffix}"
    async with factory() as session, session.begin():
        session.add(
            UserModel(
                id=user_id,
                email=f"tool-{suffix}@example.test",
                password_hash="unused",
                display_name=None,
                status="active",
                role="user",
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        session.add(
            ModelProfileVersionModel(
                id=profile_id,
                name="Tool integration profile",
                protocol="chat_completions",
                base_url="https://llm.example.test/v1",
                model="tool-integration-model",
                credential_env="ATHENA_LLM_CREDENTIAL_TOOL_TEST",
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
        session.add(DefaultModelProfileModel(singleton_id=1, profile_id=profile_id))
    return user_id, profile_id


def _snapshot(description: str = "first") -> RuntimeToolSnapshot:
    return RuntimeToolSnapshot(
        tools=(
            RuntimeToolDefinition(
                tool_id="builtin_safe_tool",
                model_name="safe_tool",
                description=description,
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                source=BuiltinToolSource(version=1),
            ),
        )
    )


async def _running_run(
    repository: SqlAlchemyRunRepository,
    factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: str,
    suffix: str,
) -> RunLease:
    """Submit and claim one run in a fresh visible session."""
    now = datetime.now(UTC)
    session_id = f"ses_tool_{suffix}"
    run_id = f"run_tool_{suffix}"
    message_id = f"msg_tool_{suffix}"
    async with factory() as session, session.begin():
        session.add(
            SessionModel(
                id=session_id,
                owner_id=owner_id,
                title=None,
                project_id=None,
                last_message_sequence=0,
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
    await repository.submit(
        session_id,
        owner_id,
        SubmitRunCommand(
            run_id=run_id,
            request_id=f"submit_{suffix}",
            fingerprint="a" * 64,
            prompt_template_version="run-v2",
            message=ConversationMessageDraft(
                id=message_id,
                session_id=session_id,
                owner_id=owner_id,
                role=ConversationRole.USER,
                status=MessageStatus.COMPLETED,
                parts=(TextPart("perform the tool task"),),
                reply_to_message_id=None,
                run_id=run_id,
                client_message_id=None,
                created_at=now,
                completed_at=now,
                metadata=MessageMetadata(),
            ),
            queued_at=now,
            auth_session_id=f"auth_{suffix}",
        ),
    )
    leases = await repository.claim(
        f"worker_{suffix}",
        lease_seconds=30,
        execution_seconds=60,
        limit=1,
    )
    assert len(leases) == 1
    return leases[0]


def _begin_command(
    lease: RunLease,
    *,
    suffix: str,
    tool_call_id: str,
    ordinal_label: str = "",
) -> BeginToolCallCommand:
    now = datetime.now(UTC)
    run = lease.run
    return BeginToolCallCommand(
        run_id=run.id,
        worker_id=run.worker_id or "",
        execution_epoch=run.execution_epoch,
        record_id=f"record_{suffix}_{ordinal_label}",
        tool_call_id=tool_call_id,
        model_name="safe_tool",
        tool_id="builtin_safe_tool",
        tool_kind=ToolKind.BUILTIN,
        call_message=ExecutionMessageDraft(
            id=f"exec_call_{suffix}_{ordinal_label}",
            session_id=run.session_id,
            owner_id=run.owner_id,
            run_id=run.id,
            thread_id=run.id,
            role=ExecutionRole.ASSISTANT,
            parts=(
                ToolCallPart(
                    tool_call_id,
                    "safe_tool",
                    {"query": f"value-{suffix}-{ordinal_label}"},
                ),
            ),
            tool_call_id=None,
            source_message_id=None,
            created_at=now,
            metadata=MessageMetadata(provider="chat_completions", model="tool-integration-model"),
        ),
        started_at=now,
        round=1,
    )


def _finish_command(
    lease: RunLease,
    begun: RunToolCallRecord,
    *,
    suffix: str,
    outcome: ToolOutcome | None = None,
    ordinal_label: str = "",
) -> FinishToolCallCommand:
    record_id = begun.id
    tool_call_id = begun.tool_call_id
    selected = outcome or ToolOutcome(
        status=ToolOutcomeStatus.OK,
        code="tool_completed",
        message="completed",
        data={"value": f"{suffix}-{ordinal_label}"},
    )
    now = datetime.now(UTC)
    run = lease.run
    return FinishToolCallCommand(
        run_id=run.id,
        worker_id=run.worker_id or "",
        execution_epoch=run.execution_epoch,
        record_id=record_id,
        result_message=ExecutionMessageDraft(
            id=f"exec_result_{suffix}_{ordinal_label}",
            session_id=run.session_id,
            owner_id=run.owner_id,
            run_id=run.id,
            thread_id=run.id,
            role=ExecutionRole.TOOL,
            parts=(
                ToolResultPart(
                    tool_call_id=tool_call_id,
                    status=protocol_status_for_outcome(selected),
                    output=selected.canonical_output(),
                ),
            ),
            tool_call_id=tool_call_id,
            source_message_id=None,
            created_at=now,
            metadata=MessageMetadata(),
        ),
        outcome=selected,
        finished_at=now,
    )


def _reply(run: Run, *, suffix: str) -> ConversationMessageDraft:
    now = datetime.now(UTC)
    return ConversationMessageDraft(
        id=f"reply_{suffix}",
        session_id=run.session_id,
        owner_id=run.owner_id,
        role=ConversationRole.ASSISTANT,
        status=MessageStatus.COMPLETED,
        parts=(TextPart("final answer"),),
        reply_to_message_id=run.trigger_message_id,
        run_id=run.id,
        client_message_id=None,
        created_at=now,
        completed_at=now,
        metadata=MessageMetadata(),
    )


async def _assert_call_invariants(
    factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    expected_unknown: int,
) -> None:
    async with factory() as session:
        records = (
            await session.scalars(
                select(RunToolCallModel)
                .where(RunToolCallModel.run_id == run_id)
                .order_by(RunToolCallModel.ordinal)
            )
        ).all()
        assert records
        assert not any(record.status == ToolCallStatus.EXECUTING.value for record in records)
        assert all(record.result_message_id is not None for record in records)
        assert (
            sum(record.status == ToolCallStatus.UNKNOWN.value for record in records)
            == expected_unknown
        )
        for record in records:
            result = await session.get(ExecutionMessageModel, record.result_message_id)
            assert result is not None
            assert result.tool_call_id == record.tool_call_id
            assert any(
                part.get("type") == "tool_result"
                and part.get("tool_call_id") == record.tool_call_id
                for part in result.parts
            )


def test_tool_snapshot_llm_quota_begin_finish_and_query_are_atomic() -> None:
    """快照只冻结一次，额度与调用准入在并发下只提交一个赢家，查询按 ordinal 分页。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _profile_id = await _seed_owner_and_profile(factory, suffix="atomic")
            messages = SqlAlchemyMessageRepository(factory)
            repository = SqlAlchemyRunRepository(factory, messages)
            lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="atomic",
            )
            first_snapshot = _snapshot("first")
            second_snapshot = _snapshot("second")
            fixed = await repository.initialize_tools(
                InitializeRunToolsCommand(
                    lease.run.id,
                    lease.run.worker_id or "",
                    lease.run.execution_epoch,
                    first_snapshot,
                )
            )
            repeated = await repository.initialize_tools(
                InitializeRunToolsCommand(
                    lease.run.id,
                    lease.run.worker_id or "",
                    lease.run.execution_epoch,
                    second_snapshot,
                )
            )
            assert fixed == repeated == first_snapshot

            for expected in range(1, 40):
                reserved = await repository.reserve_llm_request(
                    ReserveLlmRequestCommand(
                        lease.run.id,
                        lease.run.worker_id or "",
                        lease.run.execution_epoch,
                        40,
                    )
                )
                assert reserved == expected
            last_slot = await asyncio.gather(
                repository.reserve_llm_request(
                    ReserveLlmRequestCommand(
                        lease.run.id,
                        lease.run.worker_id or "",
                        lease.run.execution_epoch,
                        40,
                    )
                ),
                repository.reserve_llm_request(
                    ReserveLlmRequestCommand(
                        lease.run.id,
                        lease.run.worker_id or "",
                        lease.run.execution_epoch,
                        40,
                    )
                ),
            )
            assert sorted(value for value in last_slot if value is not None) == [40]
            assert last_slot.count(None) == 1
            assert (
                await repository.reserve_llm_request(
                    ReserveLlmRequestCommand(
                        lease.run.id,
                        lease.run.worker_id or "",
                        lease.run.execution_epoch,
                        40,
                    )
                )
                is None
            )

            first_call = _begin_command(
                lease,
                suffix="atomic",
                tool_call_id="call_atomic_1",
                ordinal_label="1",
            )
            first_admissions = await asyncio.gather(
                repository.begin_tool_call(first_call),
                repository.begin_tool_call(first_call),
            )
            assert sorted(result.execute for result in first_admissions) == [False, True]
            begun = next(result.item for result in first_admissions if result.execute)
            first_finish = _finish_command(
                lease,
                begun,
                suffix="atomic",
                ordinal_label="1",
            )
            await repository.finish_tool_call(first_finish)
            repeated_finish = await repository.finish_tool_call(first_finish)
            assert repeated_finish is not None

            second_begin = _begin_command(
                lease,
                suffix="atomic",
                tool_call_id="call_atomic_2",
                ordinal_label="2",
            )
            second_admission = await repository.begin_tool_call(second_begin)
            second_finish = _finish_command(
                lease,
                second_admission.item,
                suffix="atomic",
                ordinal_label="2",
            )
            await repository.finish_tool_call(second_finish)

            first_page = await repository.list_tool_calls(
                lease.run.session_id,
                owner_id,
                lease.run.id,
                after_ordinal=0,
                limit=1,
            )
            second_page = await repository.list_tool_calls(
                lease.run.session_id,
                owner_id,
                lease.run.id,
                after_ordinal=1,
                limit=1,
            )
            assert first_page is not None and second_page is not None
            assert first_page.next_after_ordinal == 1
            assert second_page.items[0].tool_call_id == "call_atomic_2"
            details = await repository.get_tool_call(
                lease.run.session_id,
                owner_id,
                lease.run.id,
                first_page.items[0].id,
            )
            assert details is not None
            assert details.arguments == {"query": "value-atomic-1"}
            assert details.record.outcome is not None
            assert details.record.outcome.code == "tool_completed"
            assert (
                await repository.get_tool_call(
                    lease.run.session_id,
                    "other-owner",
                    lease.run.id,
                    first_page.items[0].id,
                )
                is None
            )

            async with factory() as session:
                run_row = await session.get(RunModel, lease.run.id)
                call_count = await session.scalar(
                    select(func.count())
                    .select_from(RunToolCallModel)
                    .where(RunToolCallModel.run_id == lease.run.id)
                )
                result_count = await session.scalar(
                    select(func.count())
                    .select_from(RunToolCallModel)
                    .where(
                        RunToolCallModel.run_id == lease.run.id,
                        RunToolCallModel.result_message_id.is_not(None),
                    )
                )
                finish_events = await session.scalar(
                    select(func.count())
                    .select_from(RunEventModel)
                    .where(
                        RunEventModel.run_id == lease.run.id,
                        RunEventModel.type == "tool_call_finished",
                    )
                )
            assert run_row is not None and run_row.llm_request_count == 40
            assert call_count == result_count == 2
            assert finish_events == 2

    asyncio.run(scenario())


def test_every_terminal_transaction_settles_executing_calls_and_old_epoch_cannot_write() -> None:
    """fail/finish/cancel/reclaim/unknown 均成对收敛，已有结果不变，旧代迟到写入被拒绝。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _profile_id = await _seed_owner_and_profile(factory, suffix="terminal")
            repository = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))

            failed_lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="failed",
            )
            settled_begin = _begin_command(
                failed_lease,
                suffix="failed",
                tool_call_id="call_failed_settled",
                ordinal_label="1",
            )
            settled = await repository.begin_tool_call(settled_begin)
            settled_finish = _finish_command(
                failed_lease,
                settled.item,
                suffix="failed",
                ordinal_label="1",
            )
            await repository.finish_tool_call(settled_finish)
            executing_begin = _begin_command(
                failed_lease,
                suffix="failed",
                tool_call_id="call_failed_executing",
                ordinal_label="2",
            )
            await repository.begin_tool_call(executing_begin)
            failed = await repository.fail(
                FailRunCommand(
                    failed_lease.run.id,
                    failed_lease.run.worker_id or "",
                    failed_lease.run.execution_epoch,
                    "execution_failed",
                    datetime.now(UTC),
                )
            )
            assert failed is not None and failed.status is RunStatus.FAILED
            await _assert_call_invariants(factory, failed_lease.run.id, expected_unknown=1)
            async with factory() as session:
                first_call = await session.scalar(
                    select(RunToolCallModel).where(
                        RunToolCallModel.run_id == failed_lease.run.id,
                        RunToolCallModel.tool_call_id == "call_failed_settled",
                    )
                )
            assert first_call is not None
            assert first_call.status == ToolCallStatus.COMPLETED.value
            assert first_call.outcome is not None
            assert first_call.outcome["status"] == ToolOutcomeStatus.OK.value

            finish_lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="finish",
            )
            finish_begin = _begin_command(
                finish_lease,
                suffix="finish",
                tool_call_id="call_finish_executing",
                ordinal_label="1",
            )
            await repository.begin_tool_call(finish_begin)
            finished = await repository.finish(
                FinishRunCommand(
                    finish_lease.run.id,
                    finish_lease.run.worker_id or "",
                    finish_lease.run.execution_epoch,
                    _reply(finish_lease.run, suffix="finish"),
                    RunUsage(),
                    datetime.now(UTC),
                )
            )
            assert finished is not None
            assert finished.status is RunStatus.FAILED
            assert finished.safe_error_code == "tool_outcome_unknown"
            await _assert_call_invariants(factory, finish_lease.run.id, expected_unknown=1)
            async with factory() as session:
                assistant_replies = (
                    await session.scalars(
                        select(ConversationMessageModel).where(
                            ConversationMessageModel.run_id == finish_lease.run.id,
                            ConversationMessageModel.role == "assistant",
                        )
                    )
                ).all()
            assert assistant_replies == []

            cancel_lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="cancel",
            )
            cancel_begin = _begin_command(
                cancel_lease,
                suffix="cancel",
                tool_call_id="call_cancel_executing",
                ordinal_label="1",
            )
            await repository.begin_tool_call(cancel_begin)
            requested = await repository.cancel(
                cancel_lease.run.session_id,
                owner_id,
                cancel_lease.run.id,
                datetime.now(UTC),
            )
            assert requested is not None and requested.status is RunStatus.CANCELLING
            cancelled = await repository.settle_cancelled(
                cancel_lease.run.id,
                cancel_lease.run.worker_id or "",
                cancel_lease.run.execution_epoch,
                datetime.now(UTC),
            )
            assert cancelled is not None and cancelled.status is RunStatus.CANCELLED
            await _assert_call_invariants(factory, cancel_lease.run.id, expected_unknown=1)

            reclaim_lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="reclaim",
            )
            reclaim_command = _begin_command(
                reclaim_lease,
                suffix="reclaim",
                tool_call_id="call_reclaim_executing",
                ordinal_label="1",
            )
            reclaimed_call = await repository.begin_tool_call(reclaim_command)
            async with factory() as session, session.begin():
                await session.execute(
                    update(RunModel)
                    .where(RunModel.id == reclaim_lease.run.id)
                    .values(lease_expires_at=func.clock_timestamp() - text("INTERVAL '1 second'"))
                )
            reclaimed = await repository.reclaim_expired(limit=10)
            reclaimed_run = next(item for item in reclaimed if item.id == reclaim_lease.run.id)
            assert reclaimed_run.status is RunStatus.FAILED
            await _assert_call_invariants(factory, reclaim_lease.run.id, expected_unknown=1)
            late_finish = _finish_command(
                reclaim_lease,
                reclaimed_call.item,
                suffix="reclaim",
                ordinal_label="late",
            )
            with pytest.raises(RunStateError, match="run_execution_lease_lost"):
                await repository.finish_tool_call(late_finish)
            await _assert_call_invariants(factory, reclaim_lease.run.id, expected_unknown=1)

            mismatch_lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="mismatch",
            )
            mismatch_begin = _begin_command(
                mismatch_lease,
                suffix="mismatch",
                tool_call_id="call_mismatch",
                ordinal_label="1",
            )
            mismatch_call = await repository.begin_tool_call(mismatch_begin)
            mismatch_finish = _finish_command(
                mismatch_lease,
                mismatch_call.item,
                suffix="mismatch",
                ordinal_label="1",
            )
            mismatch_part = ToolResultPart(
                tool_call_id="call_mismatch",
                status=protocol_status_for_outcome(mismatch_finish.outcome),
                output='{"status":"ok","code":"tampered","message":"","data":{},"truncated":false}',
            )
            mismatch_finish = replace(
                mismatch_finish,
                result_message=replace(
                    mismatch_finish.result_message,
                    parts=(mismatch_part,),
                ),
            )
            with pytest.raises(RunStateError, match="run_tool_result_message_invalid"):
                await repository.finish_tool_call(mismatch_finish)
            mismatch_failed = await repository.fail(
                FailRunCommand(
                    mismatch_lease.run.id,
                    mismatch_lease.run.worker_id or "",
                    mismatch_lease.run.execution_epoch,
                    "execution_failed",
                    datetime.now(UTC),
                )
            )
            assert mismatch_failed is not None
            await _assert_call_invariants(factory, mismatch_lease.run.id, expected_unknown=1)

            unknown_lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="unknown",
            )
            unknown_begin = _begin_command(
                unknown_lease,
                suffix="unknown",
                tool_call_id="call_unknown",
                ordinal_label="1",
            )
            unknown_call = await repository.begin_tool_call(unknown_begin)
            unknown_result = await repository.finish_tool_call(
                _finish_command(
                    unknown_lease,
                    unknown_call.item,
                    suffix="unknown",
                    ordinal_label="1",
                    outcome=ToolOutcome(
                        status=ToolOutcomeStatus.UNKNOWN,
                        code="tool_timeout",
                        message="unknown",
                    ),
                )
            )
            assert unknown_result is not None
            assert unknown_result.status is RunStatus.FAILED
            assert unknown_result.safe_error_code == "tool_outcome_unknown"
            await _assert_call_invariants(factory, unknown_lease.run.id, expected_unknown=1)

            with pytest.raises(ToolRetryConfirmationRequiredError):
                await repository.retry(
                    unknown_lease.run.session_id,
                    owner_id,
                    unknown_lease.run.id,
                    "retry_unconfirmed",
                    "b" * 64,
                    "run_retry_unconfirmed",
                    "run-v2",
                    datetime.now(UTC),
                )
            retried = await repository.retry(
                unknown_lease.run.session_id,
                owner_id,
                unknown_lease.run.id,
                "retry_confirmed",
                "c" * 64,
                "run_retry_confirmed",
                "run-v2",
                datetime.now(UTC),
                confirm_unknown=True,
                auth_session_id="auth_confirmed",
            )
            assert retried is not None
            assert retried.retry_of_run_id == unknown_lease.run.id
            assert retried.llm_request_count == 0
            await _assert_call_invariants(factory, unknown_lease.run.id, expected_unknown=1)

    asyncio.run(scenario())


def test_project_knowledge_limit_is_atomic_and_repeat_bind_is_idempotent() -> None:
    """项目最多 20 库；满额重复绑定成功，解绑释放名额，并发最后名额只成功一个。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            now = datetime.now(UTC)
            owner_id = "usr_knowledge_limit"
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id=owner_id,
                        email="knowledge-limit@example.test",
                        password_hash="unused",
                        display_name=None,
                        status="active",
                        role="user",
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                for project_id in ("prj_limit_one", "prj_limit_two"):
                    session.add(
                        ProjectModel(
                            id=project_id,
                            owner_id=owner_id,
                            name=project_id,
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                    )
                for index in range(21):
                    session.add(
                        KnowledgeBaseModel(
                            id=f"kbase_limit_{index:02d}",
                            owner_id=owner_id,
                            name=f"Knowledge {index:02d}",
                            description=None,
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                    )

            repository = KnowledgeRepository(factory)
            project_one = "prj_limit_one"
            project_two = "prj_limit_two"
            for index in range(20):
                assert await repository.bind_project_base(
                    owner_id,
                    project_one,
                    f"kbase_limit_{index:02d}",
                    now,
                )
            with pytest.raises(ProjectKnowledgeLimitReachedError):
                await repository.bind_project_base(
                    owner_id,
                    project_one,
                    "kbase_limit_20",
                    now,
                )
            assert await repository.bind_project_base(
                owner_id,
                project_one,
                "kbase_limit_00",
                now,
            )
            assert len(await repository.list_project_bases(owner_id, project_one)) == 20
            assert await repository.unbind_project_base(
                owner_id,
                project_one,
                "kbase_limit_00",
            )
            assert await repository.bind_project_base(
                owner_id,
                project_one,
                "kbase_limit_20",
                now,
            )
            assert len(await repository.list_project_bases(owner_id, project_one)) == 20

            for index in range(19):
                assert await repository.bind_project_base(
                    owner_id,
                    project_two,
                    f"kbase_limit_{index:02d}",
                    now,
                )
            race = await asyncio.gather(
                repository.bind_project_base(
                    owner_id,
                    project_two,
                    "kbase_limit_19",
                    now,
                ),
                repository.bind_project_base(
                    owner_id,
                    project_two,
                    "kbase_limit_20",
                    now,
                ),
                return_exceptions=True,
            )
            assert sum(result is True for result in race) == 1
            assert (
                sum(isinstance(result, ProjectKnowledgeLimitReachedError) for result in race) == 1
            )
            assert len(await repository.list_project_bases(owner_id, project_two)) == 20

    asyncio.run(scenario())


def _approval_snapshot(*, both_risky: bool = False) -> RuntimeToolSnapshot:
    return RuntimeToolSnapshot(
        tools=(
            RuntimeToolDefinition(
                tool_id="builtin_safe_tool",
                model_name="safe_tool",
                description="safe tool",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                source=BuiltinToolSource(version=1),
                requires_approval=both_risky,
            ),
            RuntimeToolDefinition(
                tool_id="builtin_risky_tool",
                model_name="risky_tool",
                description="risky tool",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                source=BuiltinToolSource(version=1),
                requires_approval=True,
            ),
        )
    )


def _batch_command(
    lease: RunLease,
    *,
    suffix: str,
    calls: tuple[tuple[str, str], ...],
) -> BeginToolCallBatchCommand:
    now = datetime.now(UTC)
    run = lease.run
    return BeginToolCallBatchCommand(
        run_id=run.id,
        worker_id=run.worker_id or "",
        execution_epoch=run.execution_epoch,
        call_message=ExecutionMessageDraft(
            id=f"exec_batch_{suffix}",
            session_id=run.session_id,
            owner_id=run.owner_id,
            run_id=run.id,
            thread_id=run.id,
            role=ExecutionRole.ASSISTANT,
            parts=tuple(
                ToolCallPart(call_id, name, {"query": f"{suffix}-{ordinal}"})
                for ordinal, (call_id, name) in enumerate(calls, start=1)
            ),
            tool_call_id=None,
            source_message_id=None,
            created_at=now,
            metadata=MessageMetadata(provider="chat_completions", model="tool-integration-model"),
        ),
        calls=tuple(
            BeginToolCallBatchItem(
                record_id=f"record_{suffix}_{ordinal}",
                tool_call_id=call_id,
                model_name=name,
                tool_id=(
                    "builtin_risky_tool" if name == "risky_tool" else "builtin_safe_tool"
                ),
                tool_kind=ToolKind.BUILTIN,
                requires_approval=name == "risky_tool",
            )
            for ordinal, (call_id, name) in enumerate(calls, start=1)
        ),
        requested_at=now,
        round=1,
    )


def _batch_finish(
    lease: RunLease,
    record: RunToolCallRecord,
    *,
    suffix: str,
    output: str,
) -> FinishToolCallCommand:
    now = datetime.now(UTC)
    outcome = ToolOutcome(
        status=ToolOutcomeStatus.OK,
        code="tool_completed",
        message="completed",
        data={"value": output},
    )
    return FinishToolCallCommand(
        run_id=lease.run.id,
        worker_id=lease.run.worker_id or "",
        execution_epoch=lease.run.execution_epoch,
        record_id=record.id,
        result_message=ExecutionMessageDraft(
            id=f"exec_result_{record.id}",
            session_id=lease.run.session_id,
            owner_id=lease.run.owner_id,
            run_id=lease.run.id,
            thread_id=lease.run.id,
            role=ExecutionRole.TOOL,
            parts=(
                ToolResultPart(
                    tool_call_id=record.tool_call_id,
                    status=protocol_status_for_outcome(outcome),
                    output=outcome.canonical_output(),
                ),
            ),
            tool_call_id=record.tool_call_id,
            source_message_id=None,
            created_at=now,
            metadata=MessageMetadata(),
        ),
        outcome=outcome,
        finished_at=now,
        defer_terminal_convergence=True,
    )


def test_tool_approval_batch_barrier_resume_and_context_order_are_atomic() -> None:
    """一个待审批调用建立整批屏障；最后决定只排队一次，恢复后结果稳定排序。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _ = await _seed_owner_and_profile(factory, suffix="approval")
            repository = SqlAlchemyRunRepository(
                factory, SqlAlchemyMessageRepository(factory)
            )
            lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="approval",
            )
            await repository.initialize_tools(
                InitializeRunToolsCommand(
                    lease.run.id,
                    lease.run.worker_id or "",
                    lease.run.execution_epoch,
                    _approval_snapshot(),
                )
            )
            begun = await repository.begin_tool_call_batch(
                _batch_command(
                    lease,
                    suffix="approval",
                    calls=(("call_safe", "safe_tool"), ("call_risky", "risky_tool")),
                )
            )
            assert begun.waiting_for_approval is True
            waiting = await repository.get(
                lease.run.session_id, owner_id, lease.run.id
            )
            assert waiting is not None and waiting.status is RunStatus.WAITING_FOR_APPROVAL
            assert [call.record.status for call in begun.batch.calls] == [
                ToolCallStatus.APPROVED,
                ToolCallStatus.PENDING_APPROVAL,
            ]
            pending = await repository.list_pending_approvals(
                lease.run.session_id, owner_id, lease.run.id
            )
            assert pending is not None and len(pending) == 1
            assert pending[0].call_id == begun.batch.calls[1].record.id

            first = DecideToolApprovalCommand(
                session_id=lease.run.session_id,
                owner_id=owner_id,
                run_id=lease.run.id,
                record_id=pending[0].call_id,
                request_id="approve-risky",
                fingerprint="1" * 64,
                decision=ToolApprovalDecision.APPROVE,
                decided_at=datetime.now(UTC),
                auth_session_id="fresh-auth",
            )
            partially_decided = await repository.decide_tool_approval(first)
            assert partially_decided is not None
            assert partially_decided.status is RunStatus.QUEUED
            assert partially_decided.auth_session_id == "fresh-auth"
            replay_request_id = "approve-risky-replay"
            replay = await repository.decide_tool_approval(
                replace(
                    first,
                    request_id=replay_request_id,
                    fingerprint="2" * 64,
                    decided_at=datetime.now(UTC),
                )
            )
            assert replay is not None and replay.status is RunStatus.QUEUED
            async with factory() as session:
                replay_request = await session.scalar(
                    select(RunRequestModel).where(
                        RunRequestModel.owner_id == owner_id,
                        RunRequestModel.session_id == lease.run.session_id,
                        RunRequestModel.operation
                        == RunRequestOperation.DECIDE_TOOL_APPROVAL.value,
                        RunRequestModel.request_id == replay_request_id,
                    )
                )
            assert replay_request is not None
            assert replay_request.fingerprint == "2" * 64
            assert (
                RunRequestResult.model_validate(replay_request.result).run_id
                == lease.run.id
            )
            with pytest.raises(
                RunConflictError, match="run_request_id_conflict"
            ):
                await repository.decide_tool_approval(
                    replace(
                        first,
                        request_id=replay_request_id,
                        fingerprint="3" * 64,
                        decision=ToolApprovalDecision.REJECT,
                        decided_at=datetime.now(UTC),
                    )
                )

            resumable = await repository.get_resumable_tool_batch(lease.run.id)
            assert resumable is not None
            assert [call.record.tool_call_id for call in resumable.calls] == [
                "call_safe",
                "call_risky",
            ]
            assert [call.arguments for call in resumable.calls] == [
                {"query": "approval-1"},
                {"query": "approval-2"},
            ]

            leases = await repository.claim(
                "worker_resume",
                lease_seconds=30,
                execution_seconds=60,
                limit=1,
            )
            assert len(leases) == 1
            resumed = leases[0]
            assert resumed.run.execution_epoch > lease.run.execution_epoch
            assert resumed.run.session_id == lease.run.session_id
            stale = await repository.admit_tool_call(
                AdmitToolCallCommand(
                    run_id=lease.run.id,
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    record_id=resumable.calls[0].record.id,
                    admitted_at=datetime.now(UTC),
                )
            )
            assert stale is None
            admitted = await asyncio.gather(
                *(
                    repository.admit_tool_call(
                        AdmitToolCallCommand(
                            run_id=resumed.run.id,
                            worker_id=resumed.run.worker_id or "",
                            execution_epoch=resumed.run.execution_epoch,
                            record_id=call.record.id,
                            admitted_at=datetime.now(UTC),
                        )
                    )
                    for call in resumable.calls
                )
            )
            assert all(record is not None for record in admitted)
            assert [record.ordinal for record in admitted if record is not None] == [1, 2]
            first_record, second_record = admitted
            assert first_record is not None and second_record is not None

            await repository.finish_tool_call(
                _batch_finish(
                    resumed,
                    second_record,
                    suffix="approval",
                    output="second",
                )
            )
            await repository.finish_tool_call(
                _batch_finish(
                    resumed,
                    first_record,
                    suffix="approval",
                    output="first",
                )
            )
            context = await repository.list_context_messages(
                lease.run.session_id, owner_id, lease.run.id
            )
            assert context is not None
            execution = [
                message
                for message in context
                if isinstance(message, ExecutionMessage)
            ]
            assert isinstance(execution[0], ExecutionMessage)
            assert [
                part.tool_call_id
                for part in execution[0].parts
                if isinstance(part, ToolCallPart)
            ] == ["call_safe", "call_risky"]
            result_ids = [
                next(
                    part.tool_call_id
                    for part in message.parts
                    if isinstance(part, ToolResultPart)
                )
                for message in execution[1:]
            ]
            assert result_ids == ["call_safe", "call_risky"]

            finished = await repository.finish(
                FinishRunCommand(
                    resumed.run.id,
                    resumed.run.worker_id or "",
                    resumed.run.execution_epoch,
                    _reply(resumed.run, suffix="approval"),
                    RunUsage(),
                    datetime.now(UTC),
                )
            )
            assert finished is not None and finished.status is RunStatus.COMPLETED

    asyncio.run(scenario())


def test_concurrent_last_approval_decision_queues_once_and_cancel_closes_pending() -> None:
    """并发最后决定遵循行锁；拒绝后等待取消，迟到决定不能恢复。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _ = await _seed_owner_and_profile(factory, suffix="approval-race")
            repository = SqlAlchemyRunRepository(
                factory, SqlAlchemyMessageRepository(factory)
            )
            lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="approval-race",
            )
            begun = await repository.begin_tool_call_batch(
                _batch_command(
                    lease,
                    suffix="approval-race",
                    calls=(("call_r1", "risky_tool"), ("call_r2", "risky_tool")),
                )
            )
            assert begun.waiting_for_approval
            pending = await repository.list_pending_approvals(
                lease.run.session_id, owner_id, lease.run.id
            )
            assert pending is not None and len(pending) == 2
            decisions = await asyncio.gather(
                repository.decide_tool_approval(
                    DecideToolApprovalCommand(
                        session_id=lease.run.session_id,
                        owner_id=owner_id,
                        run_id=lease.run.id,
                        record_id=pending[0].call_id,
                        request_id="race-a",
                        fingerprint="a" * 64,
                        decision=ToolApprovalDecision.APPROVE,
                        decided_at=datetime.now(UTC),
                        auth_session_id="fresh-auth",
                    )
                ),
                repository.decide_tool_approval(
                    DecideToolApprovalCommand(
                        session_id=lease.run.session_id,
                        owner_id=owner_id,
                        run_id=lease.run.id,
                        record_id=pending[1].call_id,
                        request_id="race-b",
                        fingerprint="b" * 64,
                        decision=ToolApprovalDecision.REJECT,
                        decided_at=datetime.now(UTC),
                        auth_session_id="fresh-auth",
                    )
                ),
            )
            assert all(item is not None for item in decisions)
            assert sorted(
                item.status.value for item in decisions if item is not None
            ) == ["queued", "waiting_for_approval"]
            final = await repository.get(lease.run.session_id, owner_id, lease.run.id)
            assert final is not None and final.status is RunStatus.QUEUED
            async with factory() as session:
                resumed_events = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(RunEventModel)
                        .where(
                            RunEventModel.run_id == lease.run.id,
                            RunEventModel.type == "approval_resumed",
                        )
                    )
                    or 0
                )
            assert resumed_events == 1
            async with factory() as session:
                rejected = await session.scalar(
                    select(RunToolCallModel).where(
                        RunToolCallModel.id == pending[1].call_id
                    )
                )
            assert rejected is not None
            assert rejected.status == "failed"
            assert rejected.safe_error_code == "tool_approval_denied"
            assert rejected.result_message_id is not None
            context = await repository.list_context_messages(
                lease.run.session_id, owner_id, lease.run.id
            )
            assert context is not None
            denied_results = [
                ToolOutcome.model_validate_json(part.output)
                for message in context
                if isinstance(message, ExecutionMessage)
                for part in message.parts
                if isinstance(part, ToolResultPart) and part.output is not None
            ]
            assert any(
                outcome.code == "tool_approval_denied" for outcome in denied_results
            )
            # 已决定的调用不能被后续相反决策覆盖。
            decided = decisions[0]
            assert decided is not None
            async with factory() as session:
                row = await session.scalar(
                    select(RunToolCallModel).where(
                        RunToolCallModel.id == pending[0].call_id
                    )
                )
            assert row is not None and row.approval_decision is not None
            opposite = (
                ToolApprovalDecision.REJECT
                if row.approval_decision == ToolApprovalDecision.APPROVE.value
                else ToolApprovalDecision.APPROVE
            )
            with pytest.raises(RunConflictError):
                await repository.decide_tool_approval(
                    DecideToolApprovalCommand(
                        session_id=lease.run.session_id,
                        owner_id=owner_id,
                        run_id=lease.run.id,
                        record_id=pending[0].call_id,
                        request_id="opposite",
                        fingerprint="c" * 64,
                        decision=opposite,
                        decided_at=datetime.now(UTC),
                    )
                )

            cancel_lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="approval-cancel",
            )
            await repository.begin_tool_call_batch(
                _batch_command(
                    cancel_lease,
                    suffix="approval-cancel",
                    calls=(("call_cancel_r", "risky_tool"),),
                )
            )
            cancelled = await repository.cancel(
                cancel_lease.run.session_id,
                owner_id,
                cancel_lease.run.id,
                datetime.now(UTC),
            )
            assert cancelled is not None and cancelled.status is RunStatus.CANCELLED
            with pytest.raises(RunStateError):
                await repository.decide_tool_approval(
                    DecideToolApprovalCommand(
                        session_id=cancel_lease.run.session_id,
                        owner_id=owner_id,
                        run_id=cancel_lease.run.id,
                        record_id="record_approval-cancel_1",
                        request_id="late-cancel",
                        fingerprint="d" * 64,
                        decision=ToolApprovalDecision.APPROVE,
                        decided_at=datetime.now(UTC),
                    )
                )

    asyncio.run(scenario())
