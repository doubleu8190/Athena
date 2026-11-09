"""模型版本管理迁移和 PostgreSQL 仓储集成测试。"""

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
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.llm.model.profile import ModelProfileInput
from domain.llm.model.profile_errors import (
    ModelProfileAuthorizationError,
    ModelProfileNotFoundError,
)

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "backend" / "alembic.ini"
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
        schema = f"athena_v2_llm_profile_test_{token_hex(8)}"
        admin_engine = create_async_engine(database_url, poolclass=NullPool)
        test_engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            migration_config = Config(str(ALEMBIC_INI))
            migration_config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            migration_config.attributes["connect_args"] = {
                "server_settings": {"search_path": f"{schema},public"}
            }
            migration_config.attributes["version_table_schema"] = schema
            await asyncio.to_thread(command.upgrade, migration_config, "head")
            async with test_engine.connect() as connection:
                created_table = await connection.scalar(
                    text("SELECT to_regclass(current_schema() || '.llm_model_profile_versions')")
                )
            assert created_table == "llm_model_profile_versions"
            factory = async_sessionmaker(test_engine, expire_on_commit=False)
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
                            role="admin",
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
                    role=UserRole.ADMIN,
                    created_at=now,
                    updated_at=now,
                ),
                session_id="auth_llm_admin",
            )
            repository = SqlAlchemyModelProfileRepository(factory)
            value = ModelProfileInput(
                name="Integration model",
                base_url="https://llm.example/v1",
                model="test-model",
                credential_env="ATHENA_LLM_CREDENTIAL_INTEGRATION",
                context_window_tokens=8192,
                max_output_tokens=512,
            )
            monkeypatch.setenv(value.credential_env, "first-secret")
            created = await repository.create(actor, value)
            assert created.enabled is True
            assert "first-secret" not in repr(created)
            await repository.set_default(actor, created.id)
            assert await repository.get_default_id(actor) == created.id
            runtime = await repository.get_default_runtime()
            assert runtime is not None and runtime.api_key == "first-secret"
            unsupported = await repository.create(
                actor, replace(value, name="Unsupported model", supports_tools=False)
            )
            with pytest.raises(ModelProfileNotFoundError):
                await repository.set_default(actor, unsupported.id)
            unsupported_runtime = await repository.get_runtime(unsupported.id)
            assert unsupported_runtime is not None
            assert unsupported_runtime.version.supports_tools is False
            monkeypatch.setenv(value.credential_env, "rotated-secret")
            runtime = await repository.get_runtime(created.id)
            assert runtime is not None and runtime.api_key == "rotated-secret"
            await repository.set_enabled(actor, created.id, False)
            assert await repository.get_runtime(created.id) is None
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        update(UserModel).where(UserModel.id == "usr_llm_admin").values(role="user")
                    )
            with pytest.raises(ModelProfileAuthorizationError):
                user_actor = AuthContext(
                    user=User(
                        id="usr_llm_admin",
                        email="llm-admin@example.com",
                        password_hash="unused",
                        display_name=None,
                        status=UserStatus.ACTIVE,
                        role=UserRole.USER,
                        created_at=now,
                        updated_at=now,
                    ),
                    session_id="auth_llm_admin",
                )
                await repository.create(user_actor, value)
        finally:
            await test_engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin_engine.dispose()

    asyncio.run(scenario())
