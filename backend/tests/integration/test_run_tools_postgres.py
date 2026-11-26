"""Run 工具快照、额度、调用终态和项目知识库上限的真实 PostgreSQL 测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from integration.llm_fixtures import encrypted_llm_key_fields
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from adapter.access.repository import SqlAlchemyAccessControlRepository
from adapter.auth.model import AuthSessionModel, UserModel
from adapter.file.model import SessionFileModel
from adapter.file.repository import SqlAlchemySessionFileRepository
from adapter.knowledge.model import KnowledgeBaseModel
from adapter.knowledge.repository import KnowledgeRepository
from adapter.llm.model.profile import DefaultModelProfileModel, ModelProfileVersionModel
from adapter.message.converter import conversation_to_domain, execution_to_domain
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.project.model import ProjectModel
from adapter.run.model import RunEventModel, RunModel, RunRequestModel, RunToolCallModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.skill import SqlAlchemySkillRepository
from adapter.skill.model import SkillExecutionModel, SkillModel, SkillVersionModel
from app.access import AccessPolicyService
from app.file import FileObjectMaintenance
from app.run.budget import RunLlmBudget
from app.skill.service import SkillService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.file import FileObjectScanEntry, PendingSessionFile
from domain.knowledge import ProjectKnowledgeLimitReachedError
from domain.llm.model import LlmError
from domain.message import (
    ArtifactFileRef,
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessage,
    ExecutionMessageDraft,
    ExecutionRole,
    FileRefPart,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from domain.run.model import (
    FailRunCommand,
    FinishRunCommand,
    RecordRunUsageCommand,
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
from domain.skill import (
    SKILL_EXECUTE_TOOL_ID,
    DependencyCheck,
    DependencyCheckStatus,
    PackageResource,
    SkillExecutionCompletion,
    SkillExecutionStatus,
    SkillPackageManifest,
    SkillScriptDefinition,
)
from domain.tool import (
    AdmitToolCallCommand,
    BeginToolCallBatchCommand,
    BeginToolCallBatchItem,
    BuiltinToolSource,
    DecideToolApprovalCommand,
    FinishToolCallCommand,
    InitializeRunToolsCommand,
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
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="ATHENA_TEST_DATABASE_URL is not configured"),
]


@asynccontextmanager
async def _database(
    *,
    pool_size: int | None = None,
) -> AsyncGenerator[
    tuple[
        async_sessionmaker[AsyncSession],
        AsyncEngine,
        AsyncEngine,
        str,
    ],
    None,
]:
    """Create one isolated PostgreSQL schema and apply the real migration chain."""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    async with isolated_postgres_schema(
        database_url,
        prefix="athena_v2_tool_test",
        pool_size=pool_size,
    ) as db:
        yield db.session_factory, db.engine, db.admin_engine, db.schema


def test_initial_schema_omits_removed_online_maintenance_tables() -> None:
    """基线迁移 0001_initial_schema 不再包含已移除的在线维护表。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        async with isolated_postgres_schema(database_url, prefix="athena_v2_tool_test") as db:
            engine = db.engine
            schema = db.schema
            async with engine.connect() as connection:
                for table_name in (
                    "file_object_publications",
                    "file_object_reclamation_cursors",
                ):
                    exists = await connection.scalar(
                        text(
                            "SELECT 1 FROM information_schema.tables "
                            "WHERE table_schema = :schema AND table_name = :table_name"
                        ),
                        {"schema": schema, "table_name": table_name},
                    )
                    assert exists is None

    asyncio.run(scenario())


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
                role="member",
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
                updated_at=now,
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
                context_turn_id=message_id,
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


async def _begin_executing_call(
    repository: SqlAlchemyRunRepository,
    lease: RunLease,
    *,
    suffix: str,
    tool_call_id: str,
    ordinal_label: str = "",
    model_name: str = "safe_tool",
    tool_id: str = "builtin_safe_tool",
    arguments: dict[str, object] | None = None,
) -> RunToolCallRecord:
    now = datetime.now(UTC)
    run = lease.run
    record_id = f"record_{suffix}_{ordinal_label}"
    await repository.begin_tool_call_batch(
        BeginToolCallBatchCommand(
            run_id=run.id,
            worker_id=run.worker_id or "",
            execution_epoch=run.execution_epoch,
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
                        model_name,
                        arguments or {"query": f"value-{suffix}-{ordinal_label}"},
                    ),
                ),
                tool_call_id=None,
                source_message_id=None,
                created_at=now,
                metadata=MessageMetadata(
                    provider="chat_completions", model="tool-integration-model"
                ),
            ),
            calls=(
                BeginToolCallBatchItem(
                    record_id=record_id,
                    tool_call_id=tool_call_id,
                    model_name=model_name,
                    tool_id=tool_id,
                    tool_kind=ToolKind.BUILTIN,
                    requires_approval=False,
                ),
            ),
            round=1,
            requested_at=now,
        )
    )
    admitted = await repository.admit_tool_call(
        AdmitToolCallCommand(
            run_id=run.id,
            worker_id=run.worker_id or "",
            execution_epoch=run.execution_epoch,
            record_id=record_id,
            admitted_at=now,
        )
    )
    assert admitted is not None
    return admitted


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


async def _start_skill_execution(
    factory: async_sessionmaker[AsyncSession],
    lease: RunLease,
    begun: RunToolCallRecord,
    *,
    suffix: str,
) -> tuple[str, str, str]:
    """为已准入调用创建不可变 Skill 版本及 running 脚本附加记录。"""
    now = datetime.now(UTC)
    skill_id = f"skill_{suffix}"
    version_id = f"skillver_{suffix}"
    manifest = SkillPackageManifest(
        slug=f"tool-{suffix}",
        name="工具测试 Skill",
        description="验证脚本产物与工具调用同事务发布。",
        guide="只执行登记脚本。",
        scripts=(
            SkillScriptDefinition(
                path="scripts/task.py",
                label="执行任务",
                purpose="验证原子产物发布。",
                parameters={"type": "object", "additionalProperties": True},
            ),
        ),
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=1,
                sha256="a" * 64,
                text=True,
            ),
            PackageResource(
                path="scripts/task.py",
                size_bytes=1,
                sha256="b" * 64,
                text=True,
            ),
        ),
    )
    dependency_check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.13",
        python_implementation="CPython",
        items=(),
        checked_at=now,
    )
    async with factory() as session, session.begin():
        skill = SkillModel(
            id=skill_id,
            slug=manifest.slug,
            current_version_id=None,
            enabled=True,
            script_requires_approval=False,
            revision=1,
            created_by=lease.run.owner_id,
            created_at=now,
            updated_at=now,
        )
        session.add(skill)
        await session.flush()
        version = SkillVersionModel(
            id=version_id,
            skill_id=skill_id,
            ordinal=1,
            label="v1",
            guide=manifest.guide,
            manifest=manifest.model_dump(mode="json"),
            object_key=f"skill-packages/{suffix}.zip",
            package_sha256="c" * 64,
            dependency_requirements=[],
            dependency_check=dependency_check.model_dump(mode="json"),
            created_by=lease.run.owner_id,
            request_operation="import",
            idempotency_key=f"import-{suffix}",
            request_fingerprint="d" * 64,
            request_target_skill_id=None,
            created_at=now,
        )
        session.add(version)
        await session.flush()
        skill.current_version_id = version_id
    execution = await _start_script_execution_record(
        SqlAlchemySkillRepository(factory),
        lease=lease,
        begun=begun,
        suffix=suffix,
        skill_id=skill_id,
        version_id=version_id,
        dependency_check=dependency_check,
    )
    return skill_id, version_id, execution


async def _start_script_execution_record(
    repository: SqlAlchemySkillRepository,
    *,
    lease: RunLease,
    begun: RunToolCallRecord,
    suffix: str,
    skill_id: str,
    version_id: str,
    dependency_check: DependencyCheck,
) -> str:
    """隔离测试辅助：创建脚本执行记录并返回其 ID。"""
    execution_id = f"skexec_{suffix}"
    start = await repository.start_script_execution(
        execution_id=execution_id,
        run_id=lease.run.id,
        session_id=lease.run.session_id,
        tool_call_record_id=begun.id,
        skill_id=skill_id,
        skill_version_id=version_id,
        script_path="scripts/task.py",
        input_file_ids=(),
        parameters_sha256="e" * 64,
        python_version=dependency_check.python_version,
        dependency_check=dependency_check,
        created_at=datetime.now(UTC),
    )
    assert start is not None and start.created is True
    return start.record.id


def _pending_artifacts(
    call_id: str,
    *,
    count: int,
    prefix: str,
) -> tuple[PendingSessionFile, ...]:
    """构造已上传但尚未由 Run 事务发布的有序产物元数据。"""
    now = datetime.now(UTC)
    return tuple(
        PendingSessionFile(
            id=f"file_{prefix}_{index}",
            filename=f"{prefix}-{index}.csv",
            label=f"产物 {index}",
            mime_type="text/csv",
            size_bytes=2 * 1024 * 1024 * 1024 + index,
            sha256=f"{index:x}" * 64,
            object_key=f"session-artifacts/{prefix}/{index}",
            output_index=index,
            created_at=now,
        )
        for index in range(count)
    )


def _skill_finish_command(
    lease: RunLease,
    begun: RunToolCallRecord,
    *,
    suffix: str,
    execution_id: str,
    artifacts: tuple[PendingSessionFile, ...],
) -> FinishToolCallCommand:
    """构造成功脚本结果；展平用户拒绝的产物尺寸也远高于上传 50 MiB。"""
    now = datetime.now(UTC)
    outcome = ToolOutcome(
        status=ToolOutcomeStatus.OK,
        code="skill_execution_succeeded",
        message="脚本执行成功。",
        data={"internal_object_key": artifacts[0].object_key if artifacts else ""},
    )
    refs = tuple(
        ArtifactFileRef(
            file_id=item.id,
            filename=item.filename,
            label=item.label,
            mime_type=item.mime_type,
            size_bytes=item.size_bytes,
        )
        for item in artifacts
    )
    return FinishToolCallCommand(
        run_id=lease.run.id,
        worker_id=lease.run.worker_id or "",
        execution_epoch=lease.run.execution_epoch,
        record_id=begun.id,
        result_message=ExecutionMessageDraft(
            id=f"exec_skill_result_{suffix}",
            session_id=lease.run.session_id,
            owner_id=lease.run.owner_id,
            run_id=lease.run.id,
            thread_id=lease.run.id,
            role=ExecutionRole.TOOL,
            parts=(
                ToolResultPart(
                    tool_call_id=begun.tool_call_id,
                    status=protocol_status_for_outcome(outcome),
                    output=outcome.canonical_output(),
                    artifacts=refs,
                ),
            ),
            tool_call_id=begun.tool_call_id,
            source_message_id=None,
            created_at=now,
            metadata=MessageMetadata(),
        ),
        outcome=outcome,
        finished_at=now,
        artifacts=artifacts,
        delivery_message=ConversationMessageDraft(
            id=f"delivery_skill_{suffix}",
            session_id=lease.run.session_id,
            owner_id=lease.run.owner_id,
            role=ConversationRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            parts=tuple(
                FileRefPart(
                    file_id=item.id,
                    filename=item.filename,
                    mime_type=item.mime_type,
                    size_bytes=item.size_bytes,
                )
                for item in artifacts
            ),
            reply_to_message_id=lease.run.trigger_message_id,
            run_id=lease.run.id,
            context_turn_id=lease.run.trigger_message_id,
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata=MessageMetadata(),
        ),
        skill_execution_completion=SkillExecutionCompletion(
            execution_id=execution_id,
            status=SkillExecutionStatus.SUCCEEDED,
            log_object_key=f"skill-logs/{suffix}.log",
            log_truncated=False,
            log_error_code=None,
            error_code=None,
            cleanup_error_code=None,
            finished_at=now,
        ),
    )


async def _seed_published_files(
    factory: async_sessionmaker[AsyncSession],
    lease: RunLease,
    *,
    count: int,
) -> None:
    """直接写入配额测试所需的已发布上传行，不创建额外 Run。"""
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add_all(
            [
                SessionFileModel(
                    id=f"seed_file_{index}",
                    session_id=lease.run.session_id,
                    owner_id=lease.run.owner_id,
                    source="upload",
                    filename=f"seed-{index}.txt",
                    label=None,
                    mime_type="text/plain",
                    size_bytes=1,
                    sha256=f"{index % 10}" * 64,
                    object_key=f"session-files/seed/{index}",
                    source_call_id=None,
                    output_index=None,
                    idempotency_key=f"seed-{index}",
                    request_fingerprint="f" * 64,
                    created_at=now,
                )
                for index in range(count)
            ]
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
        context_turn_id=run.trigger_message_id,
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

            budget = RunLlmBudget(used=lease.run.llm_request_count, maximum=40)
            for expected in range(1, 40):
                assert budget.reserve() == expected
            assert budget.final_only is True
            assert budget.can_compress is False
            assert budget.reserve() == 40
            with pytest.raises(LlmError, match="run_llm_request_limit_exceeded"):
                budget.reserve()
            recorded = await repository.record_usage(
                RecordRunUsageCommand(
                    run_id=lease.run.id,
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    attempt=1,
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    complete=True,
                    recorded_at=datetime.now(UTC),
                    llm_request_count=budget.used,
                )
            )
            assert recorded is not None and recorded.llm_request_count == 40
            # 单调不回退：传入更小的 llm_request_count 不得覆盖既有计数。
            non_regressing = await repository.record_usage(
                RecordRunUsageCommand(
                    run_id=lease.run.id,
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    attempt=2,
                    input_tokens=4,
                    output_tokens=2,
                    total_tokens=6,
                    complete=True,
                    recorded_at=datetime.now(UTC),
                    llm_request_count=12,
                )
            )
            assert non_regressing is not None and non_regressing.llm_request_count == 40

            begun = await _begin_executing_call(
                repository,
                lease,
                suffix="atomic",
                tool_call_id="call_atomic_1",
                ordinal_label="1",
            )
            first_finish = _finish_command(
                lease,
                begun,
                suffix="atomic",
                ordinal_label="1",
            )
            await repository.finish_tool_call(first_finish)
            # 已终结的工具调用再次调用 finish_tool_call 安全返回 None，拒绝覆盖。
            repeated_finish = await repository.finish_tool_call(first_finish)
            assert repeated_finish is None

            second_begun = await _begin_executing_call(
                repository,
                lease,
                suffix="atomic",
                tool_call_id="call_atomic_2",
                ordinal_label="2",
            )
            second_finish = _finish_command(
                lease,
                second_begun,
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


def test_skill_artifacts_messages_and_execution_completion_are_one_transaction() -> None:
    """真实 PostgreSQL 中多产物、脚本终态、TOOL 消息和调用终态原子提交并可重放。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _profile_id = await _seed_owner_and_profile(
                factory,
                suffix="skill-artifacts",
            )
            repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="skill-artifacts",
            )
            begun = await _begin_executing_call(
                repository,
                lease,
                suffix="skill-artifacts",
                tool_call_id="call_skill_artifacts",
                model_name="skill_execute",
                tool_id=SKILL_EXECUTE_TOOL_ID,
                arguments={
                    "skill_id": "skill_test",
                    "script_path": "scripts/task.py",
                    "parameters": {"api_token": "must-stay-server-side"},
                    "input_file_ids": [],
                },
            )
            skill_id, version_id, execution_id = await _start_skill_execution(
                factory,
                lease,
                begun,
                suffix="skill-artifacts",
            )
            assert skill_id == "skill_skill-artifacts"
            assert version_id == "skillver_skill-artifacts"
            artifacts = _pending_artifacts(
                begun.id,
                count=2,
                prefix="skill-artifacts",
            )
            # 会话产物不继承用户上传的 50 MiB 单文件限制。
            assert all(item.size_bytes > 50 * 1024 * 1024 for item in artifacts)
            command = _skill_finish_command(
                lease,
                begun,
                suffix="skill-artifacts",
                execution_id=execution_id,
                artifacts=artifacts,
            )

            completed = await repository.finish_tool_call(command)
            # 非 EXECUTING 终态守卫：重复完成安全返回 None，既有产物、消息与审计行保持不变。
            replay = await repository.finish_tool_call(command)

            assert completed is not None and replay is None
            async with factory() as session:
                call = await session.get(RunToolCallModel, begun.id)
                files = (
                    await session.scalars(
                        select(SessionFileModel)
                        .where(SessionFileModel.source_call_id == begun.id)
                        .order_by(SessionFileModel.output_index)
                    )
                ).all()
                execution = await session.get(SkillExecutionModel, execution_id)
                result = await session.get(
                    ExecutionMessageModel,
                    command.result_message.id,
                )
                assert command.delivery_message is not None
                delivery = await session.get(
                    ConversationMessageModel,
                    command.delivery_message.id,
                )
                result_count = await session.scalar(
                    select(func.count(ExecutionMessageModel.id)).where(
                        ExecutionMessageModel.id == command.result_message.id
                    )
                )
            assert call is not None
            assert call.status == ToolCallStatus.COMPLETED.value
            assert call.result_message_id == command.result_message.id
            assert [item.id for item in files] == [item.id for item in artifacts]
            assert [item.size_bytes for item in files] == [item.size_bytes for item in artifacts]
            assert execution is not None
            assert execution.status == SkillExecutionStatus.SUCCEEDED.value
            assert [item["file_id"] for item in execution.result_artifacts] == [
                item.id for item in artifacts
            ]
            assert result is not None and result_count == 1
            assert delivery is not None
            delivery_message = conversation_to_domain(delivery)
            assert delivery_message.role is ConversationRole.ASSISTANT
            assert tuple(
                part.file_id for part in delivery_message.parts if isinstance(part, FileRefPart)
            ) == tuple(item.id for item in artifacts)
            result_message = execution_to_domain(result)
            result_part = next(
                part for part in result_message.parts if isinstance(part, ToolResultPart)
            )
            assert result_part.artifacts == tuple(
                ArtifactFileRef(
                    file_id=item.id,
                    filename=item.filename,
                    label=item.label,
                    mime_type=item.mime_type,
                    size_bytes=item.size_bytes,
                )
                for item in artifacts
            )

    asyncio.run(scenario())


def test_skill_start_unknown_without_completion_settles_running_audit_by_call_id() -> None:
    """启动回查失败后无伪 completion 的 UNKNOWN 仍可按工具调用身份原子收敛。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _profile_id = await _seed_owner_and_profile(
                factory,
                suffix="skill-start-unknown",
            )
            repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="skill-start-unknown",
            )
            begun = await _begin_executing_call(
                repository,
                lease,
                suffix="skill-start-unknown",
                tool_call_id="call_skill_start_unknown",
                model_name="skill_execute",
                tool_id=SKILL_EXECUTE_TOOL_ID,
            )
            _skill_id, _version_id, execution_id = await _start_skill_execution(
                factory,
                lease,
                begun,
                suffix="skill-start-unknown",
            )
            unknown = ToolOutcome(
                status=ToolOutcomeStatus.UNKNOWN,
                code="skill_execution_start_unknown",
                message="脚本执行记录状态无法确认。",
            )
            finish = _finish_command(
                lease,
                begun,
                suffix="skill-start-unknown",
                outcome=unknown,
            )

            await repository.finish_tool_call(finish)
            # 已终态调用再次提交 finish_tool_call 安全返回 None，不破坏既有终态与审计。
            replayed = await repository.finish_tool_call(finish)

            async with factory() as session:
                call = await session.get(RunToolCallModel, begun.id)
                execution = await session.get(SkillExecutionModel, execution_id)
            assert replayed is None
            assert call is not None and call.status == ToolCallStatus.UNKNOWN.value
            assert execution is not None
            assert execution.status == SkillExecutionStatus.UNKNOWN.value
            assert execution.error_code == "tool_outcome_unknown"
            assert execution.finished_at is not None

            converged = await repository.fail(
                FailRunCommand(
                    run_id=lease.run.id,
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    safe_error_code="tool_outcome_unknown",
                    failed_at=datetime.now(UTC),
                )
            )
            assert converged is not None
            assert converged.status is RunStatus.FAILED
            assert converged.safe_error_code == "tool_outcome_unknown"
            async with factory() as session:
                execution = await session.get(SkillExecutionModel, execution_id)
            assert execution is not None
            assert execution.status == SkillExecutionStatus.UNKNOWN.value

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
            settled = await _begin_executing_call(
                repository,
                failed_lease,
                suffix="failed",
                tool_call_id="call_failed_settled",
                ordinal_label="1",
            )
            settled_finish = _finish_command(
                failed_lease,
                settled,
                suffix="failed",
                ordinal_label="1",
            )
            await repository.finish_tool_call(settled_finish)
            await _begin_executing_call(
                repository,
                failed_lease,
                suffix="failed",
                tool_call_id="call_failed_executing",
                ordinal_label="2",
            )
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
            await _begin_executing_call(
                repository,
                finish_lease,
                suffix="finish",
                tool_call_id="call_finish_executing",
                ordinal_label="1",
            )
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
            await _begin_executing_call(
                repository,
                cancel_lease,
                suffix="cancel",
                tool_call_id="call_cancel_executing",
                ordinal_label="1",
            )
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
            reclaimed_call = await _begin_executing_call(
                repository,
                reclaim_lease,
                suffix="reclaim",
                tool_call_id="call_reclaim_executing",
                ordinal_label="1",
            )
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
                reclaimed_call,
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
            mismatch_call = await _begin_executing_call(
                repository,
                mismatch_lease,
                suffix="mismatch",
                tool_call_id="call_mismatch",
                ordinal_label="1",
            )
            mismatch_finish = _finish_command(
                mismatch_lease,
                mismatch_call,
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
            unknown_call = await _begin_executing_call(
                repository,
                unknown_lease,
                suffix="unknown",
                tool_call_id="call_unknown",
                ordinal_label="1",
            )
            unknown_finished = await repository.finish_tool_call(
                _finish_command(
                    unknown_lease,
                    unknown_call,
                    suffix="unknown",
                    ordinal_label="1",
                    outcome=ToolOutcome(
                        status=ToolOutcomeStatus.UNKNOWN,
                        code="tool_timeout",
                        message="unknown",
                    ),
                )
            )
            assert unknown_finished is not None
            # 工具批次收尾统一通过 fail() 原子收敛 Run 终态与未完成工具调用。
            unknown_result = await repository.fail(
                FailRunCommand(
                    run_id=unknown_lease.run.id,
                    worker_id=unknown_lease.run.worker_id or "",
                    execution_epoch=unknown_lease.run.execution_epoch,
                    safe_error_code="tool_outcome_unknown",
                    failed_at=datetime.now(UTC),
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
                        role="member",
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
                tool_id=("builtin_risky_tool" if name == "risky_tool" else "builtin_safe_tool"),
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
    )


def test_tool_approval_batch_barrier_resume_and_context_order_are_atomic() -> None:
    """一个待审批调用建立整批屏障；最后决定只排队一次，恢复后结果稳定排序。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _ = await _seed_owner_and_profile(factory, suffix="approval")
            repository = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
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
            waiting = await repository.get(lease.run.session_id, owner_id, lease.run.id)
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
                        RunRequestModel.operation == RunRequestOperation.DECIDE_TOOL_APPROVAL.value,
                        RunRequestModel.request_id == replay_request_id,
                    )
                )
            assert replay_request is not None
            assert replay_request.fingerprint == "2" * 64
            assert RunRequestResult.model_validate(replay_request.result).run_id == lease.run.id
            with pytest.raises(RunConflictError, match="run_request_id_conflict"):
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
            execution = [message for message in context if isinstance(message, ExecutionMessage)]
            assert isinstance(execution[0], ExecutionMessage)
            assert [
                part.tool_call_id for part in execution[0].parts if isinstance(part, ToolCallPart)
            ] == ["call_safe", "call_risky"]
            result_ids = [
                next(
                    part.tool_call_id for part in message.parts if isinstance(part, ToolResultPart)
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
            repository = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
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
            assert sorted(item.status.value for item in decisions if item is not None) == [
                "queued",
                "waiting_for_approval",
            ]
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
                    select(RunToolCallModel).where(RunToolCallModel.id == pending[1].call_id)
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
            assert any(outcome.code == "tool_approval_denied" for outcome in denied_results)
            # 已决定的调用不能被后续相反决策覆盖。
            decided = decisions[0]
            assert decided is not None
            async with factory() as session:
                row = await session.scalar(
                    select(RunToolCallModel).where(RunToolCallModel.id == pending[0].call_id)
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


def test_skill_artifact_quota_rejection_rolls_back_and_converges_running_record() -> None:
    """配额超额时整组文件、结果消息和脚本终态都不提交，失败收敛不再遗留 running。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _profile_id = await _seed_owner_and_profile(
                factory,
                suffix="skill-quota",
            )
            repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="skill-quota",
            )
            await _seed_published_files(factory, lease, count=99)
            begun = await _begin_executing_call(
                repository,
                lease,
                suffix="skill-quota",
                tool_call_id="call_skill_quota",
                model_name="skill_execute",
                tool_id=SKILL_EXECUTE_TOOL_ID,
            )
            _skill_id, _version_id, execution_id = await _start_skill_execution(
                factory,
                lease,
                begun,
                suffix="skill-quota",
            )
            artifacts = _pending_artifacts(
                begun.id,
                count=2,
                prefix="skill-quota",
            )
            command = _skill_finish_command(
                lease,
                begun,
                suffix="skill-quota",
                execution_id=execution_id,
                artifacts=artifacts,
            )

            with pytest.raises(ValueError, match="session_file_quota_exceeded"):
                await repository.finish_tool_call(command)

            async with factory() as session:
                call = await session.get(RunToolCallModel, begun.id)
                execution = await session.get(SkillExecutionModel, execution_id)
                artifact_count = await session.scalar(
                    select(func.count(SessionFileModel.id)).where(
                        SessionFileModel.source_call_id == begun.id
                    )
                )
                result = await session.get(
                    ExecutionMessageModel,
                    command.result_message.id,
                )
                assert command.delivery_message is not None
                delivery = await session.get(
                    ConversationMessageModel,
                    command.delivery_message.id,
                )
            assert call is not None and call.status == ToolCallStatus.EXECUTING.value
            assert call.result_message_id is None
            assert execution is not None
            assert execution.status == SkillExecutionStatus.RUNNING.value
            assert artifact_count == 0
            assert result is None
            assert delivery is None

            failed = await repository.fail(
                FailRunCommand(
                    run_id=lease.run.id,
                    worker_id=lease.run.worker_id or "",
                    execution_epoch=lease.run.execution_epoch,
                    safe_error_code="execution_failed",
                    failed_at=datetime.now(UTC),
                )
            )
            assert failed is not None and failed.status is RunStatus.FAILED
            async with factory() as session:
                call = await session.get(RunToolCallModel, begun.id)
                execution = await session.get(SkillExecutionModel, execution_id)
            assert call is not None
            assert call.status == ToolCallStatus.UNKNOWN.value
            assert call.result_message_id is not None
            assert execution is not None
            assert execution.status == SkillExecutionStatus.UNKNOWN.value
            assert execution.error_code == "tool_outcome_unknown"
            assert execution.finished_at is not None

    asyncio.run(scenario())


def test_upload_and_multi_artifact_publish_concurrency_never_exceeds_quota() -> None:
    """共享配额锁下上传与多产物整组发布只有一个可提交到 100 文件边界。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _profile_id = await _seed_owner_and_profile(
                factory,
                suffix="skill-race",
            )
            repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            files_repository = SqlAlchemySessionFileRepository(factory)
            lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="skill-race",
            )
            await _seed_published_files(factory, lease, count=98)
            begun = await _begin_executing_call(
                repository,
                lease,
                suffix="skill-race",
                tool_call_id="call_skill_race",
                model_name="skill_execute",
                tool_id=SKILL_EXECUTE_TOOL_ID,
            )
            _skill_id, _version_id, execution_id = await _start_skill_execution(
                factory,
                lease,
                begun,
                suffix="skill-race",
            )
            artifacts = _pending_artifacts(
                begun.id,
                count=2,
                prefix="skill-race",
            )
            finish = _skill_finish_command(
                lease,
                begun,
                suffix="skill-race",
                execution_id=execution_id,
                artifacts=artifacts,
            )
            upload = PendingSessionFile(
                id="file_upload_race",
                filename="upload-race.txt",
                label=None,
                mime_type="text/plain",
                size_bytes=1,
                sha256="9" * 64,
                object_key="session-files/upload-race",
                output_index=-1,
                created_at=datetime.now(UTC),
            )
            finish_result, upload_result = await asyncio.gather(
                repository.finish_tool_call(finish),
                files_repository.publish_upload(
                    lease.run.session_id,
                    owner_id,
                    upload,
                    idempotency_key="upload-race",
                    request_fingerprint="8" * 64,
                    published_at=datetime.now(UTC),
                ),
                return_exceptions=True,
            )

            async with factory() as session:
                count = await session.scalar(
                    select(func.count(SessionFileModel.id)).where(
                        SessionFileModel.session_id == lease.run.session_id
                    )
                )
                call = await session.get(RunToolCallModel, begun.id)
                execution = await session.get(SkillExecutionModel, execution_id)
            assert count is not None and count <= 100
            assert isinstance(finish_result, BaseException) != isinstance(
                upload_result,
                BaseException,
            )
            if isinstance(finish_result, Exception):
                assert str(finish_result) == "session_file_quota_exceeded"
                assert not isinstance(upload_result, Exception)
                assert count == 99
                assert call is not None
                assert call.status == ToolCallStatus.EXECUTING.value
                assert execution is not None
                assert execution.status == SkillExecutionStatus.RUNNING.value
                failed = await repository.fail(
                    FailRunCommand(
                        run_id=lease.run.id,
                        worker_id=lease.run.worker_id or "",
                        execution_epoch=lease.run.execution_epoch,
                        safe_error_code="execution_failed",
                        failed_at=datetime.now(UTC),
                    )
                )
                assert failed is not None
                async with factory() as session:
                    execution = await session.get(SkillExecutionModel, execution_id)
                assert execution is not None
                assert execution.status == SkillExecutionStatus.UNKNOWN.value
            else:
                assert isinstance(upload_result, ValueError)
                assert str(upload_result) == "session_file_quota_exceeded"
                assert count == 100
                assert call is not None
                assert call.status == ToolCallStatus.COMPLETED.value
                assert execution is not None
                assert execution.status == SkillExecutionStatus.SUCCEEDED.value

    asyncio.run(scenario())


def test_upload_and_run_finish_both_wait_on_session_before_quota_lock() -> None:
    """真实 PostgreSQL 中上传和 Run 完成都先等 Session，再串行占用配额。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, _profile_id = await _seed_owner_and_profile(
                factory,
                suffix="skill-lock-order",
            )
            repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            files_repository = SqlAlchemySessionFileRepository(factory)
            lease = await _running_run(
                repository,
                factory,
                owner_id=owner_id,
                suffix="skill-lock-order",
            )
            begun = await _begin_executing_call(
                repository,
                lease,
                suffix="skill-lock-order",
                tool_call_id="call_skill_lock_order",
                model_name="skill_execute",
                tool_id=SKILL_EXECUTE_TOOL_ID,
            )
            _skill_id, _version_id, execution_id = await _start_skill_execution(
                factory,
                lease,
                begun,
                suffix="skill-lock-order",
            )
            artifacts = _pending_artifacts(
                begun.id,
                count=2,
                prefix="skill-lock-order",
            )
            upload = PendingSessionFile(
                id="file_upload_lock_order",
                filename="upload-lock-order.txt",
                label=None,
                mime_type="text/plain",
                size_bytes=1,
                sha256="7" * 64,
                object_key="session-files/lock-order",
                output_index=-1,
                created_at=datetime.now(UTC),
            )
            finish = _skill_finish_command(
                lease,
                begun,
                suffix="skill-lock-order",
                execution_id=execution_id,
                artifacts=artifacts,
            )

            # 两任务同时先阻塞在同一 Session 行，释放后各自在同一配额锁内
            # 提交文件；这里验证上传与多产物不会越过 100 文件边界。
            async with factory() as blocker, blocker.begin():
                await blocker.scalar(
                    select(SessionModel.id)
                    .where(SessionModel.id == lease.run.session_id)
                    .with_for_update()
                )
                finish_task = asyncio.create_task(repository.finish_tool_call(finish))
                upload_task = asyncio.create_task(
                    files_repository.publish_upload(
                        lease.run.session_id,
                        owner_id,
                        upload,
                        idempotency_key="upload-lock-order",
                        request_fingerprint="6" * 64,
                        published_at=datetime.now(UTC),
                    )
                )
                await asyncio.sleep(0.15)
                assert finish_task.done() is False
                assert upload_task.done() is False

            finished, uploaded = await asyncio.wait_for(
                asyncio.gather(finish_task, upload_task),
                timeout=5,
            )
            assert finished is not None
            assert uploaded.id == upload.id
            async with factory() as session:
                count = await session.scalar(
                    select(func.count(SessionFileModel.id)).where(
                        SessionFileModel.session_id == lease.run.session_id
                    )
                )
            assert count == 3

    asyncio.run(scenario())


def test_pool_size_one_maintenance_imports_and_publishes_without_nested_connections(
    tmp_path: Path,
) -> None:
    """真实 pool_size=1 下离线维护、Skill 导入和文件发布并发不会等待自身连接。"""

    class PoolStore:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}
            self.deleted: list[str] = []

        async def put_file(
            self,
            key: str,
            path: Path,
            *,
            content_type: str,
        ) -> None:
            del content_type
            self.values[key] = path.read_bytes()

        async def delete(self, key: str) -> None:
            self.deleted.append(key)
            self.values.pop(key, None)

        async def iter_maintenance_candidates(
            self,
            *,
            older_than_seconds: int = 86400,
            after_key: str | None = None,
            maximum_items: int = 1000,
        ) -> AsyncIterator[FileObjectScanEntry]:
            del older_than_seconds
            examined = 0
            for key in sorted(self.values):
                if after_key is not None and key <= after_key:
                    continue
                if examined >= maximum_items:
                    return
                examined += 1
                yield FileObjectScanEntry(object_key=key, is_candidate=True)

        async def aclose(self) -> None:
            return None

    class AdminReader:
        def __init__(self, user: User) -> None:
            self.user = user

        async def get_current_session_user(
            self,
            user_id: str,
            session_id: str,
            now: datetime,
        ) -> User | None:
            del session_id, now
            return self.user if self.user.id == user_id else None

    class Checker:
        def check(
            self,
            requirements: tuple[str, ...],
            *,
            checked_at: datetime,
        ) -> DependencyCheck:
            del requirements
            return DependencyCheck(
                status=DependencyCheckStatus.SATISFIED,
                python_version="3.14.0",
                python_implementation="CPython",
                checked_at=checked_at,
            )

    async def scenario() -> None:
        async with _database(pool_size=1) as (factory, _engine, _admin, _schema):
            owner_id, _profile_id = await _seed_owner_and_profile(
                factory,
                suffix="pool-one",
            )
            async with factory() as session, session.begin():
                await session.execute(
                    update(UserModel).where(UserModel.id == owner_id).values(role="super_admin")
                )
                session.add(
                    AuthSessionModel(
                        id="auth_pool_one",
                        user_id=owner_id,
                        token_hash="e" * 64,
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                        revoked_time=None,
                        created_at=datetime.now(UTC),
                    )
                )
            now = datetime.now(UTC)
            actor_user = User(
                id=owner_id,
                email="tool-pool-one@example.test",
                password_hash="unused",
                display_name=None,
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
                role=UserRole.SUPER_ADMIN,
            )
            actor = AuthContext(actor_user, "auth_pool_one")
            store = PoolStore()
            orphan_key = "session-files/00/orphan"
            store.values[orphan_key] = b"orphan"
            manifest = SkillPackageManifest(
                slug="pool-one",
                name="pool one",
                description="connection pool regression",
                guide="guide",
                files=(
                    PackageResource(
                        path="SKILL.md",
                        size_bytes=6,
                        sha256="a" * 64,
                        text=True,
                    ),
                ),
            )
            package_path = tmp_path / "pool-one.zip"
            package_path.write_bytes(b"package")
            package_sha256 = "b" * 64
            skill_repository = SqlAlchemySkillRepository(factory)
            skill_ids = iter(("skill_pool_one", "skillver_pool_one"))
            skill_service = SkillService(
                skill_repository,
                store,  # type: ignore[arg-type]
                AdminReader(actor_user),
                access_policy=AccessPolicyService(SqlAlchemyAccessControlRepository(factory)),
                package_validator=SimpleNamespace(
                    validate=lambda path: SimpleNamespace(
                        package_sha256=package_sha256,
                        manifest=manifest,
                    )
                ),  # type: ignore[arg-type]
                dependency_checker=Checker(),  # type: ignore[arg-type]
                id_factory=lambda prefix: next(skill_ids),
                clock=lambda: now,
            )

            run_repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            lease = await _running_run(
                run_repository,
                factory,
                owner_id=owner_id,
                suffix="pool-one",
            )
            file_repository = SqlAlchemySessionFileRepository(factory)
            upload = PendingSessionFile(
                id="file_pool_one",
                filename="pool-one.txt",
                label=None,
                mime_type="text/plain",
                size_bytes=1,
                sha256="c" * 64,
                object_key="session-files/10/upload",
                output_index=-1,
                created_at=now,
            )
            store.values[upload.object_key] = b"x"
            maintenance = FileObjectMaintenance(
                store=store,  # type: ignore[arg-type]
                references=file_repository,
            )

            imported = await skill_service.import_package(
                actor,
                package_path,
                idempotency_key="pool-one",
            )
            published = await file_repository.publish_upload(
                lease.run.session_id,
                owner_id,
                upload,
                idempotency_key="pool-one-upload",
                request_fingerprint="d" * 64,
                published_at=now,
            )
            maintained = await maintenance.run(
                execute=True,
                writers_stopped=True,
            )
            assert maintained.failed_count == 0
            assert orphan_key in store.deleted
            assert imported.skill.id == "skill_pool_one"
            assert published.id == upload.id
            async with factory() as session:
                skill_count = await session.scalar(
                    select(func.count()).select_from(SkillVersionModel)
                )
            assert skill_count == 1

    asyncio.run(scenario())
