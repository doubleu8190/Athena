"""系统提示词不可变版本、CAS、迁移回填与 Run 绑定的 PostgreSQL 测试。"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from integration.llm_fixtures import TEST_LLM_CIPHER, encrypted_llm_key_fields
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.llm.model.profile import (
    CompressionPolicyVersionModel,
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.model import ConversationMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.model import RunModel, SessionContextSummaryModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.system_prompt.model import SystemPromptVersionModel
from adapter.system_prompt.repository import (
    SqlAlchemySystemPromptRepository,
    current_system_prompts,
)
from app.run.config import RunWorkerConfig
from app.run.executor import RunExecutor
from app.run.worker import RunWorker
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.llm.model import (
    LlmRequest,
    LlmResponse,
    LlmStreamEvent,
    LlmText,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.message import (
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import AnswerRunCommand, Run, RunStatus, SubmitRunCommand
from domain.system_prompt import (
    INITIAL_COMPRESSION_SYSTEM_PROMPT,
    INITIAL_MAIN_SYSTEM_PROMPT,
    INITIAL_SYSTEM_PROMPT_IDS,
    SystemPromptAuthorizationError,
    SystemPromptConflictError,
    SystemPromptKind,
    SystemPromptNotFoundError,
    SystemPromptVersion,
)

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "backend" / "alembic.ini"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="ATHENA_TEST_DATABASE_URL is not configured"),
]


@asynccontextmanager
async def _schema() -> AsyncIterator[
    tuple[
        async_sessionmaker[AsyncSession],
        Config,
        str,
        AsyncEngine,
        AsyncEngine,
    ]
]:
    """创建隔离 schema 和迁移配置，结束时只删除本测试 schema。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    schema = f"athena_system_prompt_{token_hex(8)}"
    admin_engine = create_async_engine(database_url, poolclass=NullPool)
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["connect_args"] = {
        "server_settings": {"search_path": f"{schema},public"}
    }
    config.attributes["version_table_schema"] = schema
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        yield (
            async_sessionmaker(engine, expire_on_commit=False),
            config,
            schema,
            engine,
            admin_engine,
        )
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


def _auth_context(
    *,
    user_id: str,
    session_id: str,
    now: datetime,
    role: UserRole = UserRole.ADMIN,
) -> AuthContext:
    """构造带管理员或普通用户角色的认证上下文。"""
    return AuthContext(
        user=User(
            id=user_id,
            email=f"{user_id}@example.test",
            password_hash="unused",
            display_name=None,
            status=UserStatus.ACTIVE,
            role=role,
            created_at=now,
            updated_at=now,
        ),
        session_id=session_id,
    )


async def _seed_admins(
    factory: async_sessionmaker[AsyncSession],
    now: datetime,
) -> tuple[AuthContext, AuthContext, AuthContext]:
    """创建两名管理员和一名普通用户，每个上下文都有独立有效会话。"""
    contexts = (
        _auth_context(user_id="usr_prompt_admin_1", session_id="auth_prompt_admin_1", now=now),
        _auth_context(user_id="usr_prompt_admin_2", session_id="auth_prompt_admin_2", now=now),
        _auth_context(
            user_id="usr_prompt_user",
            session_id="auth_prompt_user",
            now=now,
            role=UserRole.USER,
        ),
    )
    async with factory() as session, session.begin():
        for actor in contexts:
            session.add(
                UserModel(
                    id=actor.user.id,
                    email=actor.user.email,
                    password_hash=actor.user.password_hash,
                    display_name=None,
                    status=actor.user.status.value,
                    role=actor.user.role.value,
                    created_at=now,
                    updated_at=now,
                    deleted_time=None,
                )
            )
        await session.flush()
        for actor in contexts:
            session.add(
                AuthSessionModel(
                    id=actor.session_id,
                    user_id=actor.user.id,
                    token_hash=token_hex(32),
                    expires_at=now + timedelta(days=1),
                    revoked_time=None,
                    created_at=now,
                )
            )
    return contexts


async def _seed_session_and_model(
    factory: async_sessionmaker[AsyncSession],
    owner_id: str,
    session_id: str,
    now: datetime,
) -> None:
    """创建 Run 创建事务所需的 Session 和默认模型版本。"""
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
        session.add(
            ModelProfileVersionModel(
                id="llmprof_prompt",
                name="Prompt integration",
                protocol="chat_completions",
                base_url="https://llm.example.test/v1",
                model="prompt-integration",
                **encrypted_llm_key_fields("llmprof_prompt"),
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
            DefaultModelProfileModel(
                singleton_id=1,
                profile_id="llmprof_prompt",
            )
        )


def _submit_command(
    *,
    owner_id: str,
    session_id: str,
    now: datetime,
    run_id: str = "run_prompt_bound",
    request_id: str = "request_prompt_bound",
) -> SubmitRunCommand:
    """构造一条可提交的用户消息及其幂等请求标识。"""
    return SubmitRunCommand(
        run_id=run_id,
        request_id=request_id,
        fingerprint="a" * 64,
        prompt_template_version="run-v2",
        message=ConversationMessageDraft(
            id=f"msg_{run_id}",
            session_id=session_id,
            owner_id=owner_id,
            role=ConversationRole.USER,
            status=MessageStatus.COMPLETED,
            parts=(TextPart("bind immutable prompts"),),
            reply_to_message_id=None,
            run_id=run_id,
            client_message_id=None,
            created_at=now,
            completed_at=now,
            metadata=MessageMetadata(),
        ),
        queued_at=now,
    )


async def _seed_prompt_closure_context(
    factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: str,
    session_id: str,
    now: datetime,
) -> None:
    """为真实压缩路径补充压缩模型、策略和超过触发阈值的既有历史。"""
    compression_profile_id = "llmprof_prompt_compression"
    policy_id = "cmpol_prompt_closure"
    async with factory() as session, session.begin():
        session.add(
            ModelProfileVersionModel(
                id=compression_profile_id,
                name="Prompt closure compression",
                protocol="chat_completions",
                base_url="https://llm.example.test/v1",
                model="prompt-closure-summary",
                **encrypted_llm_key_fields(compression_profile_id),
                context_window_tokens=8192,
                tokenizer_encoding="o200k_base",
                supports_stream=False,
                supports_tools=False,
                max_output_tokens=128,
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
            CompressionPolicyVersionModel(
                id=policy_id,
                compression_model_profile_id=compression_profile_id,
                trigger_ratio=0.4,
                trigger_tokens=1_024,
                recent_ratio=0.2,
                recent_token_cap=2_048,
                summary_ratio=0.1,
                summary_token_cap=1_024,
                prompt_template_version="context-summary-v1",
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            CurrentCompressionPolicyModel(
                singleton_id=1,
                policy_version_id=policy_id,
            )
        )
        _add_compression_history(
            session,
            session_id=session_id,
            id_prefix="msg_prompt_closure_history",
            now=now,
        )
        locked_session = await session.get(SessionModel, session_id)
        assert locked_session is not None
        locked_session.last_message_sequence = 2
        locked_session.updated_at = now


def _add_compression_history(
    session: AsyncSession,
    *,
    session_id: str,
    id_prefix: str,
    now: datetime,
) -> None:
    """向指定会话加入可稳定超过压缩触发阈值的两轮历史。"""
    history = (
        (
            f"{id_prefix}_user",
            "user",
            "Plan a herb garden with light, soil, watering and seasonal care. " * 150,
            None,
        ),
        (
            f"{id_prefix}_assistant",
            "assistant",
            "Group herbs by sunlight and water only after checking the top soil. " * 150,
            f"{id_prefix}_user",
        ),
    )
    for sequence, (message_id, role, content, reply_to_message_id) in enumerate(
        history,
        start=1,
    ):
        session.add(
            ConversationMessageModel(
                id=message_id,
                session_id=session_id,
                role=role,
                status="completed",
                sequence=sequence,
                parts=[{"type": "text", "text": content}],
                reply_to_message_id=reply_to_message_id,
                run_id=None,
                managed_run_id=None,
                context_turn_id=None,
                client_message_id=None,
                created_at=now,
                completed_at=now,
                metadata_json={},
            )
        )


class _PromptCaptureProvider:
    """分别记录真实压缩和主模型请求，并按脚本返回主回答。"""

    def __init__(self, main_responses: list[LlmResponse]) -> None:
        self._main_responses = main_responses
        self.main_requests: list[LlmRequest] = []
        self.compression_requests: list[LlmRequest] = []

    async def complete(
        self,
        runtime: RuntimeModelProfile,
        request: LlmRequest,
    ) -> LlmResponse:
        """记录压缩请求并返回稳定的纯文本摘要。"""
        self.compression_requests.append(request)
        return LlmResponse(
            output=(LlmText(f"summary {len(self.compression_requests)}"),),
            status="completed",
            incomplete_reason=None,
            model=runtime.version.model,
            response_id=f"summary-{len(self.compression_requests)}",
            request_id=None,
            usage=LlmUsage(input_tokens=20, output_tokens=3, total_tokens=23),
        )

    @asynccontextmanager
    async def stream(
        self,
        runtime: RuntimeModelProfile,
        request: LlmRequest,
    ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
        """记录主请求并流式交付一条完整的脚本化响应。"""
        self.main_requests.append(request)
        if not self._main_responses:
            raise AssertionError("unexpected main model request")
        response = self._main_responses.pop(0)

        async def events() -> AsyncIterator[LlmStreamEvent]:
            yield ResponseCompleted(response)

        yield events()


def _build_prompt_worker(
    factory: async_sessionmaker[AsyncSession],
    repository: SqlAlchemyRunRepository,
    provider: _PromptCaptureProvider,
    worker_id: str,
) -> RunWorker:
    """完全从持久化依赖重新装配 Worker 与 Executor。"""
    config = RunWorkerConfig(
        concurrency=1,
        lease_seconds=10,
        heartbeat_seconds=1,
        execution_seconds=30,
        cancel_poll_seconds=0.02,
        queue_poll_seconds=0.02,
        reclaim_seconds=0.2,
    )
    executor = RunExecutor(
        repository,
        SqlAlchemyModelProfileRepository(factory, TEST_LLM_CIPHER),
        provider,
        system_prompts=SqlAlchemySystemPromptRepository(factory),
        config=config,
    )
    return RunWorker(
        repository,
        executor,
        config,
        worker_id=worker_id,
    )


async def _wait_for_run_status(
    repository: SqlAlchemyRunRepository,
    session_id: str,
    owner_id: str,
    run_id: str,
    expected: RunStatus,
    *,
    timeout_seconds: float = 15,
) -> Run:
    """等待数据库中的真实状态收敛到预期值。"""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        run = await repository.get(session_id, owner_id, run_id)
        if run is not None and run.status is expected:
            return run
        await asyncio.sleep(0.02)
    current = await repository.get(session_id, owner_id, run_id)
    raise AssertionError(
        f"run {run_id} did not reach {expected.value}: "
        f"{None if current is None else current.status.value}"
    )


def test_system_prompt_migration_backfills_legacy_run_and_summary() -> None:
    """从旧 revision 升级时初始化两类原文并回填既有 Run 和摘要。"""

    async def scenario() -> None:
        async with _schema() as (_factory, config, schema, engine, _admin_engine):
            await asyncio.to_thread(command.upgrade, config, "20261115_0016")
            now = datetime.now(UTC)
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id,email,password_hash,display_name,status,role,created_at,"
                        "updated_at,deleted_time) VALUES "
                        "('usr_legacy_prompt','legacy-prompt@example.test','unused',NULL,"
                        "'active','user',:now,:now,NULL)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO sessions "
                        "(id,owner_id,title,project_id,last_message_sequence,created_at,"
                        "updated_at,deleted_time) VALUES "
                        "('ses_legacy_prompt','usr_legacy_prompt',NULL,NULL,2,:now,:now,NULL)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO llm_model_profile_versions "
                        "(id,name,protocol,base_url,model,api_key_ciphertext,api_key_nonce,"
                        "api_key_key_version,context_window_tokens,tokenizer_encoding,"
                        "supports_stream,supports_tools,supports_parallel_tool_calls,"
                        "max_output_tokens,request_timeout_seconds,execution_timeout_seconds,"
                        "max_concurrency,temperature,enabled,created_at) VALUES "
                        "('llmprof_legacy_prompt','Legacy','chat_completions',"
                        "'https://legacy.example/v1','legacy',NULL,NULL,NULL,8192,"
                        "'o200k_base',true,true,false,512,30,60,1,NULL,false,:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO conversation_messages "
                        "(id,session_id,role,status,sequence,parts,reply_to_message_id,run_id,"
                        "managed_run_id,context_turn_id,client_message_id,created_at,"
                        "completed_at,metadata) VALUES "
                        "('msg_legacy_prompt','ses_legacy_prompt','user','completed',2,"
                        "CAST(:parts AS jsonb),NULL,'run_legacy_prompt',NULL,NULL,NULL,"
                        ":now,:now,CAST('{}' AS jsonb))"
                    ),
                    {
                        "parts": json.dumps([{"type": "text", "text": "legacy"}]),
                        "now": now,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO llm_compression_policy_versions "
                        "(id,compression_model_profile_id,trigger_ratio,recent_ratio,"
                        "summary_ratio,prompt_template_version,created_at) VALUES "
                        "('cmpol_legacy_prompt','llmprof_legacy_prompt',0.8,0.2,0.1,"
                        "'context-summary-v1',:now)"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO runs "
                        "(id,session_id,owner_id,status,trigger_message_id,"
                        "context_cutoff_sequence,retry_of_run_id,model_config_version_id,"
                        "compression_policy_version_id,prompt_template_version,event_sequence,"
                        "execution_epoch,queued_at,created_at,updated_at,usage) VALUES "
                        "('run_legacy_prompt','ses_legacy_prompt','usr_legacy_prompt','failed',"
                        "'msg_legacy_prompt',2,NULL,'llmprof_legacy_prompt',"
                        "'cmpol_legacy_prompt','run-v2',0,1,:now,:now,:now,"
                        "CAST('{}' AS jsonb))"
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO session_context_summaries "
                        "(id,session_id,owner_id,covered_through_sequence,parent_summary_id,"
                        "text,run_id,execution_epoch,operation_id,policy_version_id,"
                        "compression_model_version_id,prompt_version,created_at) VALUES "
                        "('summary_legacy_prompt','ses_legacy_prompt','usr_legacy_prompt',1,"
                        "NULL,'legacy summary','run_legacy_prompt',1,'cmpop_legacy_prompt',"
                        "'cmpol_legacy_prompt','llmprof_legacy_prompt',"
                        "'context-summary-v1',:now)"
                    ),
                    {"now": now},
                )

            await asyncio.to_thread(command.upgrade, config, "head")
            async with engine.connect() as connection:
                run_prompts = (
                    await connection.execute(
                        text(
                            "SELECT main_prompt_version_id, compression_prompt_version_id,"
                            "prompt_template_version FROM runs "
                            "WHERE id = 'run_legacy_prompt'"
                        )
                    )
                ).one()
                summary_prompts = (
                    await connection.execute(
                        text(
                            "SELECT compression_prompt_version_id, prompt_version "
                            "FROM session_context_summaries "
                            "WHERE id = 'summary_legacy_prompt'"
                        )
                    )
                ).one()
                settings: dict[str, str] = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT kind, current_version_id "
                                "FROM system_prompt_settings"
                            )
                        )
                    ).all()
                )
                contents: dict[str, str] = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT kind, content FROM system_prompt_versions "
                                "WHERE id IN (:main_id, :compression_id)"
                            ),
                            {
                                "main_id": INITIAL_SYSTEM_PROMPT_IDS["main"],
                                "compression_id": INITIAL_SYSTEM_PROMPT_IDS["compression"],
                            },
                        )
                    ).all()
                )
                assert tuple(run_prompts) == (
                    INITIAL_SYSTEM_PROMPT_IDS["main"],
                    INITIAL_SYSTEM_PROMPT_IDS["compression"],
                    "run-v2",
                )
                assert tuple(summary_prompts) == (
                    INITIAL_SYSTEM_PROMPT_IDS["compression"],
                    "context-summary-v1",
                )
                assert settings == INITIAL_SYSTEM_PROMPT_IDS
                assert contents == {
                    "main": INITIAL_MAIN_SYSTEM_PROMPT,
                    "compression": INITIAL_COMPRESSION_SYSTEM_PROMPT,
                }
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE runs SET main_prompt_version_id = NULL "
                            "WHERE id = 'run_legacy_prompt'"
                        )
                    )

    asyncio.run(scenario())


def test_system_prompt_repository_cas_restore_rollback_and_run_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 PostgreSQL 验证 CAS、回滚、同正文恢复、权限和 Run 创建绑定。"""

    async def scenario() -> None:
        async with _schema() as (factory, config, _schema_name, _engine, _admin_engine):
            await asyncio.to_thread(command.upgrade, config, "head")
            now = datetime.now(UTC)
            admin_one, admin_two, _ = await _seed_admins(factory, now)
            await _seed_session_and_model(
                factory,
                admin_one.user.id,
                "ses_prompt_binding",
                now,
            )
            repository = SqlAlchemySystemPromptRepository(factory)

            initial = await repository.get_current(SystemPromptKind.MAIN)
            assert initial is not None
            same = await repository.save(
                SystemPromptKind.MAIN,
                content=initial.content,
                expected_current_version_id=initial.id,
                actor=admin_one,
                created_at=now,
            )
            assert same.id == initial.id
            async with factory() as session:
                count = await session.scalar(
                    select(func.count(SystemPromptVersionModel.id)).where(
                        SystemPromptVersionModel.kind == "main"
                    )
                )
            assert count == 1

            baseline = await repository.save(
                SystemPromptKind.MAIN,
                content="baseline\n",
                expected_current_version_id=initial.id,
                actor=admin_one,
                created_at=now + timedelta(microseconds=1),
            )
            race_results = await asyncio.gather(
                repository.save(
                    SystemPromptKind.MAIN,
                    content="admin one wins",
                    expected_current_version_id=baseline.id,
                    actor=admin_one,
                    created_at=now + timedelta(seconds=1),
                ),
                repository.save(
                    SystemPromptKind.MAIN,
                    content="admin two wins",
                    expected_current_version_id=baseline.id,
                    actor=admin_two,
                    created_at=now + timedelta(seconds=1),
                ),
                return_exceptions=True,
            )
            winners = [
                item for item in race_results if isinstance(item, SystemPromptVersion)
            ]
            conflicts = [
                item for item in race_results if isinstance(item, SystemPromptConflictError)
            ]
            assert len(winners) == 1
            assert len(conflicts) == 1
            current = await repository.get_current(SystemPromptKind.MAIN)
            assert current is not None and current.id == winners[0].id

            prompt_repository_module = cast(
                Any,
                importlib.import_module(
                    "adapter.system_prompt.repository.system_prompt"
                ),
            )

            original_new_id = prompt_repository_module.new_id
            monkeypatch.setattr(prompt_repository_module, "new_id", lambda _: current.id)
            with pytest.raises(IntegrityError):
                await repository.save(
                    SystemPromptKind.MAIN,
                    content="must roll back",
                    expected_current_version_id=current.id,
                    actor=admin_one,
                    created_at=now + timedelta(seconds=2),
                )
            monkeypatch.setattr(prompt_repository_module, "new_id", original_new_id)
            rolled_back = await repository.get_current(SystemPromptKind.MAIN)
            assert rolled_back is not None
            assert rolled_back.id == current.id
            assert rolled_back.content == current.content

            restored = await repository.restore(
                SystemPromptKind.MAIN,
                source_version_id=initial.id,
                expected_current_version_id=current.id,
                actor=admin_one,
                created_at=now + timedelta(seconds=3),
            )
            assert restored.id != current.id
            assert restored.content == initial.content
            assert restored.restored_from_version_id == initial.id
            restored_same_content = await repository.restore(
                SystemPromptKind.MAIN,
                source_version_id=initial.id,
                expected_current_version_id=restored.id,
                actor=admin_one,
                created_at=now + timedelta(seconds=4),
            )
            assert restored_same_content.id not in {current.id, restored.id}
            assert restored_same_content.content == initial.content
            assert restored_same_content.restored_from_version_id == initial.id

            compression_initial = await repository.get_current(
                SystemPromptKind.COMPRESSION
            )
            assert compression_initial is not None
            with pytest.raises(SystemPromptNotFoundError):
                await repository.restore(
                    SystemPromptKind.MAIN,
                    source_version_id=compression_initial.id,
                    expected_current_version_id=restored_same_content.id,
                    actor=admin_one,
                    created_at=now + timedelta(seconds=5),
                )
            assert (
                await repository.get_pair_by_ids(
                    compression_initial.id,
                    compression_initial.id,
                )
                is None
            )

            run_repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            submit = _submit_command(
                owner_id=admin_one.user.id,
                session_id="ses_prompt_binding",
                now=now + timedelta(seconds=6),
            )
            submitted = await run_repository.submit(
                "ses_prompt_binding",
                admin_one.user.id,
                submit,
            )
            assert submitted.run.main_prompt_version_id == restored_same_content.id
            assert submitted.run.compression_prompt_version_id == compression_initial.id

            main_after_submit = await repository.save(
                SystemPromptKind.MAIN,
                content="new main after submit",
                expected_current_version_id=restored_same_content.id,
                actor=admin_one,
                created_at=now + timedelta(seconds=7),
            )
            compression_after_submit = await repository.save(
                SystemPromptKind.COMPRESSION,
                content="new compression after submit",
                expected_current_version_id=compression_initial.id,
                actor=admin_one,
                created_at=now + timedelta(seconds=7),
            )
            replay = await run_repository.submit(
                "ses_prompt_binding",
                admin_one.user.id,
                submit,
            )
            assert replay.replayed is True
            assert replay.run.id == submitted.run.id
            assert replay.run.main_prompt_version_id == restored_same_content.id
            assert replay.run.compression_prompt_version_id == compression_initial.id

            async with factory() as session, session.begin():
                await session.execute(
                    update(RunModel)
                    .where(RunModel.id == submitted.run.id)
                    .values(
                        status=RunStatus.FAILED.value,
                        finished_at=now + timedelta(seconds=8),
                        updated_at=now + timedelta(seconds=8),
                    )
                )
            retried = await run_repository.retry(
                "ses_prompt_binding",
                admin_one.user.id,
                submitted.run.id,
                "request_prompt_retry",
                "b" * 64,
                "run_prompt_retry",
                "run-v2",
                now + timedelta(seconds=8),
            )
            assert retried is not None
            assert retried.main_prompt_version_id == main_after_submit.id
            assert retried.compression_prompt_version_id == compression_after_submit.id

            main_after_retry = await repository.save(
                SystemPromptKind.MAIN,
                content="newest main",
                expected_current_version_id=main_after_submit.id,
                actor=admin_one,
                created_at=now + timedelta(seconds=9),
            )
            compression_after_retry = await repository.save(
                SystemPromptKind.COMPRESSION,
                content="newest compression",
                expected_current_version_id=compression_after_submit.id,
                actor=admin_one,
                created_at=now + timedelta(seconds=9),
            )
            retry_replay = await run_repository.retry(
                "ses_prompt_binding",
                admin_one.user.id,
                submitted.run.id,
                "request_prompt_retry",
                "b" * 64,
                "run_prompt_retry_replay_must_not_create",
                "run-v2",
                now + timedelta(seconds=10),
            )
            assert retry_replay is not None and retry_replay.id == retried.id
            assert retry_replay.main_prompt_version_id == main_after_submit.id
            assert retry_replay.compression_prompt_version_id == compression_after_submit.id

            async with factory() as session:
                prompts = await current_system_prompts(session)
            assert prompts is not None
            assert prompts.main.id == main_after_retry.id
            assert prompts.compression.id == compression_after_retry.id

            async with factory() as session, session.begin():
                await session.execute(
                    update(UserModel)
                    .where(UserModel.id == admin_one.user.id)
                    .values(role=UserRole.USER.value)
                )
            with pytest.raises(SystemPromptAuthorizationError):
                await repository.save(
                    SystemPromptKind.MAIN,
                    content="demoted admin must fail",
                    expected_current_version_id=main_after_retry.id,
                    actor=replace(
                        admin_one,
                        user=replace(admin_one.user, role=UserRole.ADMIN),
                    ),
                    created_at=now + timedelta(seconds=11),
                )
    asyncio.run(scenario())


def test_waiting_run_resumes_with_original_prompts_and_new_run_uses_new_versions() -> None:
    """跨 Worker 执行与澄清恢复时，实际请求和摘要继续使用 Run 创建时绑定的正文。"""

    async def scenario() -> None:
        async with _schema() as (factory, config, _schema_name, _engine, _admin_engine):
            await asyncio.to_thread(command.upgrade, config, "head")
            now = datetime.now(UTC)
            admin, _, _ = await _seed_admins(factory, now)
            session_id = "ses_prompt_closure"
            await _seed_session_and_model(
                factory,
                admin.user.id,
                session_id,
                now,
            )
            await _seed_prompt_closure_context(
                factory,
                owner_id=admin.user.id,
                session_id=session_id,
                now=now,
            )

            prompt_repository = SqlAlchemySystemPromptRepository(factory)
            main_a = await prompt_repository.get_current(SystemPromptKind.MAIN)
            compression_a = await prompt_repository.get_current(
                SystemPromptKind.COMPRESSION
            )
            assert main_a is not None and compression_a is not None

            submit_repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            submitted_a = await submit_repository.submit(
                session_id,
                admin.user.id,
                _submit_command(
                    owner_id=admin.user.id,
                    session_id=session_id,
                    now=now,
                    run_id="run_prompt_closure_a",
                    request_id="request_prompt_closure_a",
                ),
            )
            assert submitted_a.run.main_prompt_version_id == main_a.id
            assert submitted_a.run.compression_prompt_version_id == compression_a.id

            main_b = await prompt_repository.save(
                SystemPromptKind.MAIN,
                content="MAIN PROMPT B",
                expected_current_version_id=main_a.id,
                actor=admin,
                created_at=now + timedelta(microseconds=1),
            )
            compression_b = await prompt_repository.save(
                SystemPromptKind.COMPRESSION,
                content="COMPRESSION PROMPT B",
                expected_current_version_id=compression_a.id,
                actor=admin,
                created_at=now + timedelta(microseconds=2),
            )

            waiting_provider = _PromptCaptureProvider(
                [
                    LlmResponse(
                        output=(
                            LlmToolCall(
                                "call_prompt_closure_clarify",
                                "request_user_input",
                                {"question": "Which climate should the garden plan assume?"},
                            ),
                        ),
                        status="completed",
                        incomplete_reason=None,
                        model="prompt-integration",
                        response_id="prompt-closure-clarify",
                        request_id=None,
                        usage=LlmUsage(input_tokens=50, output_tokens=8, total_tokens=58),
                    )
                ]
            )
            waiting_repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            waiting_worker = _build_prompt_worker(
                factory,
                waiting_repository,
                waiting_provider,
                "worker_prompt_closure_waiting",
            )
            waiting_task = asyncio.create_task(waiting_worker.run())
            try:
                waiting_run = await _wait_for_run_status(
                    waiting_repository,
                    session_id,
                    admin.user.id,
                    submitted_a.run.id,
                    RunStatus.WAITING_FOR_INPUT,
                )
            finally:
                await waiting_worker.stop()
                await asyncio.wait_for(waiting_task, timeout=5)

            # 当前已切换为 B，但首次 Worker 的实际主请求与压缩请求都必须仍使用 A。
            assert waiting_run.main_prompt_version_id == main_a.id
            assert waiting_run.compression_prompt_version_id == compression_a.id
            assert len(waiting_provider.main_requests) == 1
            assert waiting_provider.main_requests[0].messages[0].content == main_a.content
            assert waiting_provider.compression_requests
            assert (
                waiting_provider.compression_requests[0].messages[0].content
                == compression_a.content
            )
            async with factory() as session:
                persisted_summary = await session.scalar(
                    select(SessionContextSummaryModel).where(
                        SessionContextSummaryModel.run_id == submitted_a.run.id
                    )
                )
            assert persisted_summary is not None
            assert persisted_summary.compression_prompt_version_id == compression_a.id

            pending = await submit_repository.get_pending_input(
                session_id,
                admin.user.id,
                submitted_a.run.id,
            )
            assert pending is not None
            input_call = await submit_repository.get_input_call(
                session_id,
                admin.user.id,
                submitted_a.run.id,
                pending.id,
            )
            assert input_call is not None
            answer_at = datetime.now(UTC)
            await submit_repository.answer(
                session_id,
                admin.user.id,
                AnswerRunCommand(
                    run_id=submitted_a.run.id,
                    input_request_id=pending.id,
                    request_id="request_prompt_closure_answer",
                    fingerprint="c" * 64,
                    answer_message=ConversationMessageDraft(
                        id="msg_prompt_closure_answer",
                        session_id=session_id,
                        owner_id=admin.user.id,
                        role=ConversationRole.USER,
                        status=MessageStatus.COMPLETED,
                        parts=(TextPart("Use a temperate climate."),),
                        reply_to_message_id=None,
                        run_id=submitted_a.run.id,
                        client_message_id=None,
                        created_at=answer_at,
                        completed_at=answer_at,
                        metadata=MessageMetadata(),
                        context_turn_id=submitted_a.run.trigger_message_id,
                    ),
                    execution_message=ExecutionMessageDraft(
                        id="exec_prompt_closure_answer",
                        session_id=session_id,
                        owner_id=admin.user.id,
                        run_id=submitted_a.run.id,
                        thread_id=input_call.thread_id,
                        role=ExecutionRole.TOOL,
                        parts=(
                            ToolResultPart(
                                input_call.tool_call_id,
                                ToolResultStatus.SUCCESS,
                                "Use a temperate climate.",
                            ),
                        ),
                        tool_call_id=input_call.tool_call_id,
                        source_message_id=None,
                        created_at=answer_at,
                        metadata=MessageMetadata(),
                    ),
                    queued_at=answer_at,
                    auth_session_id=admin.session_id,
                ),
            )

            resumed_provider = _PromptCaptureProvider(
                [
                    LlmResponse(
                        output=(LlmText("temperate garden plan complete"),),
                        status="completed",
                        incomplete_reason=None,
                        model="prompt-integration",
                        response_id="prompt-closure-resumed",
                        request_id=None,
                        usage=LlmUsage(input_tokens=40, output_tokens=5, total_tokens=45),
                    )
                ]
            )
            resumed_repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            resumed_worker = _build_prompt_worker(
                factory,
                resumed_repository,
                resumed_provider,
                "worker_prompt_closure_resumed",
            )
            resumed_task = asyncio.create_task(resumed_worker.run())
            try:
                completed_a = await _wait_for_run_status(
                    resumed_repository,
                    session_id,
                    admin.user.id,
                    submitted_a.run.id,
                    RunStatus.COMPLETED,
                )
            finally:
                await resumed_worker.stop()
                await asyncio.wait_for(resumed_task, timeout=5)
            assert completed_a.main_prompt_version_id == main_a.id
            assert completed_a.compression_prompt_version_id == compression_a.id
            assert len(resumed_provider.main_requests) == 1
            assert resumed_provider.main_requests[0].messages[0].content == main_a.content

            new_session_id = "ses_prompt_closure_new"
            async with factory() as session, session.begin():
                session.add(
                    SessionModel(
                        id=new_session_id,
                        owner_id=admin.user.id,
                        title=None,
                        project_id=None,
                        last_message_sequence=2,
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                )
                await session.flush()
                _add_compression_history(
                    session,
                    session_id=new_session_id,
                    id_prefix="msg_prompt_closure_new_history",
                    now=now,
                )
            submit_repository_b = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            submitted_b = await submit_repository_b.submit(
                new_session_id,
                admin.user.id,
                _submit_command(
                    owner_id=admin.user.id,
                    session_id=new_session_id,
                    now=datetime.now(UTC),
                    run_id="run_prompt_closure_b",
                    request_id="request_prompt_closure_b",
                ),
            )
            assert submitted_b.run.main_prompt_version_id == main_b.id
            assert submitted_b.run.compression_prompt_version_id == compression_b.id

            new_run_provider = _PromptCaptureProvider(
                [
                    LlmResponse(
                        output=(LlmText("new prompt run complete"),),
                        status="completed",
                        incomplete_reason=None,
                        model="prompt-integration",
                        response_id="prompt-closure-new-run",
                        request_id=None,
                        usage=LlmUsage(input_tokens=30, output_tokens=4, total_tokens=34),
                    )
                ]
            )
            new_run_repository = SqlAlchemyRunRepository(
                factory,
                SqlAlchemyMessageRepository(factory),
            )
            new_run_worker = _build_prompt_worker(
                factory,
                new_run_repository,
                new_run_provider,
                "worker_prompt_closure_new_run",
            )
            new_run_task = asyncio.create_task(new_run_worker.run())
            try:
                completed_b = await _wait_for_run_status(
                    new_run_repository,
                    new_session_id,
                    admin.user.id,
                    submitted_b.run.id,
                    RunStatus.COMPLETED,
                )
            finally:
                await new_run_worker.stop()
                await asyncio.wait_for(new_run_task, timeout=5)
            assert completed_b.main_prompt_version_id == main_b.id
            assert completed_b.compression_prompt_version_id == compression_b.id
            assert len(new_run_provider.main_requests) == 1
            assert new_run_provider.main_requests[0].messages[0].content == main_b.content
            assert new_run_provider.compression_requests
            assert (
                new_run_provider.compression_requests[0].messages[0].content
                == compression_b.content
            )
            async with factory() as session:
                new_summary = await session.scalar(
                    select(SessionContextSummaryModel).where(
                        SessionContextSummaryModel.run_id == submitted_b.run.id
                    )
                )
            assert new_summary is not None
            assert new_summary.compression_prompt_version_id == compression_b.id

    asyncio.run(scenario())
