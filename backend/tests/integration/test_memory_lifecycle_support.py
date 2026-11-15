"""记忆生命周期 PostgreSQL 集成测试的隔离 schema 与建数工具。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.llm.model.profile import DefaultModelProfileModel, ModelProfileVersionModel
from adapter.memory.model import MemoryJobModel, MemoryScopeStateModel, MemorySettingModel
from adapter.memory.repository import MemoryRepository
from adapter.message.model import ConversationMessageModel
from adapter.project.model import ProjectModel
from adapter.run.model import RunModel
from adapter.session.model import SessionModel
from domain.memory.lifecycle import (
    ExtractBatchMember,
    ExtractBatchPayload,
    ExtractionDecision,
    ExtractPendingPayload,
    JobKind,
    JobStatus,
    MemoryCandidate,
    MemoryPendingRun,
)
from domain.system_prompt import INITIAL_SYSTEM_PROMPT_IDS

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@dataclass(frozen=True, slots=True)
class DatabaseHarness:
    """单个测试独占 schema 的异步会话工厂与 Alembic 配置。"""

    factory: async_sessionmaker[AsyncSession]
    config: Config
    schema: str


@dataclass(frozen=True, slots=True)
class HeadFixture:
    """head schema 中供记忆批次测试复用的最小身份与来源。"""

    owner_id: str
    auth_session_id: str
    project_id: str
    session_id: str
    profile_id: str
    run_ids: tuple[str, ...]
    message_ids: tuple[str, ...]
    now: datetime


@asynccontextmanager
async def isolated_database(*, revision: str = "head") -> AsyncIterator[DatabaseHarness]:
    """创建隔离 schema，升级到指定 revision，并在结束后彻底删除。"""
    database_url = DATABASE_URL
    if database_url is None:
        raise RuntimeError("ATHENA_TEST_DATABASE_URL is required")
    schema = f"athena_v2_memory_lifecycle_{token_hex(6)}"
    admin = create_async_engine(database_url, poolclass=NullPool)
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        config = Config(str(ALEMBIC_INI))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        config.attributes["connect_args"] = {"server_settings": {"search_path": f"{schema},public"}}
        config.attributes["version_table_schema"] = schema
        await asyncio.to_thread(command.upgrade, config, revision)
        yield DatabaseHarness(
            factory=async_sessionmaker(engine, expire_on_commit=False),
            config=config,
            schema=schema,
        )
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def seed_head_fixture(
    factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: str,
    pending_count: int,
    generation: int = 1,
) -> HeadFixture:
    """写入一个项目 Session、固定模型版本及指定数量的 pending Run。"""
    if pending_count < 0:
        raise ValueError("pending_count_invalid")
    project_id = f"project_{owner_id}"
    session_id = f"session_{owner_id}"
    profile_id = f"profile_{owner_id}"
    auth_session_id = f"auth_{owner_id}"
    now = datetime.now(UTC)
    run_ids = tuple(f"run_{owner_id}_{index:03d}" for index in range(pending_count))
    message_ids = tuple(f"message_{owner_id}_{index:03d}" for index in range(pending_count))

    async with factory() as session, session.begin():
        session.add(
            UserModel(
                id=owner_id,
                email=f"{owner_id}@example.test",
                password_hash="unused",
                display_name=None,
                status="active",
                role="user",
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        # 登录会话引用用户外键；先结算用户行，避免 SQLAlchemy 按无依赖顺序插入。
        await session.flush()
        session.add(
            AuthSessionModel(
                id=auth_session_id,
                user_id=owner_id,
                token_hash=hashlib.sha256(auth_session_id.encode()).hexdigest(),
                expires_at=now + timedelta(hours=1),
                revoked_time=None,
                created_at=now,
            )
        )
        session.add(
            ProjectModel(
                id=project_id,
                owner_id=owner_id,
                name=f"Project {owner_id}",
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        session.add(
            SessionModel(
                id=session_id,
                owner_id=owner_id,
                title=None,
                project_id=project_id,
                last_message_sequence=pending_count,
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        session.add(
            ModelProfileVersionModel(
                id=profile_id,
                name=profile_id,
                protocol="chat_completions",
                base_url="https://llm.example.test/v1",
                model="memory-lifecycle-test",
                api_key_ciphertext=b"memory-test-ciphertext",
                api_key_nonce=b"0" * 12,
                api_key_key_version=1,
                context_window_tokens=4096,
                tokenizer_encoding="o200k_base",
                supports_stream=True,
                supports_tools=True,
                supports_parallel_tool_calls=False,
                max_output_tokens=256,
                request_timeout_seconds=10,
                execution_timeout_seconds=30,
                max_concurrency=1,
                temperature=None,
                enabled=True,
                created_at=now,
            )
        )
        # 默认指针依赖模型版本外键；先结算父行，避免 ORM 按无关顺序先插指针。
        await session.flush()
        session.add(
            DefaultModelProfileModel(
                singleton_id=1,
                profile_id=profile_id,
            )
        )
        session.add(
            MemorySettingModel(
                namespace_id=owner_id,
                read_enabled=True,
                auto_extract_enabled=True,
                generation=generation,
            )
        )
        session.add_all(
            [
                MemoryScopeStateModel(
                    namespace_id=owner_id,
                    scope_key="personal",
                    enabled=True,
                    last_maintained_day=None,
                    created_at=now,
                    updated_at=now,
                ),
                MemoryScopeStateModel(
                    namespace_id=owner_id,
                    scope_key=f"project:{project_id}",
                    enabled=True,
                    last_maintained_day=None,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        await session.flush()

        for index, (run_id, message_id) in enumerate(
            zip(run_ids, message_ids, strict=True),
            start=1,
        ):
            source_at = now + timedelta(microseconds=index)
            message = ConversationMessageModel(
                id=message_id,
                session_id=session_id,
                role="user",
                status="completed",
                sequence=index,
                parts=[{"type": "text", "text": f"用户证据 {index}"}],
                reply_to_message_id=None,
                run_id=None,
                managed_run_id=None,
                context_turn_id=None,
                client_message_id=None,
                created_at=source_at,
                completed_at=source_at,
                metadata_json={},
            )
            session.add(message)
            await session.flush()
            session.add(
                RunModel(
                    id=run_id,
                    session_id=session_id,
                    owner_id=owner_id,
                    status="completed",
                    trigger_message_id=message_id,
                    context_cutoff_sequence=index,
                    retry_of_run_id=None,
                    model_config_version_id=profile_id,
                    main_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
                    compression_prompt_version_id=INITIAL_SYSTEM_PROMPT_IDS["compression"],
                    compression_policy_version_id=None,
                    skill_snapshot=None,
                    input_file_ids=[],
                    memory_project_id=project_id,
                    memory_input_message_ids=[message_id],
                    memory_context_state={
                        "initialized": True,
                        "references": [
                            {
                                "memory_id": f"memory_{owner_id}",
                                "version": 1,
                                "scope_key": "personal",
                            }
                        ],
                    },
                    memory_extraction_status="pending",
                    memory_extracted_at=None,
                    memory_extraction_batch_id=None,
                    memory_extraction_safe_error_code=None,
                    tool_snapshot=None,
                    auth_session_id=auth_session_id,
                    llm_request_count=0,
                    prompt_template_version="run-v2",
                    event_sequence=0,
                    execution_epoch=0,
                    worker_id=None,
                    lease_expires_at=None,
                    execution_started_at=None,
                    execution_deadline_at=None,
                    queued_at=source_at,
                    first_started_at=None,
                    finished_at=source_at,
                    created_at=source_at,
                    updated_at=source_at,
                    cancel_requested_at=None,
                    safe_error_code=None,
                    report_message_id=None,
                    usage={},
                )
            )
            await session.flush()
            message.run_id = run_id

    return HeadFixture(
        owner_id=owner_id,
        auth_session_id=auth_session_id,
        project_id=project_id,
        session_id=session_id,
        profile_id=profile_id,
        run_ids=run_ids,
        message_ids=message_ids,
        now=now,
    )


async def add_extract_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: str,
    job_id: str,
    status: JobStatus = JobStatus.QUEUED,
    now: datetime | None = None,
) -> None:
    """为指定用户写入一个不含候选正文的提取任务。"""
    created_at = now or datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add(
            MemoryJobModel(
                id=job_id,
                dedupe_key=f"extract_pending:{owner_id}",
                kind=JobKind.EXTRACT_PENDING.value,
                status=status.value,
                payload=ExtractPendingPayload(owner_id=owner_id).model_dump(mode="json"),
                result=None,
                namespace_id=owner_id,
                scope_key=None,
                attempt_count=0,
                schema_retry_count=0,
                available_at=created_at,
                created_at=created_at,
                updated_at=created_at,
            )
        )


def build_batch(
    fixture: HeadFixture,
    pending: tuple[MemoryPendingRun, ...],
    *,
    batch_id: str,
    candidates: tuple[MemoryCandidate, ...] = (),
    decisions: tuple[ExtractionDecision, ...] = (),
    skipped: int = 0,
) -> ExtractBatchPayload:
    """把当前待处理投影收敛为完整、类型化的模型批次。"""
    if not pending:
        raise ValueError("pending_runs_required")
    project_ids = {item.project_id for item in pending}
    scope_keys = ["personal"]
    if any(item.project_id is not None for item in pending):
        if len(project_ids) != 1 or None in project_ids:
            raise ValueError("pending_project_mixed")
        project_id = next(iter(project_ids))
        scope_keys.append(f"project:{project_id}")
    return ExtractBatchPayload(
        batch_id=batch_id,
        owner_id=fixture.owner_id,
        generation=pending[0].generation,
        members=tuple(
            ExtractBatchMember(
                run_id=item.run_id,
                session_id=item.session_id,
                project_id=item.project_id,
                source_message_ids=tuple(source.message_id for source in item.messages),
                source_cutoff_at=max(source.source_at for source in item.messages),
            )
            for item in pending
        ),
        model_profile_version_id=fixture.profile_id,
        prompt_version=2,
        scope_keys=tuple(scope_keys),
        created_at=fixture.now,
        candidates=candidates,
        extraction_complete=True,
        decisions=decisions,
        decisions_complete=True,
        skipped=skipped,
    )


def memory_repository(
    factory: async_sessionmaker[AsyncSession],
) -> MemoryRepository:
    """构造共享 PostgreSQL 工厂的记忆生命周期仓储。"""
    return MemoryRepository(factory)


__all__ = [
    "DATABASE_URL",
    "DatabaseHarness",
    "HeadFixture",
    "add_extract_job",
    "build_batch",
    "isolated_database",
    "memory_repository",
    "seed_head_fixture",
]
