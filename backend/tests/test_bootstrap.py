"""Bootstrap 组合根的环境解析与资源构造测试。"""

from __future__ import annotations

from typing import Any

import pytest

import bootstrap.api as bootstrap_api
import bootstrap.database as bootstrap_database
import bootstrap.knowledge as bootstrap_knowledge
import bootstrap.redis as bootstrap_redis
import bootstrap.tool as bootstrap_tool
from app.auth.rate_limit import RedisFixedWindowRateLimiter
from infrastructure.database import (
    DEFAULT_DATABASE_MAX_OVERFLOW,
    DEFAULT_DATABASE_POOL_SIZE,
)
from infrastructure.embedding import (
    EmbeddingConfigurationError,
    OpenAICompatibleEmbeddingConfig,
    remote_embedding_profile_id,
)


def test_build_database_passes_pool_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    class FakeDatabase:
        def __init__(self, url: str, *, pool_size: int, max_overflow: int) -> None:
            recorded.update(url=url, pool_size=pool_size, max_overflow=max_overflow)

    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setenv("ATHENA_DATABASE_POOL_SIZE", "7")
    monkeypatch.setenv("ATHENA_DATABASE_MAX_OVERFLOW", "3")

    bootstrap_database.build_database("postgresql+asyncpg://db.invalid/athena")

    assert recorded == {
        "url": "postgresql+asyncpg://db.invalid/athena",
        "pool_size": 7,
        "max_overflow": 3,
    }


def test_build_database_uses_default_pool_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    class FakeDatabase:
        def __init__(self, url: str, *, pool_size: int, max_overflow: int) -> None:
            recorded.update(url=url, pool_size=pool_size, max_overflow=max_overflow)

    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.delenv("ATHENA_DATABASE_POOL_SIZE", raising=False)
    monkeypatch.delenv("ATHENA_DATABASE_MAX_OVERFLOW", raising=False)

    bootstrap_database.build_database("postgresql+asyncpg://db.invalid/athena")

    assert recorded["pool_size"] == DEFAULT_DATABASE_POOL_SIZE
    assert recorded["max_overflow"] == DEFAULT_DATABASE_MAX_OVERFLOW


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("ATHENA_DATABASE_POOL_SIZE", "0"),
        ("ATHENA_DATABASE_POOL_SIZE", "not-a-number"),
        ("ATHENA_DATABASE_MAX_OVERFLOW", "-1"),
        ("ATHENA_DATABASE_MAX_OVERFLOW", "not-a-number"),
    ),
)
def test_build_database_rejects_invalid_pool_configuration(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    class FakeDatabase:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("invalid configuration must fail before construction")

    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name}_invalid"):
        bootstrap_database.build_database("postgresql+asyncpg://db.invalid/athena")


def test_build_run_transport_returns_none_without_redis_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATHENA_REDIS_URL", raising=False)
    assert bootstrap_redis.build_run_transport() is None

    monkeypatch.setenv("ATHENA_REDIS_URL", "   ")
    assert bootstrap_redis.build_run_transport() is None


def test_build_run_transport_uses_binary_redis_client(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}
    client = object()

    def fake_from_url(url: str, **options: Any) -> object:
        recorded.update(url=url, **options)
        return client

    class FakeTransport:
        def __init__(self, redis: object) -> None:
            self.redis = redis

    monkeypatch.setenv("ATHENA_REDIS_URL", "redis://cache.invalid:6379/0")
    monkeypatch.setattr("bootstrap.redis.Redis.from_url", fake_from_url)
    monkeypatch.setattr(bootstrap_redis, "RedisRunTransport", FakeTransport)

    transport = bootstrap_redis.build_run_transport()

    assert recorded == {
        "url": "redis://cache.invalid:6379/0",
        "decode_responses": False,
        "socket_connect_timeout": 1,
    }
    assert isinstance(transport, FakeTransport)
    assert transport.redis is client


def test_build_knowledge_models_uses_remote_embedding_and_tiktoken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, Any]] = []

    class FakeEmbedding:
        def __init__(self, config: OpenAICompatibleEmbeddingConfig) -> None:
            self.config = config
            created.append(("embedding", config))

    class FakeCounter:
        def __init__(self) -> None:
            created.append(("counter", ()))

    class FakeTokenizer:
        def __init__(self) -> None:
            created.append(("tokenizer", ()))

    monkeypatch.setattr(bootstrap_knowledge, "OpenAICompatibleEmbeddingClient", FakeEmbedding)
    monkeypatch.setattr(bootstrap_knowledge, "TiktokenTokenCounter", FakeCounter)
    monkeypatch.setattr(bootstrap_knowledge, "JiebaChineseTokenizer", FakeTokenizer)
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "https://embed.example.test/v1")
    monkeypatch.setenv("ATHENA_EMBEDDING_API_KEY", "sk-secret")
    monkeypatch.setenv("ATHENA_EMBEDDING_MODEL", "custom/model")
    monkeypatch.setenv("ATHENA_EMBEDDING_DIMENSIONS", "1024")
    monkeypatch.setenv("ATHENA_EMBEDDING_BATCH_SIZE", "16")

    resources = bootstrap_knowledge.build_knowledge_models()

    assert [item[0] for item in created] == ["embedding", "counter", "tokenizer"]
    assert isinstance(resources.embedding, FakeEmbedding)
    assert resources.embedding.config.base_url == "https://embed.example.test/v1"
    assert resources.embedding.config.model == "custom/model"
    assert resources.embedding.config.dimensions == 1024
    assert resources.embedding.config.batch_size == 16
    assert isinstance(resources.counter, FakeCounter)
    assert isinstance(resources.tokenizer, FakeTokenizer)
    assert resources.profile_id == remote_embedding_profile_id("custom/model", 1024)


def test_build_knowledge_models_rejects_mismatched_vector_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "https://embed.example.test/v1")
    monkeypatch.setenv("ATHENA_EMBEDDING_DIMENSIONS", "1536")

    with pytest.raises(EmbeddingConfigurationError, match="embedding_dimensions_mismatch"):
        bootstrap_knowledge.build_knowledge_models()


def test_production_env_rejects_missing_redis_for_auth_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_databases: list[str] = []

    class FakeDatabase:
        def __init__(self, url: str, **_kwargs: Any) -> None:
            created_databases.append(url)

    monkeypatch.setenv("ATHENA_ENV", "production")
    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://db.invalid/athena")
    monkeypatch.delenv("ATHENA_REDIS_URL", raising=False)
    monkeypatch.setattr(bootstrap_database, "Database", FakeDatabase)

    with pytest.raises(RuntimeError, match="auth_redis_rate_limit_required"):
        bootstrap_api.build_api_dependencies()
    assert created_databases == []


def test_api_dependencies_wire_shared_redis_auth_rate_limiters_and_close_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    close_calls = 0

    class FakeRedis:
        async def aclose(self) -> None:
            nonlocal close_calls
            close_calls += 1

    class FakeDatabase:
        session_factory = object()

        async def dispose(self) -> None:
            return None

    shared_redis: Any = FakeRedis()
    monkeypatch.setenv("ATHENA_ENV", "production")
    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://db.invalid/athena")
    monkeypatch.setenv("ATHENA_REDIS_URL", "redis://cache.invalid:6379/0")
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "false")
    monkeypatch.setenv("ATHENA_FILES_ENABLED", "false")
    monkeypatch.setattr(bootstrap_api, "build_database", lambda _url: FakeDatabase())
    monkeypatch.setattr("bootstrap.redis.Redis.from_url", lambda *_args, **_kwargs: shared_redis)

    deps = bootstrap_api.build_api_dependencies()

    assert deps.redis_client is shared_redis
    assert deps.auth_service is not None
    assert isinstance(deps.auth_service._login_account_limiter, RedisFixedWindowRateLimiter)
    assert isinstance(deps.auth_service._login_ip_limiter, RedisFixedWindowRateLimiter)
    assert isinstance(deps.auth_service._change_password_limiter, RedisFixedWindowRateLimiter)
    assert deps.org_service is not None
    assert deps.auth_service._login_account_limiter._client is shared_redis
    assert deps.run_transport is not None
    assert deps.run_transport._client is shared_redis
    # ATHENA_FILES_ENABLED=false 时仍装配 SkillService 供纯 Markdown 使用，不初始化 MinIO。
    assert deps.file_object_store is None
    assert deps.session_file_service is None
    assert deps.skill_service is not None
    assert deps.skill_service._object_store is None

    asyncio.run(deps.aclose())
    assert close_calls == 1


def test_build_mcp_executor_rejects_invalid_private_host_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_MCP_ALLOWED_PRIVATE_HOSTS", "bad host")
    monkeypatch.delenv("ATHENA_MCP_STDIO_BASE_ENV_NAMES", raising=False)

    with pytest.raises(ValueError, match="ATHENA_MCP_ALLOWED_PRIVATE_HOSTS_invalid"):
        bootstrap_tool.build_mcp_executor()


def test_build_mcp_executor_accepts_valid_allowlist_and_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_MCP_ALLOWED_PRIVATE_HOSTS", "mcp.internal,10.0.0.8")
    monkeypatch.setenv("ATHENA_MCP_STDIO_BASE_ENV_NAMES", "GOOD_NAME")
    monkeypatch.setenv("GOOD_NAME", "value")

    executor = bootstrap_tool.build_mcp_executor(cleanup_timeout_seconds=3.0)

    assert executor is not None


def test_settings_classes_load_typed_frozen_snapshots_and_preserve_error_codes() -> None:
    """各在用 Pydantic v2 配置类支持纯字典加载、不可变约束，并完整保留全部既有错误码。"""
    from pathlib import Path

    from pydantic import ValidationError

    from app.run.config import RunWorkerConfig
    from bootstrap import config as bootstrap_config
    from bootstrap.config import (
        AuthSettings,
        DatabaseSettings,
        FileSkillSettings,
        KnowledgeSettings,
        LoggingSettings,
        McpSettings,
        RedisSettings,
        RunWorkerSettings,
        is_production_env,
    )

    assert not hasattr(bootstrap_config, "AthenaSettings")
    assert not hasattr(RunWorkerConfig(), "claim_batch_size")
    assert not hasattr(RunWorkerSettings(), "claim_batch_size")

    env = {
        "ATHENA_ENV": "PRODUCTION",
        "ATHENA_DATABASE_URL": "postgresql+asyncpg://db.internal/athena",
        "ATHENA_DATABASE_POOL_SIZE": "12",
        "ATHENA_DATABASE_MAX_OVERFLOW": "4",
        "ATHENA_REDIS_URL": "redis://cache.internal:6379/1",
        "ATHENA_COOKIE_SECURE": "true",
        "ATHENA_PUBLIC_ORIGIN": "https://athena.example.com",
        "ATHENA_MCP_ALLOWED_PRIVATE_HOSTS": "mcp.internal,10.0.0.5",
        "ATHENA_MCP_STDIO_BASE_ENV_NAMES": "HOME,PATH",
        "ATHENA_KNOWLEDGE_ENABLED": "true",
        "ATHENA_KNOWLEDGE_SEMANTIC_MIN_SCORE": "0.42",
        "ATHENA_KNOWLEDGE_OCR_CACHE_ROOT": "/var/cache/ocr",
        "ATHENA_KNOWLEDGE_OWNER_CONCURRENCY": "3",
        "ATHENA_KNOWLEDGE_WORKER_CONCURRENCY": "6",
        "ATHENA_FILES_ENABLED": "true",
        "ATHENA_SKILL_PYTHON_EXECUTABLE": "/usr/bin/python3",
        "ATHENA_SKILL_TEMP_ROOT": "/tmp/athena-skills",
        "ATHENA_RUN_WORKER_CONCURRENCY": "8",
        "ATHENA_RUN_LLM_REQUEST_LIMIT": "25",
        "ATHENA_RUN_TOOL_TIMEOUT_SECONDS": "45.5",
        "ATHENA_RUN_TOOL_CONCURRENCY": "6",
        "ATHENA_LOG_LEVEL": "warning",
    }

    database = DatabaseSettings.from_environment(env)
    redis = RedisSettings.from_environment(env)
    auth = AuthSettings.from_environment(env)
    mcp = McpSettings.from_environment(env)
    knowledge = KnowledgeSettings.from_environment(env)
    files = FileSkillSettings.from_environment(env)
    run_worker = RunWorkerSettings.from_environment(env)
    logging_settings = LoggingSettings.from_environment(env)

    assert is_production_env(env) is True
    assert database.pool_size == 12
    assert database.max_overflow == 4
    assert redis.url == "redis://cache.internal:6379/1"
    assert auth.cookie_secure is True
    assert auth.public_origin == "https://athena.example.com"
    assert mcp.allowed_private_hosts == frozenset({"mcp.internal", "10.0.0.5"})
    assert knowledge.enabled is True
    assert knowledge.semantic_min_score == pytest.approx(0.42)
    assert knowledge.ocr_cache_root == "/var/cache/ocr"
    assert not hasattr(knowledge, "upload_min_free_bytes")
    assert not hasattr(knowledge, "parse_memory_mb")
    assert knowledge.owner_concurrency == 3
    assert knowledge.worker_concurrency == 6
    assert files.files_enabled is True
    assert files.python_executable == Path("/usr/bin/python3")
    assert files.temp_root == Path("/tmp/athena-skills")
    assert run_worker.to_config().llm_request_limit == 25
    assert run_worker.to_config().tool_timeout_seconds == pytest.approx(45.5)
    assert logging_settings.level == "WARNING"

    with pytest.raises(ValidationError):
        database.pool_size = 99

    # LOG_LEVEL 兼容回退仅在 allow_legacy_log_level=True 时生效。
    assert LoggingSettings.from_environment({"LOG_LEVEL": "debug"}).level == "INFO"
    assert (
        LoggingSettings.from_environment(
            {"LOG_LEVEL": "debug"},
            allow_legacy_log_level=True,
        ).level
        == "DEBUG"
    )

    with pytest.raises(ValueError, match="athena_knowledge_semantic_min_score_invalid"):
        KnowledgeSettings.from_environment({"ATHENA_KNOWLEDGE_SEMANTIC_MIN_SCORE": "nan"})
    with pytest.raises(ValueError, match="knowledge_worker_concurrency_invalid"):
        KnowledgeSettings.from_environment({"ATHENA_KNOWLEDGE_WORKER_CONCURRENCY": "17"})
    with pytest.raises(ValueError, match="knowledge_worker_concurrency_invalid"):
        KnowledgeSettings.from_environment({"ATHENA_KNOWLEDGE_WORKER_CONCURRENCY": "abc"})
    with pytest.raises(ValueError, match="ATHENA_RUN_LLM_REQUEST_LIMIT_invalid"):
        RunWorkerSettings.from_environment({"ATHENA_RUN_LLM_REQUEST_LIMIT": "99"})
    with pytest.raises(ValueError, match="run_worker_config_invalid"):
        RunWorkerSettings.from_environment(
            {
                "ATHENA_RUN_LEASE_SECONDS": "10",
                "ATHENA_RUN_HEARTBEAT_SECONDS": "10",
            }
        )


def test_close_resources_lifo_isolates_failures_and_preserves_initial_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AsyncExitStack 逆序释放辅助在单项失败时继续清理后续资源，并正确保留首个异常。"""
    import asyncio
    import logging

    from bootstrap.lifecycle import close_resources_lifo

    test_logger = logging.getLogger("test.bootstrap.lifecycle")
    events: list[str] = []

    async def close_db() -> None:
        events.append("db")

    def close_sync_failing() -> None:
        events.append("sync_fail")
        raise RuntimeError("sync_boom")

    async def close_async_failing() -> None:
        events.append("async_fail")
        raise ValueError("async_boom")

    with caplog.at_level(logging.ERROR, logger="test.bootstrap.lifecycle"):
        with pytest.raises(ValueError, match="async_boom"):
            asyncio.run(
                close_resources_lifo(
                    [
                        ("db_failed", close_db),
                        ("sync_failed", close_sync_failing),
                        ("async_failed", close_async_failing),
                    ],
                    log=test_logger,
                    reraise=True,
                )
            )
    assert events == ["async_fail", "sync_fail", "db"]
    assert any("async_failed exception_type=ValueError" in r.message for r in caplog.records)
    assert any("sync_failed exception_type=RuntimeError" in r.message for r in caplog.records)

    # 存在初始异常时，清理失败不覆盖初始异常。
    events.clear()
    initial = KeyError("primary_failure")
    with pytest.raises(KeyError, match="primary_failure"):
        asyncio.run(
            close_resources_lifo(
                [
                    ("db_failed", close_db),
                    ("async_failed", close_async_failing),
                ],
                log=test_logger,
                reraise=True,
                initial_error=initial,
            )
        )
    assert events == ["async_fail", "db"]


def test_api_bootstrap_fixes_auth_repo_dedup_transport_probe_and_skill_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 API 组合根修复：单次 AuthRepo、无 Redis 不重调 build_run_transport、Skill 建库门禁。"""
    import asyncio

    from main import create_app

    auth_repo_inits = 0
    transport_calls = 0

    class FakeDatabase:
        session_factory = object()

        async def dispose(self) -> None:
            return None

    class SpyAuthRepository:
        def __init__(self, _sessions: object) -> None:
            nonlocal auth_repo_inits
            auth_repo_inits += 1

    def spy_build_run_transport(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal transport_calls
        transport_calls += 1
        return None

    monkeypatch.setenv("ATHENA_ENV", "development")
    monkeypatch.setenv("ATHENA_DATABASE_URL", "postgresql+asyncpg://db.invalid/athena")
    monkeypatch.delenv("ATHENA_REDIS_URL", raising=False)
    monkeypatch.setenv("ATHENA_MCP_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("ATHENA_KNOWLEDGE_ENABLED", "false")
    monkeypatch.setenv("ATHENA_FILES_ENABLED", "false")
    monkeypatch.setattr(bootstrap_api, "build_database", lambda _url: FakeDatabase())
    monkeypatch.setattr(bootstrap_api, "SqlAlchemyAuthRepository", SpyAuthRepository)
    monkeypatch.setattr(bootstrap_api, "build_run_transport", spy_build_run_transport)

    # 1. 全量默认装配：AuthRepository 只实例化 1 次，无 Redis 时不二次调用 build_run_transport。
    deps = bootstrap_api.build_api_dependencies()
    assert auth_repo_inits == 1
    assert transport_calls == 0
    assert deps.model_profile_service is not None
    assert deps.auth_service is not None
    assert deps.model_profile_service._auth_reader is deps.auth_service._repository
    asyncio.run(deps.aclose())

    # 2. 注入除 skill_service 外的全部服务时，数据库门禁仍触发并正常装配 Markdown SkillService。
    stub: Any = object()
    deps_skill_only = bootstrap_api.build_api_dependencies(
        project_service=stub,
        message_service=stub,
        session_service=stub,
        run_service=stub,
        auth_service=stub,
        tool_manager=stub,
        model_profile_service=stub,
        system_prompt_service=stub,
        preference_service=stub,
        subagent_service=stub,
        org_service=stub,
        session_file_service=stub,
    )
    assert deps_skill_only.database is not None
    assert deps_skill_only.skill_service is not None
    asyncio.run(deps_skill_only.aclose())

    # 3. create_app 仅保留 5 个实际被读取的 app.state 属性，移除 9 个死属性。
    app = create_app()
    for live_attr in (
        "authenticator",
        "auth_cookie_name",
        "public_origin",
        "tool_manager",
        "preference_service",
    ):
        assert hasattr(app.state, live_attr)
    for dead_attr in (
        "model_profile_service",
        "system_prompt_service",
        "subagent_service",
        "skill_service",
        "session_file_service",
        "run_service",
        "knowledge_service",
        "org_service",
        "knowledge_original_store",
    ):
        assert not hasattr(app.state, dead_attr)
