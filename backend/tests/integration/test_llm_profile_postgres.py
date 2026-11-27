"""模型版本管理迁移和 PostgreSQL 仓储集成测试。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.llm.model import CompressionPolicyVersionModel, ModelProfileVersionModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.repository import SqlAlchemyRunRepository
from app.llm.profile_service import ModelProfileService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.llm.model.compression_policy import (
    MAX_COMPRESSION_TOKEN_BUDGET,
    CompressionPolicyInput,
)
from domain.llm.model.profile import ModelProfileInput
from domain.llm.model.profile_errors import (
    ModelProfileAuthorizationError,
    ModelProfileError,
    ModelProfileNotFoundError,
)
from domain.mcp import McpSecretError, McpSecretPurpose
from infrastructure.security.secrets import SecretCipher

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="ATHENA_TEST_DATABASE_URL is not configured"),
]


def test_model_profile_migration_admin_and_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证真实迁移、事务内管理员复核、默认指向及密钥轮换。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        async with isolated_postgres_schema(
            database_url, prefix="athena_v2_llm_profile_test"
        ) as db:
            async with db.engine.connect() as connection:
                created_table = await connection.scalar(
                    text("SELECT to_regclass(current_schema() || '.llm_model_profile_versions')")
                )
            assert created_table == "llm_model_profile_versions"
            factory = db.session_factory
            now = datetime.now(UTC)
            async with factory() as session:
                async with session.begin():
                    session.add(
                        UserModel(
                            id="usr_llm_admin",
                            email="llm-admin@example.com",
                            password_hash="unused",
                            display_name=None,
                            status="active",
                            role="super_admin",
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                    )
                    await session.flush()
                    session.add(
                        AuthSessionModel(
                            id="auth_llm_admin",
                            user_id="usr_llm_admin",
                            token_hash="f" * 64,
                            expires_at=now + timedelta(days=1),
                            revoked_time=None,
                            created_at=now,
                        )
                    )
            actor = AuthContext(
                user=User(
                    id="usr_llm_admin",
                    email="llm-admin@example.com",
                    password_hash="unused",
                    display_name=None,
                    status=UserStatus.ACTIVE,
                    role=UserRole.SUPER_ADMIN,
                    created_at=now,
                    updated_at=now,
                ),
                session_id="auth_llm_admin",
            )
            secret_cipher = SecretCipher(bytes(range(32)))
            repository = SqlAlchemyModelProfileRepository(factory, secret_cipher)
            value = ModelProfileInput(
                name="Integration model",
                base_url="https://llm.example/v1",
                model="test-model",
                api_key="first-secret",
                context_window_tokens=8192,
                max_output_tokens=512,
            )
            created = await repository.create(actor, value)
            assert created.enabled is True
            assert "first-secret" not in repr(created)
            await repository.set_default(actor, created.id)
            assert await repository.get_default_id(actor) == created.id
            runtime = await repository.get_default_runtime()
            assert runtime is not None and runtime.api_key == "first-secret"
            policy_input = CompressionPolicyInput(
                compression_model_profile_id=created.id,
                trigger_tokens=262_144,
                recent_ratio=0.2,
                recent_token_cap=32_768,
                summary_ratio=0.1,
                summary_token_cap=8_192,
            )

            class _SessionReader:
                async def get_current_session_user(
                    self, user_id: str, session_id: str, now: datetime
                ) -> User | None:
                    del now
                    if user_id == actor.user.id and session_id == actor.session_id:
                        return actor.user
                    return None

            profile_service = ModelProfileService(repository, _SessionReader())
            await profile_service.set_compression_policy(actor, policy_input)
            for field in ("trigger_tokens", "recent_token_cap", "summary_token_cap"):
                for invalid in (MAX_COMPRESSION_TOKEN_BUDGET + 1, 2**80):
                    if field == "trigger_tokens":
                        invalid_policy = replace(policy_input, trigger_tokens=invalid)
                    elif field == "recent_token_cap":
                        invalid_policy = replace(policy_input, recent_token_cap=invalid)
                    else:
                        invalid_policy = replace(policy_input, summary_token_cap=invalid)
                    with pytest.raises(ModelProfileError, match="llm_compression_policy_invalid"):
                        await profile_service.set_compression_policy(actor, invalid_policy)
            async with factory() as session:
                policy_count = await session.scalar(
                    select(func.count()).select_from(CompressionPolicyVersionModel)
                )
            assert policy_count == 1
            max_policy = await repository.set_compression_policy(
                actor,
                replace(
                    policy_input,
                    trigger_tokens=MAX_COMPRESSION_TOKEN_BUDGET,
                    recent_token_cap=MAX_COMPRESSION_TOKEN_BUDGET,
                    summary_token_cap=MAX_COMPRESSION_TOKEN_BUDGET,
                ),
            )
            assert max_policy.trigger_tokens == MAX_COMPRESSION_TOKEN_BUDGET
            assert max_policy.recent_token_cap == MAX_COMPRESSION_TOKEN_BUDGET
            assert max_policy.summary_token_cap == MAX_COMPRESSION_TOKEN_BUDGET
            await repository.set_enabled(actor, created.id, False)
            assert await repository.get_current_compression_policy() == max_policy
            run_repository = SqlAlchemyRunRepository(factory, SqlAlchemyMessageRepository(factory))
            async with factory() as session:
                assert await run_repository._current_compression_policy_id(session) == max_policy.id
            await repository.set_enabled(actor, created.id, True)
            assert await repository.get_current_compression_policy() == max_policy
            unsupported = await repository.create(
                actor, replace(value, name="Unsupported model", supports_tools=False)
            )
            with pytest.raises(ModelProfileNotFoundError):
                await repository.set_default(actor, unsupported.id)
            unsupported_runtime = await repository.get_runtime(unsupported.id)
            assert unsupported_runtime is not None
            assert unsupported_runtime.version.supports_tools is False
            # 数据库层已将加密凭据三元组收紧为 NOT NULL，置空必须被 PostgreSQL 拒绝。
            with pytest.raises(IntegrityError):
                async with factory() as session, session.begin():
                    await session.execute(
                        update(ModelProfileVersionModel)
                        .where(ModelProfileVersionModel.id == unsupported.id)
                        .values(
                            api_key_ciphertext=None,
                            api_key_nonce=None,
                            api_key_key_version=None,
                        )
                    )
            assert await repository.get_default_id(actor) == created.id
            runtime = await repository.get_runtime(created.id)
            assert runtime is not None and runtime.api_key == "first-secret"
            llm_sealed = secret_cipher.seal(
                created.id,
                McpSecretPurpose.LLM_API_KEY,
                {"api_key": "first-secret"},
            )
            with pytest.raises(McpSecretError):
                secret_cipher.open(
                    created.id,
                    McpSecretPurpose.HTTP_HEADERS,
                    llm_sealed.ciphertext,
                    llm_sealed.nonce,
                    llm_sealed.key_version,
                )
            other_key_repository = SqlAlchemyModelProfileRepository(
                factory, SecretCipher(bytes(reversed(range(32))))
            )
            assert await other_key_repository.get_runtime(created.id) is None
            other_version_repository = SqlAlchemyModelProfileRepository(
                factory, SecretCipher(bytes(range(32)), key_version=2)
            )
            assert await other_version_repository.get_runtime(created.id) is None
            await repository.set_enabled(actor, created.id, False)
            assert await repository.get_runtime(created.id) is None
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        update(UserModel)
                        .where(UserModel.id == "usr_llm_admin")
                        .values(role="member")
                    )
            with pytest.raises(ModelProfileAuthorizationError):
                user_actor = AuthContext(
                    user=User(
                        id="usr_llm_admin",
                        email="llm-admin@example.com",
                        password_hash="unused",
                        display_name=None,
                        status=UserStatus.ACTIVE,
                        role=UserRole.MEMBER,
                        created_at=now,
                        updated_at=now,
                    ),
                    session_id="auth_llm_admin",
                )
                await repository.create(user_actor, value)

    asyncio.run(scenario())
